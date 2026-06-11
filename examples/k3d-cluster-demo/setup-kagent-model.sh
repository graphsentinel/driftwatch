#!/usr/bin/env bash
# Configure the model provider Kagent uses — independent of DriftWatch.
#
# DriftWatch is provider-agnostic: it governs the agent's tool-selection chain, not which LLM
# produced it. This script only sets up Kagent's ModelConfig + the provider API-key Secret, so
# the demo can run on whatever model the operator prefers. API keys are read from the
# environment and stored in a Kubernetes Secret — never written into this script.
#
# Two levels of support, honestly:
#   - ollama  → VERIFIED path for this demo (key-free, local/remote host Ollama). Auto-wires
#               host.k3d.internal via register-host-alias.sh.
#   - openai / anthropic / gemini / azure / bedrock → SCAFFOLDED: env-validated, Secret created,
#               a ModelConfig emitted. Provider-specific fields track the Kagent chart version —
#               verify against your install (`kubectl explain modelconfig.spec`).
#
# Usage:
#   MODEL_PROVIDER=ollama    ./setup-kagent-model.sh                 # default, verified
#   MODEL_PROVIDER=openai    OPENAI_API_KEY=...    ./setup-kagent-model.sh
#   MODEL_PROVIDER=anthropic ANTHROPIC_API_KEY=... ./setup-kagent-model.sh
#   MODEL_PROVIDER=gemini    GEMINI_API_KEY=...    ./setup-kagent-model.sh
#   MODEL_PROVIDER=azure     AZURE_API_KEY=... AZURE_ENDPOINT=... ./setup-kagent-model.sh
#   MODEL_PROVIDER=bedrock   AWS_ACCESS_KEY_ID=... AWS_SECRET_ACCESS_KEY=... AWS_REGION=... ./setup-kagent-model.sh
# Optional: MODEL=<name>  CLUSTER=<k3d-cluster>  NS=kagent
set -euo pipefail

PROVIDER="${MODEL_PROVIDER:-ollama}"
NS="${NS:-kagent}"
CLUSTER="${CLUSTER:-driftwatch-demo}"
SECRET="kagent-model-provider"
HERE="$(dirname "$0")"

kubectl get ns "$NS" >/dev/null 2>&1 || kubectl create ns "$NS"

# idempotent Secret from env (key value never echoed)
mk_secret() {  # mk_secret KEY1=val1 [KEY2=val2 ...]
  local args=() kv
  for kv in "$@"; do args+=(--from-literal="$kv"); done
  kubectl -n "$NS" create secret generic "$SECRET" "${args[@]}" \
    --dry-run=client -o yaml | kubectl apply -f -
}

emit_modelconfig() {  # emit_modelconfig <provider> <model> <extra-spec-yaml>
  local provider="$1" model="$2" extra="$3"
  cat <<YAML | kubectl apply -f -
apiVersion: kagent.dev/v1alpha2
kind: ModelConfig
metadata:
  name: default-model-config
  namespace: $NS
spec:
  provider: $provider
  model: $model
  apiKeySecret: $SECRET
  apiKeySecretKey: API_KEY
$extra
YAML
}

case "$PROVIDER" in
  ollama)
    echo "== provider: Ollama (VERIFIED path) =="
    MODEL="${MODEL:-qwen3-coder-next:cloud}"
    # 1. network: make host.k3d.internal resolve to the host (or remote) Ollama
    OLLAMA_HOST="${OLLAMA_HOST:-}" "$HERE/register-host-alias.sh" "$CLUSTER"
    OLLAMA_URL="${OLLAMA_BASE_URL:-http://host.k3d.internal:11434}"
    # 2. Kagent's ModelConfig requires an apiKeySecret even for Ollama — use a dummy value.
    mk_secret "API_KEY=ollama-local-no-key"
    # 3. ModelConfig pointing at Ollama by the stable name
    emit_modelconfig Ollama "$MODEL" "  ollama:
    host: $OLLAMA_URL"
    echo "   Ollama model '$MODEL' at $OLLAMA_URL (function-calling capable — required by Kagent)."
    ;;

  openai)
    echo "== provider: OpenAI (scaffolded — verify fields against your Kagent version) =="
    : "${OPENAI_API_KEY:?MODEL_PROVIDER=openai requires OPENAI_API_KEY}"
    MODEL="${MODEL:-gpt-4o-mini}"
    mk_secret "API_KEY=$OPENAI_API_KEY"
    emit_modelconfig OpenAI "$MODEL" "  openAI: {}"
    ;;

  anthropic)
    echo "== provider: Anthropic (scaffolded — verify fields against your Kagent version) =="
    : "${ANTHROPIC_API_KEY:?MODEL_PROVIDER=anthropic requires ANTHROPIC_API_KEY}"
    MODEL="${MODEL:-claude-sonnet-4-5-20250929}"
    mk_secret "API_KEY=$ANTHROPIC_API_KEY"
    emit_modelconfig Anthropic "$MODEL" "  anthropic: {}"
    ;;

  gemini)
    echo "== provider: Gemini (scaffolded — verify fields against your Kagent version) =="
    : "${GEMINI_API_KEY:?MODEL_PROVIDER=gemini requires GEMINI_API_KEY}"
    MODEL="${MODEL:-gemini-2.0-flash}"
    mk_secret "API_KEY=$GEMINI_API_KEY"
    emit_modelconfig Gemini "$MODEL" "  gemini: {}"
    ;;

  azure)
    echo "== provider: AzureOpenAI (scaffolded — verify fields against your Kagent version) =="
    : "${AZURE_API_KEY:?MODEL_PROVIDER=azure requires AZURE_API_KEY}"
    : "${AZURE_ENDPOINT:?MODEL_PROVIDER=azure requires AZURE_ENDPOINT}"
    MODEL="${MODEL:-gpt-4o}"
    mk_secret "API_KEY=$AZURE_API_KEY"
    emit_modelconfig AzureOpenAI "$MODEL" "  azureOpenAI:
    endpoint: $AZURE_ENDPOINT
    apiVersion: ${AZURE_API_VERSION:-2024-08-01-preview}"
    ;;

  bedrock)
    echo "== provider: Bedrock (scaffolded — verify fields against your Kagent version) =="
    : "${AWS_ACCESS_KEY_ID:?MODEL_PROVIDER=bedrock requires AWS_ACCESS_KEY_ID}"
    : "${AWS_SECRET_ACCESS_KEY:?MODEL_PROVIDER=bedrock requires AWS_SECRET_ACCESS_KEY}"
    MODEL="${MODEL:-anthropic.claude-3-5-sonnet-20240620-v1:0}"
    # Bedrock auth is AWS creds, not a single API key — store both; ModelConfig refs the secret.
    kubectl -n "$NS" create secret generic "$SECRET" \
      --from-literal=AWS_ACCESS_KEY_ID="$AWS_ACCESS_KEY_ID" \
      --from-literal=AWS_SECRET_ACCESS_KEY="$AWS_SECRET_ACCESS_KEY" \
      --from-literal=API_KEY="$AWS_SECRET_ACCESS_KEY" \
      --dry-run=client -o yaml | kubectl apply -f -
    emit_modelconfig Bedrock "$MODEL" "  bedrock:
    region: ${AWS_REGION:-us-east-1}"
    ;;

  *)
    echo "FATAL: unknown MODEL_PROVIDER='$PROVIDER'." >&2
    echo "       Supported: ollama (verified) | openai | anthropic | gemini | azure | bedrock" >&2
    exit 2
    ;;
esac

echo "== ModelConfig 'default-model-config' applied in ns/$NS (provider=$PROVIDER). =="
echo "   DriftWatch governs the tool-chain regardless of which provider this is."
