# Demo Makefile — k3d cluster + podman-compose observability + the five scenarios.
# Run from this directory (or `make -C examples/k3d-cluster-demo <target>` from the root).
# Project-wide targets (install/test/lint/eval/clean) live in the root Makefile.
.PHONY: cluster-up cluster-down obs-up obs-down deploy host-alias kagent-model \
        demo-1 demo-2 demo-3 demo-4 demo-5 demo-all up down

# ROOT is absolute so demo targets resolve via `make -C ...` or when run from this dir.
# Keep this on its own line: a trailing inline comment would leave spaces in ROOT.
PY ?= python3
ROOT := $(abspath ../..)
CLUSTER ?= driftwatch-demo

# --- k3d demo cluster ---
cluster-up:     ## create the k3d demo cluster
	k3d cluster create -c k3d-config.yaml

cluster-down:
	k3d cluster delete driftwatch-demo

# --- observability stack (podman-compose, decoupled) ---
obs-up:         ## start OTel Collector + Jaeger + Prometheus + Grafana (Neo4j: roadmap, --profile forensics)
	podman-compose -f compose.yaml up -d

obs-down:
	podman-compose -f compose.yaml down

# --- governance plane (Helm, demo values) ---
deploy:         ## helm install DriftWatch with the k3d demo values
	helm install driftwatch $(ROOT)/deploy/helm/driftwatch \
		-f $(ROOT)/deploy/helm/driftwatch/values-k3d.yaml

# --- host access for E9 (Kagent → Ollama on the host) — NOT needed for the core demo ---
# Registers host.k3d.internal in CoreDNS so in-cluster pods reach Ollama by a stable name
# (no IP in any manifest). LOCAL host Ollama by default; for a REMOTE Ollama:
#   make host-alias OLLAMA_HOST=10.0.0.42        (by IP)
#   make host-alias OLLAMA_HOST=ollama.corp.lan  (by DNS name)
host-alias:     ## (E9 only) register host.k3d.internal -> Ollama in CoreDNS
	OLLAMA_HOST="$(OLLAMA_HOST)" ./register-host-alias.sh $(CLUSTER)

# Pick the model provider Kagent uses (DriftWatch is provider-agnostic). Ollama is the
# verified default and auto-wires host-alias; cloud providers read keys from the environment:
#   make kagent-model MODEL_PROVIDER=ollama
#   make kagent-model MODEL_PROVIDER=openai OPENAI_API_KEY=...
#   make kagent-model MODEL_PROVIDER=anthropic ANTHROPIC_API_KEY=...
kagent-model:   ## (E9) configure Kagent's ModelConfig + provider Secret (default: ollama)
	MODEL_PROVIDER="$(MODEL_PROVIDER)" CLUSTER=$(CLUSTER) ./setup-kagent-model.sh

# --- the five live scenarios ---
# Truly standalone: PYTHONPATH points at the in-tree package, so these run with no
# install and no cluster (console-trace fallback). For the OTLP/cluster path, set
# DRIFTWATCH_OTLP_ENDPOINT and run `make up` first.
DEMO = PYTHONPATH=$(ROOT)/src $(PY) -m driftwatch.cli demo
demo-1:; $(DEMO) tool_substitution
demo-2:; $(DEMO) scope_escalation
demo-3:; $(DEMO) sequence_inversion
demo-4:; $(DEMO) argument_injection
demo-5:; $(DEMO) retry_storm

demo-all: demo-1 demo-2 demo-3 demo-4 demo-5   ## run all five scenarios

declared-e2e:   ## (E11/E12) apply AgenticArchitecture + run the declared-layer in-cluster e2e
	./declared-layer-e2e.sh

# --- convenience: full bring-up / tear-down ---
up: obs-up cluster-up deploy          ## obs stack + cluster + helm install
down: cluster-down obs-down           ## tear both down
