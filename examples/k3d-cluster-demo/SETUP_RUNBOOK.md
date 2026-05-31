# k3d Setup Runbook — DriftWatch

Step-by-step to stand up the demo from a clean machine: observability stack →
k3d cluster → DriftWatch governance plane → a policy → generate data. This is the
**setup** companion to [`DEMO_RUNBOOK.md`](DEMO_RUNBOOK.md) (the on-stage choreography).

> **Two control-plane runtimes are in play here.** k3d runs the cluster on **Docker**;
> the observability stack runs on **podman-compose** on the host. They are intentionally
> decoupled — the only link is OTLP to `host.k3d.internal:4317`. See §6 if that link
> misbehaves.

---

## 0. Prerequisites

| Tool | Verified | Note |
|---|---|---|
| `k3d` | v5.8.3 | needs a reachable **Docker** daemon (k3d's node containers run on Docker) |
| `kubectl` | v1.35 | — |
| `helm` | 3.x | OCI support (built-in on 3.8+) |
| `podman` + `podman-compose` | 4.9 | runs the observability stack |
| Docker daemon | reachable | `docker info` must succeed |

```bash
k3d version && kubectl version --client && helm version --short
docker info >/dev/null && echo "docker OK"
podman-compose --version
```

> All `make` targets below are in this directory's [`Makefile`](Makefile). Run them from
> `examples/k3d-cluster-demo/`, or prefix with `make -C examples/k3d-cluster-demo …`.

---

## 1. Clean slate (idempotent)

A `driftwatch-demo` cluster may already exist. Tear it down first so the run is repeatable:

```bash
k3d cluster list                       # is driftwatch-demo already there?
make cluster-down  2>/dev/null || true # k3d cluster delete driftwatch-demo
make obs-down      2>/dev/null || true # podman-compose down
```

---

## 2. Observability stack (podman-compose, on the host)

Brings up the OTel Collector + Jaeger + Prometheus + Grafana. (Neo4j is roadmap and
profile-gated — it does NOT start here; see the README.)

```bash
make obs-up
podman ps --format '{{.Names}}  {{.Status}}'   # collector, jaeger, prometheus, grafana up
```

- Grafana: <http://localhost:3000>  (dashboard: *DriftWatch — agent-decisions*)
- Jaeger:  <http://localhost:16686>
- OTLP gRPC ingress: `:4317` on the host — this is what the cluster targets.

Confirm the collector is actually listening before moving on:

```bash
ss -ltnp 2>/dev/null | grep 4317 || echo "WARN: nothing on :4317 — collector not up yet"
```

---

## 3. k3d cluster

```bash
make cluster-up            # k3d cluster create -c k3d-config.yaml  (name: driftwatch-demo)
kubectl config use-context k3d-driftwatch-demo
kubectl get nodes          # 1 server + 1 agent, Ready
```

`k3d-config.yaml` maps host `:8080 → :80` on the loadbalancer and labels the server node
`driftwatch.io/demo=true`.

**Verify the cluster can reach the host's collector** (the decoupled OTLP link):

```bash
kubectl run otlp-probe --rm -it --restart=Never --image=busybox -- \
  sh -c 'nc -zv host.k3d.internal 4317' || echo "see §6 if this fails"
```

---

## 4. Install the DriftWatch governance plane (CRD + operator + RBAC)

Pick **one** path.

### 4A — From the public GHCR chart (the real-world path)

The chart + image are published and public (verified anonymously):

```bash
helm install driftwatch oci://ghcr.io/graphsentinel/charts/driftwatch --version 0.1.0 \
  --namespace driftwatch --create-namespace \
  -f ../../deploy/helm/driftwatch/values-k3d.yaml      # OTLP → host.k3d.internal:4317
```

The operator pod pulls `ghcr.io/graphsentinel/driftwatch:0.1.0a0` straight from GHCR.

### 4B — From the local chart (iterating on the chart/image)

```bash
make deploy        # helm install driftwatch <repo>/deploy/helm/driftwatch -f values-k3d.yaml
```

`make deploy` does **not** create the namespace or set `--namespace`. If your policies use
`namespace: driftwatch` (the demo ones do), create it first:

```bash
kubectl create namespace driftwatch
```

> Building/iterating the image locally and want the cluster to use *that* exact build
> instead of pulling from GHCR? Side-load it:
> ```bash
> podman build -t ghcr.io/graphsentinel/driftwatch:0.1.0a0 ../..      # or docker
> k3d image import ghcr.io/graphsentinel/driftwatch:0.1.0a0 -c driftwatch-demo
> ```

### Verify the plane is up (either path)

```bash
kubectl get crd agentdriftpolicies.driftwatch.graphsentinel.org      # Established
kubectl -n driftwatch get pods                                       # operator Running
kubectl -n driftwatch logs deploy/driftwatch-operator | tail -20     # reconcile loop alive
```

---

## 5. Apply a policy and generate data

Shadow first (scores everything, blocks nothing — the NFR-5 on-ramp), then enforce:

```bash
kubectl apply -f manifests/agentdriftpolicy-shadow.yaml
kubectl get adp -n driftwatch                       # demo-shadow present
kubectl -n driftwatch get adp demo-shadow -o jsonpath='{.status}{"\n"}'   # baselineReady when seeded
```

Drive traffic and watch it score. The reproducible generator is the in-process demo
(no extra workloads needed):

```bash
make demo-all       # five scenarios → spans/events to OTLP → Grafana/Jaeger
```

Then flip to enforce and re-run to see drift actually blocked:

```bash
kubectl apply -f manifests/agentdriftpolicy-enforce.yaml
make demo-all
```

Observe in **Grafana** (*agent-decisions*) and **Jaeger** (decision-chain traces): each
call carries the `gen_ai.agent.*` schema and a `gen_ai.evaluation.result` event with
`gate.action`.

> `manifests/sample-agents.yaml` deploys **stand-in** agent pods (path A) carrying the
> interceptor sidecar — apply it if you want in-cluster pods rather than the in-process
> generator. The `image:` fields are `PLACEHOLDER-agent-image`; set them to a workload
> that POSTs tool calls to `MCP_PROXY_URL` (or leave the in-process `make demo` as the
> data source).

---

## 6. Troubleshooting

**`host.k3d.internal:4317` unreachable from the cluster.**
k3d (Docker) and the collector (podman-compose) live in different network stacks; the
link works only if the collector publishes `:4317` on the host's `0.0.0.0`.
- Confirm host-side: `ss -ltnp | grep 4317` shows `0.0.0.0:4317` (not `127.0.0.1`).
- If your podman is rootless and only binds loopback, expose it on all interfaces in
  `compose.yaml` (`ports: ["0.0.0.0:4317:4317"]`) and `make obs-down && make obs-up`.
- Last resort: point `values-k3d.yaml` `otel.endpoint` at your host's LAN IP instead of
  `host.k3d.internal`.

**Operator pod `ImagePullBackOff`.**
The image is public, so a clean cluster should pull it. If you're offline or iterating,
side-load with `k3d image import` (see §4B).

**`make deploy` fails: namespace not found.**
`make deploy` doesn't create the namespace; run `kubectl create namespace driftwatch`
first, or use path 4A (`--create-namespace`).

**Helm release already exists.**
`helm -n driftwatch uninstall driftwatch` (or `helm upgrade` instead of `install`).

**Webhook / sidecar auto-injection.**
Off by default in v1alpha1 (the injector binary is roadmap). The supported path is the
manual sidecar (`deploy/sidecar-manual.yaml`) or the stand-in workloads above.

---

## 7. Real Kagent (path B) — roadmap

This runbook covers **path A** (stand-in + in-process generator), which is what's
runnable today. Governing a real, Helm-installed Kagent happens at the **MCP tool-call
hop** via an MCP-proxy adapter (the E7 sprint). See
[`README.md`](README.md#running-against-real-kagent--goose-path-b).

---

## 8. Tear down

```bash
make cluster-down      # k3d cluster delete driftwatch-demo
make obs-down          # podman-compose down
# (helm release goes away with the cluster; to remove it explicitly first:
#  helm -n driftwatch uninstall driftwatch)
```

---

### What's verified vs. what you run

- **Verified in this repo:** image builds (docker + podman) and both entrypoints run;
  the chart lints, renders, and is **pullable anonymously from GHCR**; the five
  scenarios pass standalone (`make demo-all`, no cluster).
- **You run (not exercised end-to-end here):** the live k3d bring-up, the OTLP link from
  cluster to the host collector, and the Grafana/Jaeger views. Work top-to-bottom; §6
  covers the failure most likely on first run (the cross-runtime OTLP link).
