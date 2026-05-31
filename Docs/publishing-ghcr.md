# Publishing DriftWatch to GHCR (public)

Two artifacts, one registry (`ghcr.io`, owner = **graphsentinel**):

| Artifact | Reference |
|---|---|
| Container image (operator + interceptor) | `ghcr.io/graphsentinel/driftwatch:0.1.0a0` |
| Helm chart (OCI artifact) | `oci://ghcr.io/graphsentinel/charts/driftwatch:0.1.0` |

Both start **private** and are flipped to **public** once. After that, any third party
can `helm install` from the OCI chart and `podman pull` (or `docker pull`) the image
with no auth — fully independent of where you built them.

> **Config is NOT baked into the image.** The image is generic; endpoints/secrets come
> in at deploy time via Helm `values` / `--set` and the `AgentDriftPolicy` CRD. That is
> what lets a stranger reconfigure and run it without rebuilding.

> **Tag vs digest.** The `:0.1.0a0` tag below is a *convenience* tag — it can be re-pushed,
> so it is mutable. For anything that must be reproducible (a review, a runbook, the
> on-stage demo) **pin the immutable digest** instead and install with
> `--set-string image.digest=sha256:…` (the chart honors `image.digest` over the tag). The
> tag-push steps below are the publish flow; see **[Release immutability](#release-immutability--pin-the-digest-dont-trust-the-tag)** for cutting a fresh alpha tag, capturing the digest, and verifying published-vs-running.

---

## Path A — manual, no GitHub Actions (recommended for you)

Everything below is local container tooling + `helm`. No CI required. **Both `podman`
and `docker` work** — the commands are identical, so use whichever you have (examples
below use `podman`; swap in `docker` verbatim if you prefer).

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
echo "$GHCR_PAT" | podman login ghcr.io -u graphsentinel --password-stdin

podman build -t ghcr.io/graphsentinel/driftwatch:0.1.0a0 .
podman push       ghcr.io/graphsentinel/driftwatch:0.1.0a0
```

> The image is built and smoke-tested locally: `podman build` succeeds and the
> `driftwatch-interceptor` entrypoint serves `/healthz` (200) and `/v1/tool-call`
> (403 on drift). `driftwatch-operator` is the default entrypoint.

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
kubectl apply -f https://raw.githubusercontent.com/graphsentinel/driftwatch/main/examples/k3d-cluster-demo/manifests/agentdriftpolicy-shadow.yaml
# ...watch OTel, then:
kubectl apply -f https://raw.githubusercontent.com/graphsentinel/driftwatch/main/examples/k3d-cluster-demo/manifests/agentdriftpolicy-enforce.yaml

# 4. govern an agent pod (manual sidecar path; webhook injector is roadmap)
kubectl apply -f https://raw.githubusercontent.com/graphsentinel/driftwatch/main/deploy/sidecar-manual.yaml
```

That is the scenario end to end: chart installs the governance plane, a policy drives
it, the agent pod gets the interceptor sidecar.

> The manifests above are **path A** (a stand-in / BYO-agent sidecar). For governing a
> real, Helm-installed Kagent at the MCP tool-call hop (**path B**), see
> `examples/k3d-cluster-demo/README.md` — the MCP-proxy adapter is the next sprint.

---

## Verify it's really public

```bash
podman logout ghcr.io
podman pull ghcr.io/graphsentinel/driftwatch:0.1.0a0          # must succeed, no login
helm pull oci://ghcr.io/graphsentinel/charts/driftwatch --version 0.1.0   # must succeed
```

## Release immutability — pin the digest, don't trust the tag

A mutable tag (`0.1.0a0`) that gets re-pushed several times is a trust problem for an
outside reviewer: `podman pull ...:0.1.0a0` on two different days can resolve to two
different digests. For review, runbooks, and the on-stage demo, **pin the immutable
digest**, and cut a **new alpha tag** rather than re-pushing the old one.

**On release — bump to a fresh alpha tag, never repush an existing one:**

```bash
TAG=0.1.0a1                     # PEP 440 alpha; bump per release, do NOT reuse 0.1.0a0
podman build -t ghcr.io/graphsentinel/driftwatch:$TAG .
podman push ghcr.io/graphsentinel/driftwatch:$TAG

# capture the immutable digest the registry assigned, and record it in the runbook
podman inspect --format '{{ index .RepoDigests 0 }}' ghcr.io/graphsentinel/driftwatch:$TAG
# -> ghcr.io/graphsentinel/driftwatch@sha256:<digest>   <-- this is what you cite
```

**Verify what's actually published (and what a cluster is running):**

```bash
# the digest the tag currently resolves to in the registry
skopeo inspect docker://ghcr.io/graphsentinel/driftwatch:0.1.0a1 | jq -r .Digest

# the digest a live pod is running (must match the line above)
kubectl -n driftwatch get pod -l app.kubernetes.io/name=driftwatch \
  -o jsonpath='{.items[0].status.containerStatuses[0].imageID}{"\n"}'
```

**Pin the digest in Helm for review/demo** (so the install is reproducible regardless of
later re-tags). `values.yaml` carries `image.digest: ""`; set it and the operator template
renders `repository@digest`, ignoring the tag:

```bash
helm install driftwatch oci://ghcr.io/graphsentinel/charts/driftwatch --version 0.1.0 \
  --namespace driftwatch --create-namespace \
  --set-string image.digest="sha256:<digest>"
# When image.digest is set the operator template pins repository@digest and ignores the
# tag; leave it empty to track the tag (see templates/operator.yaml + values.yaml).
```

**Smoke-check that a pulled/running image actually carries the latest code:**

```bash
podman run --rm ghcr.io/graphsentinel/driftwatch:0.1.0a1 \
  python -c "from driftwatch.consensus import quorum_for; print('consensus OK', quorum_for(4))"
```

> Canonical digest as of the last publish: **record the current one here on every release.**
> Do not chase an older digest — the canonical image is the one currently published and
> running with the latest code (verify with the consensus smoke check above).

## One image, two entrypoints

The single image runs either plane; pick the command per workload (the chart and
`deploy/sidecar-manual.yaml` already set these):

- operator (default): `command: ["driftwatch-operator"]`
- interceptor sidecar: `command: ["driftwatch-interceptor"]` (listens on `:8080`)

## Version bumps

Image tag follows `appVersion` in `Chart.yaml` (`0.1.0a0`); chart tag follows `version`
(`0.1.0`). Bump both, rebuild/push image (step 2), repackage/push chart (step 3).
