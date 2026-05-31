#!/usr/bin/env bash
# FR-10 / TC-F-21 — in-cluster operator→sidecar handoff e2e.
#
# Proves end-to-end, across two pods sharing one baseline store:
#   1. the operator (control plane) writes a reconciled baseline to the PVC,
#   2. a separate interceptor pod (data plane) mounts that SAME PVC read-only and loads it,
#   3. on the live /v1/tool-call hop a within-baseline call is forwarded and a drift is
#      blocked per the operator-reconciled policy — not against an empty store.
#
# Requires: persistence enabled (helm --set persistence.enabled=true) so the operator and
# the interceptor share a PVC. k3d local-path is hostPath-backed, so both pods must run on
# the same node (handled below). Run from anywhere; uses $KUBECONFIG.
set -euo pipefail
NS=driftwatch
POD_MANIFEST="$(dirname "$0")/manifests/fr10-e2e-interceptor.yaml"
TASK=investigate_latency

echo "== 0. preconditions =="
kubectl -n "$NS" get pvc driftwatch-baseline >/dev/null
OP_POD=$(kubectl -n "$NS" get pod -l app.kubernetes.io/component=operator -o jsonpath='{.items[0].metadata.name}')
OP_NODE=$(kubectl -n "$NS" get pod "$OP_POD" -o jsonpath='{.spec.nodeName}')
echo "operator pod=$OP_POD node=$OP_NODE"

echo "== 1. operator writes a reconciled baseline to the shared PVC =="
# Consensus-seed the cold start (FR-9 producer), then let trusted runs take it to ready
# (runs>=2) — the documented seed→real-runs lifecycle. Writes to /data on the PVC.
kubectl -n "$NS" exec -i "$OP_POD" -- python - "$TASK" <<'PY'
import os, sys
os.environ["DRIFTWATCH_DATA_DIR"] = "/data"
task = sys.argv[1]
from driftwatch.consensus.seed import seed_from_proposals
from driftwatch.operator.policy import validate
from driftwatch.operator.reconcile import Reconciler
from driftwatch.sdk.observation import DecisionChain, ToolCall

spec = {"action": "block",
        "baseline": {"sources": [{"models": ["m1", "m2", "m3"]}], "window": 50},
        "detection": {"features": ["tool", "scope", "sequence", "argSchemaHash"],
                      "threshold": 3.0}}
proposals = {task: {m: [["QueryMetrics", "QueryLogs"]] for m in ("m1", "m2", "m3")}}
seed_from_proposals(proposals, spec, out_dir="/data")        # consensus bootstrap (runs=1)

rec = Reconciler(validate({**spec, "_name": "p"}), persistent=True)  # reload from PVC
for _ in range(3):                                            # real runs take over -> ready
    ch = DecisionChain(task_type=task)
    ch.add(ToolCall(tool="QueryMetrics", scope="ns/app"))
    ch.add(ToolCall(tool="QueryLogs", scope="ns/app"))
    rec.observe(ch, source="successfulRuns")
b = rec.store.get(task)
print("OPERATOR_BASELINE ready=%s tools=%s runs=%s" % (b.ready, sorted(b.expected_tools), b.runs))
PY

echo "== 2. confirm the db is on the PVC =="
kubectl -n "$NS" exec "$OP_POD" -- sh -c 'ls -l /data/baselines/driftwatch.db' || \
  kubectl -n "$NS" exec "$OP_POD" -- python -c "import os;print('db bytes', os.path.getsize('/data/baselines/driftwatch.db'))"

echo "== 3. launch the interceptor pod on the operator's node (RWO PVC) =="
kubectl -n "$NS" delete pod fr10-e2e-interceptor --ignore-not-found --now >/dev/null 2>&1 || true
# inject nodeName so the pod lands on the operator's node and can mount the same PVC
kubectl -n "$NS" apply -f "$POD_MANIFEST" >/dev/null
kubectl -n "$NS" patch pod fr10-e2e-interceptor --type merge \
  -p "{\"spec\":{\"nodeName\":\"$OP_NODE\"}}" >/dev/null 2>&1 || true
echo "waiting for interceptor readiness..."
kubectl -n "$NS" wait --for=condition=Ready pod/fr10-e2e-interceptor --timeout=90s

echo "== 4. probe the live /v1/tool-call hop =="
# within-baseline -> forward (200); drift -> block (403). Probed from inside the pod.
kubectl -n "$NS" exec fr10-e2e-interceptor -- python - <<'PY'
import json, urllib.request
def call(tool):
    req = urllib.request.Request(
        "http://127.0.0.1:8080/v1/tool-call",
        data=json.dumps({"tool": tool, "namespace": "ns/app", "args": {}}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        r = urllib.request.urlopen(req)
        return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())

s1, b1 = call("QueryMetrics")        # within baseline
print("WITHIN_BASELINE  http=%s outcome=%s" % (s1, b1.get("outcome")))
s2, b2 = call("DeleteNamespace")     # drift
print("DRIFT            http=%s outcome=%s kind=%s" % (s2, b2.get("outcome"), b2.get("anomaly_kind")))

ok = (s1 == 200 and b1.get("outcome") == "forward"
      and s2 == 403 and b2.get("outcome") == "block")
print("TC-F-21 RESULT:", "PASS" if ok else "FAIL")
raise SystemExit(0 if ok else 1)
PY
RESULT=$?

echo "== 5. cleanup =="
kubectl -n "$NS" delete pod fr10-e2e-interceptor --ignore-not-found --now >/dev/null 2>&1 || true
exit $RESULT
