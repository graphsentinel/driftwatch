#!/usr/bin/env bash
# E11 + E12 — the declared layer (configure → declare → enforce), in-cluster e2e.
#
# Proves end-to-end, across two pods sharing one PVC:
#   1. the operator reconciles an AgenticArchitecture into a declared contract and persists it
#      to the shared data plane (status.contractHash, <DATA_DIR>/contracts/demo-org.json),
#   2. the mcp-proxy loads that contract (DRIFTWATCH_CONTRACT_REF + DRIFTWATCH_AGENT_ID) and
#      checks each call against it BEFORE the statistical baseline,
#   3. on the live MCP hop:
#        E11 single-call : a call to an UNBOUND tool (k8s_pods_delete) is declared-blocked,
#        E12 sequence    : the forbidden ordering (namespaces_list -> pods_list) is declared-blocked,
#      while a single bound call passes.
#
# The statistical action is set to `log` for the run so the DECLARED layer is isolated (declared
# blocks fire regardless of action/baseline); it is restored afterwards. Requires persistence
# enabled so the operator + proxy share a PVC. Run from anywhere; uses $KUBECONFIG.
set -euo pipefail
NS=driftwatch
HERE="$(dirname "$0")"
CR="$HERE/manifests/agenticarchitecture-demo.yaml"

echo "== 0. preconditions =="
kubectl get deploy driftwatch-operator driftwatch-mcp -n "$NS" >/dev/null
PROXY_ACTION_BEFORE="$(kubectl get deploy driftwatch-mcp -n "$NS" \
  -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="DRIFTWATCH_ACTION")].value}')"
PROXY_ACTION_BEFORE="${PROXY_ACTION_BEFORE:-block}"

echo "== 1. apply AgenticArchitecture (operator reconciles into a declared contract) =="
kubectl apply -f "$CR"
echo -n "   waiting for reconcile (status.contractHash) ... "
for _ in $(seq 1 30); do
  H="$(kubectl get agenticarchitecture demo-org -n "$NS" -o jsonpath='{.status.contractHash}' 2>/dev/null || true)"
  [ -n "$H" ] && break; sleep 2
done
[ -n "${H:-}" ] || { echo "FAILED (operator did not reconcile — check operator RBAC for agenticarchitectures)"; exit 1; }
echo "ok (contractHash=$H)"

# Restore the statistical action + failClosed on ANY exit (success, failure, or interrupt) so a
# failed run never leaves the demo cluster in the isolating action=log/failOpen state.
restore() {
  echo "== restore statistical action ($PROXY_ACTION_BEFORE) + failClosed =="
  kubectl set env deploy/driftwatch-mcp -n "$NS" \
    DRIFTWATCH_ACTION="$PROXY_ACTION_BEFORE" DRIFTWATCH_FAILURE_POLICY=failClosed >/dev/null 2>&1 || true
  kubectl rollout status deploy/driftwatch-mcp -n "$NS" --timeout=120s >/dev/null 2>&1 || true
}

echo "== 2. wire the proxy to the contract (contractRef + agentId; action=log to isolate declared) =="
kubectl set env deploy/driftwatch-mcp -n "$NS" \
  DRIFTWATCH_CONTRACT_REF=demo-org DRIFTWATCH_AGENT_ID=demo-agent \
  DRIFTWATCH_ACTION=log DRIFTWATCH_FAILURE_POLICY=failOpen >/dev/null
trap restore EXIT     # from here on, the env is mutated — guarantee restore on any exit
kubectl rollout status deploy/driftwatch-mcp -n "$NS" --timeout=120s >/dev/null

echo "== 3. live declared checks via an in-cluster MCP client =="
OP="$(kubectl get pods -n "$NS" -l app.kubernetes.io/component=operator \
  --field-selector=status.phase=Running -o jsonpath='{.items[0].metadata.name}')"
RC=0
kubectl exec -n "$NS" -i "$OP" -- python - <<'PY' || RC=$?
import asyncio, sys
from fastmcp import Client
from fastmcp.exceptions import ToolError
PROXY = "http://driftwatch-mcp.driftwatch.svc:8000/mcp"
rc = 0
async def _wait_ready():
    # the proxy pod was just rolled (step 2), so its MCP server may not be listening yet
    for _ in range(20):
        try:
            async with Client(PROXY) as c:
                await c.list_tools()
            return
        except Exception:
            await asyncio.sleep(2)
    raise RuntimeError("mcp-proxy did not become ready")
async def main():
    global rc
    await _wait_ready()
    # E11 — UNBOUND tool -> declared violation
    async with Client(PROXY) as c:
        try:
            await c.call_tool("k8s_pods_delete", {"name": "x", "namespace": "demo"})
            print("E11 unbound k8s_pods_delete: NOT blocked  [FAIL]"); rc = 1
        except ToolError as e:
            ok = "declared" in str(e)
            print(f"E11 unbound k8s_pods_delete: {'declared-blocked  [PASS]' if ok else 'blocked (not declared)  [CHECK] '+str(e)[:50]}")
            rc = rc or (0 if ok else 1)
    # E12 — forbidden ordering namespaces_list -> pods_list -> declared sequence
    async with Client(PROXY) as c:
        await c.call_tool("k8s_namespaces_list", {})
        try:
            await c.call_tool("k8s_pods_list", {})
            print("E12 namespaces_list->pods_list: NOT blocked  [FAIL]"); rc = 1
        except ToolError as e:
            ok = "declared" in str(e)
            print(f"E12 namespaces_list->pods_list: {'declared-blocked  [PASS]' if ok else 'blocked (not declared)  [CHECK] '+str(e)[:50]}")
            rc = rc or (0 if ok else 1)
asyncio.run(main())
sys.exit(rc)
PY

# restore() runs here via the EXIT trap (set in step 2), so the env is reverted even on failure.
[ "$RC" -eq 0 ] && echo "== declared-layer e2e: PASS ==" || echo "== declared-layer e2e: FAIL =="
exit "$RC"
