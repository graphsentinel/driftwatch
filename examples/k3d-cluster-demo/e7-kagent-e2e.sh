#!/usr/bin/env bash
# E7 / T-E7.5 — real-Kagent MCP-hop e2e (SKELETON, gated on a real cluster).
#
# This is the deploy-able path-B walkthrough, NOT a self-contained CI test: it needs a real
# Kagent install, a real upstream MCP ToolServer, and a model-provider key — pieces this
# repo does not provision. It documents and drives the wiring; the cluster-free proof of the
# enforcement behavior is tests/test_mcp_proxy.py (TC-F-16/17), already green.
#
# Prereqislerin (you provide):
#   - a cluster with DriftWatch installed AND persistence.enabled=true (shared baseline PVC)
#   - a real MCP ToolServer reachable in-cluster (its URL -> UPSTREAM_MCP below)
#   - Kagent Helm-installed (kagent namespace) with a model provider secret
#   - a reconciled+ready baseline for $TASK on the shared PVC (consensus-seed or trusted runs;
#     see Docs/baseline-lifecycle-runbook.md and fr10-e2e.sh for how to seed one)
set -euo pipefail
NS=driftwatch
TASK="${TASK:-investigate_latency}"
UPSTREAM_MCP="${UPSTREAM_MCP:?set UPSTREAM_MCP to the real MCP ToolServer URL the proxy fronts}"
CHART="${CHART:-oci://ghcr.io/graphsentinel/charts/driftwatch}"
VERSION="${VERSION:-0.1.0}"

echo "== 1. deploy/upgrade DriftWatch with the MCP proxy enabled =="
# image must carry the .[mcp] extra (fastmcp); persistence on so the proxy can mount the
# operator-written baseline read-only.
helm upgrade --install driftwatch "$CHART" --version "$VERSION" \
  --namespace "$NS" --create-namespace \
  --set persistence.enabled=true \
  --set mcpProxy.enabled=true \
  --set mcpProxy.upstreamMcp="$UPSTREAM_MCP" \
  --set mcpProxy.taskType="$TASK" \
  --set mcpProxy.action=block
kubectl -n "$NS" rollout status deploy/driftwatch-mcp --timeout=120s

echo "== 2. confirm the proxy loaded the baseline (not cold-start) =="
POD=$(kubectl -n "$NS" get pod -l app.kubernetes.io/component=mcp-proxy -o jsonpath='{.items[0].metadata.name}')
kubectl -n "$NS" exec "$POD" -- python -c "import os; print('baseline db on PVC:', os.path.exists('/data/baselines/driftwatch.db'))"

echo "== 3. point real Kagent at the proxy =="
# adjust the example to your Kagent version first (kubectl explain remotemcpserver)
kubectl apply -f "$(dirname "$0")/manifests/remotemcpserver.yaml"

echo "== 4. drive Kagent and observe enforcement (MANUAL / your harness) =="
cat <<'NOTE'
  Now exercise the agent (your Kagent Agent CR / chat) and check:
    - a within-baseline task: tools/call reaches the real ToolServer, result returned;
    - a drift task (out-of-baseline tool/scope, or out-of-order in the chain): the agent
      gets an MCP error and the real ToolServer is NEVER called;
    - DriftWatch emits gen_ai.evaluation.result with gate.action=block for the denied call.
  Drop-on-retry: if action=drop, confirm the agent doesn't enter a retry loop on the
  "dropped …" MCP error (T-E7.5 acceptance check, see Docs/e7-mcp-proxy-design.md).
NOTE

echo "== 5. teardown (optional) =="
echo "  kubectl delete -f $(dirname "$0")/manifests/remotemcpserver.yaml"
echo "  helm upgrade driftwatch $CHART --version $VERSION -n $NS --set mcpProxy.enabled=false ..."
