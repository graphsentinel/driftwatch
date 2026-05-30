# DriftWatch — operator + interceptor in one image (entrypoint chosen at runtime).
# Multi-stage: build a wheel, install into a slim runtime.
FROM python:3.12-slim AS build
WORKDIR /src
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir build && python -m build --wheel --outdir /dist

FROM python:3.12-slim AS runtime
LABEL org.opencontainers.image.source="https://github.com/graphsentinel/driftwatch"
LABEL org.opencontainers.image.description="Kubernetes operator that catches AI-agent decision drift before the API"
LABEL org.opencontainers.image.licenses="Apache-2.0"

# install with operator + interceptor extras (kopf, fastapi, uvicorn, otlp)
COPY --from=build /dist/*.whl /tmp/
RUN pip install --no-cache-dir "$(echo /tmp/*.whl)[operator,interceptor]" && rm -rf /tmp/*.whl

# non-root
RUN useradd -u 10001 -m driftwatch
USER 10001

# default to the operator; override `command:` for the interceptor sidecar
#   operator:    driftwatch-operator
#   interceptor: driftwatch-interceptor   (port 8080)
ENTRYPOINT ["driftwatch-operator"]
