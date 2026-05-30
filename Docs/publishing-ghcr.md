# Publishing DriftWatch to GHCR (public)

Two artifacts, one registry (`ghcr.io`, owner = **graphsentinel**):

| Artifact | Reference |
|---|---|
| Container image (operator + interceptor) | `ghcr.io/graphsentinel/driftwatch:0.1.0a0` |
| Helm chart (OCI artifact) | `oci://ghcr.io/graphsentinel/charts/driftwatch:0.1.0` |

Both start **private** and are flipped to **public** once. After that, any third party
can `helm install` from the OCI chart and `docker pull` the image with no auth — fully
independent of where you built them.

> **Config is NOT baked into the image.** The image is generic; endpoints/secrets come
> in at deploy time via Helm `values` / `--set` and the `AgentDriftPolicy` CRD. That is
> what lets a stranger reconfigure and run it without rebuilding.

---

## Path A — manual, no GitHub Actions (recommended for you)

Everything below is local `docker` + `helm`. No CI required.

### 0. One-time: a PAT for registry login

GitHub → Settings → Developer settings → **Personal access tokens (classic)** →
scopes `write:packages` + `read:packages`. Export it:

```bash
export GHCR_PAT=ghp_xxxxxxxxxxxx
```

### 1. Create the repo and push the source

```bash
cd driftwatch
git remote add origin https://github.com/graphsentinel/driftwatch.git
git push -u origin main
```

### 2. Build + push the container image

```bash
echo "$GHCR_PAT" | docker login ghcr.io -u graphsentinel --password-stdin

docker build -t ghcr.io/graphsentinel/driftwatch:0.1.0a0 .
docker push       ghcr.io/graphsentinel/driftwatch:0.1.0a0
```

### 3. Package + push the Helm chart (OCI)

The chart bundles the CRD, operator, RBAC (and the optional webhook), so installing it
brings up everything.

```bash
helm registry login ghcr.io -u graphsentinel -p "$GHCR_PAT"

helm package deploy/helm/driftwatch -d /tmp/dwchart        # -> driftwatch-0.1.0.tgz
helm push /tmp/dwchart/driftwatch-0.1.0.tgz oci://ghcr.io/graphsentinel/charts
```

### 4. One-time: flip both packages to Public

GitHub → **graphsentinel** org → **Packages**:

1. `driftwatch` → **Package settings → Change visibility → Public**.
2. `charts/driftwatch` → same.
3. (optional) **Package settings → Manage Actions access** → link the `driftwatch`
   repo with *Read* so the package page shows provenance.

---

## Path B — automated (GitHub Actions), if you ever want it

The repo also ships `.github/workflows/release.yml`: on a `v*` tag it builds and pushes
the **image** with the built-in `GITHUB_TOKEN` (no PAT). It does *not* push the chart —
add a `helm push` step there if you want chart releases automated too. Manual Path A is
the supported flow today.

```bash
git tag v0.1.0a0 && git push origin v0.1.0a0   # triggers the image build+push
```

---

## What a third party does (the whole point)

No access to your machine, your env, or your build — just the public artifacts:

```bash
# 1. install the chart straight from the OCI registry (CRD + operator + RBAC together)
helm install driftwatch oci://ghcr.io/graphsentinel/charts/driftwatch --version 0.1.0 \
  --namespace driftwatch --create-namespace \
  --set otel.endpoint=host.k3d.internal:4317        # their observability target

# 2. confirm the CRD is installed and the operator is up
kubectl get crd agentdriftpolicies.driftwatch.graphsentinel.org
kubectl -n driftwatch get pods

# 3. apply a policy (shadow first, then enforce)
kubectl apply -f https://raw.githubusercontent.com/graphsentinel/driftwatch/main/config/policies/shadow-mode.yaml
# ...watch OTel, then:
kubectl apply -f https://raw.githubusercontent.com/graphsentinel/driftwatch/main/config/policies/kagent-cluster-ops.yaml

# 4. govern an agent pod (manual sidecar path; webhook injector is roadmap)
kubectl apply -f https://raw.githubusercontent.com/graphsentinel/driftwatch/main/deploy/sidecar-manual.yaml
```

That is the scenario end to end: chart installs the governance plane, a policy drives
it, the agent pod gets the interceptor sidecar.

---

## Verify it's really public

```bash
docker logout ghcr.io
docker pull ghcr.io/graphsentinel/driftwatch:0.1.0a0          # must succeed, no login
helm pull oci://ghcr.io/graphsentinel/charts/driftwatch --version 0.1.0   # must succeed
```

## One image, two entrypoints

The single image runs either plane; pick the command per workload (the chart and
`deploy/sidecar-manual.yaml` already set these):

- operator (default): `command: ["driftwatch-operator"]`
- interceptor sidecar: `command: ["driftwatch-interceptor"]` (listens on `:8080`)

## Version bumps

Image tag follows `appVersion` in `Chart.yaml` (`0.1.0a0`); chart tag follows `version`
(`0.1.0`). Bump both, rebuild/push image (step 2), repackage/push chart (step 3).
