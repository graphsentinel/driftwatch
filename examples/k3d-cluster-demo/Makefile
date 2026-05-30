# Demo Makefile — k3d cluster + podman-compose observability + the five scenarios.
# Run from this directory (or `make -C examples/k3d-cluster-demo <target>` from the root).
# Project-wide targets (install/test/lint/eval/clean) live in the root Makefile.
.PHONY: cluster-up cluster-down obs-up obs-down deploy \
        demo-1 demo-2 demo-3 demo-4 demo-5 demo-all up down

PY ?= python3
ROOT := ../..

# --- k3d demo cluster ---
cluster-up:     ## create the k3d demo cluster
	k3d cluster create -c k3d-config.yaml

cluster-down:
	k3d cluster delete driftwatch-demo

# --- observability stack (podman-compose, decoupled) ---
obs-up:         ## start OTel Collector + Jaeger + Prometheus + Grafana (+ Neo4j)
	podman-compose -f compose.yaml up -d

obs-down:
	podman-compose -f compose.yaml down

# --- governance plane (Helm, demo values) ---
deploy:         ## helm install DriftWatch with the k3d demo values
	helm install driftwatch $(ROOT)/deploy/helm/driftwatch \
		-f $(ROOT)/deploy/helm/driftwatch/values-k3d.yaml

# --- the five live scenarios (standalone — need only `make -C .. install`) ---
demo-1:; $(PY) -m driftwatch.cli demo tool_substitution
demo-2:; $(PY) -m driftwatch.cli demo scope_escalation
demo-3:; $(PY) -m driftwatch.cli demo sequence_inversion
demo-4:; $(PY) -m driftwatch.cli demo argument_injection
demo-5:; $(PY) -m driftwatch.cli demo retry_storm

demo-all: demo-1 demo-2 demo-3 demo-4 demo-5   ## run all five scenarios

# --- convenience: full bring-up / tear-down ---
up: obs-up cluster-up deploy          ## obs stack + cluster + helm install
down: cluster-down obs-down           ## tear both down
