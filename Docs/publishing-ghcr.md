# Publishing DriftWatch to GHCR (public)

The image is `ghcr.io/<owner>/driftwatch` where `<owner>` is the GitHub user or org
that owns the repo. Two paths: the automated CI path (recommended) and a one-off
manual push. Either way, the package starts **private** and is flipped to **public**
once.

## Prerequisites

- A GitHub repo for DriftWatch (create one under your user or the `graphsentinel` org).
- The Helm image ref matches the owner. If your owner isn't `graphsentinel`, update:
  - `deploy/helm/driftwatch/values.yaml` → `image.repository`
  - `deploy/sidecar-manual.yaml` → the `driftwatch-interceptor` image

## Path A — automated (GitHub Actions → GHCR)

The repo ships `.github/workflows/release.yml`. It builds the image and pushes to GHCR
using the built-in `GITHUB_TOKEN` (no PAT needed) because the job has
`permissions: packages: write`.

```bash
# 1. create the repo and push (replace OWNER)
gh repo create OWNER/driftwatch --public --source=. --remote=origin --push
#    or, manually:
# git remote add origin https://github.com/OWNER/driftwatch.git
# git push -u origin main

# 2. cut a version tag — this triggers the build + push
git tag v0.1.0a0
git push origin v0.1.0a0
```

The workflow publishes `ghcr.io/OWNER/driftwatch:0.1.0a0` (+ `:latest` on main,
`:sha-xxxxxxx`).

### Make the package public (one-time)

GHCR packages are private by default. After the first successful push:

1. GitHub → your profile/org → **Packages** → `driftwatch`.
2. **Package settings** → **Change visibility** → **Public**.
3. (org repos) **Package settings** → **Manage Actions access** → ensure the repo has
   *Write* so future CI runs can push.

After that, anyone can `docker pull ghcr.io/OWNER/driftwatch:0.1.0a0` with no auth, and
`helm install` works on a fresh cluster.

## Path B — manual one-off push (no CI)

```bash
# 1. a classic PAT with write:packages scope (Settings → Developer settings → Tokens)
echo "$GHCR_PAT" | docker login ghcr.io -u OWNER --password-stdin

# 2. build + tag + push
docker build -t ghcr.io/OWNER/driftwatch:0.1.0a0 .
docker push ghcr.io/OWNER/driftwatch:0.1.0a0

# 3. flip the package to Public in the GitHub Packages UI (same as Path A).
```

## Verify it's public

```bash
docker logout ghcr.io
docker pull ghcr.io/OWNER/driftwatch:0.1.0a0    # must succeed with no login
```

## One image, two entrypoints

The single image runs either plane; pick the command per workload:

- operator (default): `command: ["driftwatch-operator"]`
- interceptor sidecar: `command: ["driftwatch-interceptor"]` (listens on `:8080`)

The Helm chart and `deploy/sidecar-manual.yaml` already set these.
