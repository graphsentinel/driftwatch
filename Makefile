.PHONY: install test lint eval cluster-up cluster-down obs-up obs-down \
        demo-1 demo-2 demo-3 demo-4 demo-5 deploy clean

PY ?= python3

install:        ## editable install with all dev extras
	$(PY) -m pip install -e ".[all]"

test:           ## run functional suite (TC-F-*)
	$(PY) -m pytest

lint:
	ruff check src tests
	mypy src/driftwatch || true

eval:           ## run drift dataset -> recall / FP-rate / p95 / inverse-scaling
	$(PY) -m driftwatch.cli eval --dataset evaluation/datasets/drift.jsonl

# --- demo cluster (k3d) ---
cluster-up:     ## create the k3d demo cluster
	k3d cluster create -c examples/k3d-cluster-demo/k3d-config.yaml

cluster-down:
	k3d cluster delete driftwatch-demo

# --- observability stack (podman-compose, decoupled) ---
obs-up:         ## start OTel Collector + Jaeger + Prometheus + Grafana (+ Neo4j)
	podman-compose -f examples/k3d-cluster-demo/compose.yaml up -d

obs-down:
	podman-compose -f examples/k3d-cluster-demo/compose.yaml down

deploy:         ## helm install DriftWatch into the current cluster
	helm install driftwatch deploy/helm/driftwatch -f deploy/helm/driftwatch/values-k3d.yaml

# --- the five live scenarios ---
demo-1:; $(PY) -m driftwatch.cli demo tool_substitution
demo-2:; $(PY) -m driftwatch.cli demo scope_escalation
demo-3:; $(PY) -m driftwatch.cli demo sequence_inversion
demo-4:; $(PY) -m driftwatch.cli demo argument_injection
demo-5:; $(PY) -m driftwatch.cli demo retry_storm

clean:
	rm -rf build dist *.egg-info src/*.egg-info .pytest_cache .ruff_cache .mypy_cache
