# Demo Makefile — k3d cluster + podman-compose observability + the five scenarios.
# Run from this directory (or `make -C examples/k3d-cluster-demo <target>` from the root).
# Project-wide targets (install/test/lint/eval/clean) live in the root Makefile.
.PHONY: cluster-up cluster-down obs-up obs-down deploy \
        demo-1 demo-2 demo-3 demo-4 demo-5 demo-all up down

# ROOT is absolute so demo targets resolve via `make -C ...` or when run from this dir.
# Keep this on its own line: a trailing inline comment would leave spaces in ROOT.
PY ?= python3
ROOT := $(abspath ../..)

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

# --- convenience: full bring-up / tear-down ---
up: obs-up cluster-up deploy          ## obs stack + cluster + helm install
down: cluster-down obs-down           ## tear both down
