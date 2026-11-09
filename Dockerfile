# syntax=docker/dockerfile:1.7
# Spectral Canary Works gateway. Multi-stage: build deps + train fixtures, then a slim non-root runtime.
# The builder retrains v1..v4 from configs and FAILS if the dataset or test-prediction hashes differ from
# the committed manifests, so the image is a cross-platform reproducibility check, not a copied binary.

FROM python:3.12-slim-trixie AS builder
COPY --from=ghcr.io/astral-sh/uv:0.12.23 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/opt/venv OMP_NUM_THREADS=1
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
COPY configs ./configs
COPY manifests ./manifests
COPY contracts ./contracts
RUN uv sync --frozen --no-dev
ENV SCW_HOME=/app/var
RUN /opt/venv/bin/scw train-fixtures

FROM python:3.12-slim-trixie AS runtime
RUN groupadd --system --gid 10001 scw && useradd --system --uid 10001 --gid scw --home /app scw
ENV PATH=/opt/venv/bin:$PATH SCW_HOME=/app/var OMP_NUM_THREADS=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /app/src ./src
COPY --from=builder /app/configs ./configs
COPY --from=builder /app/manifests ./manifests
COPY --from=builder --chown=scw:scw /app/var/data ./var/data
COPY --from=builder --chown=scw:scw /app/var/artifacts ./var/artifacts
RUN mkdir -p var/runs var/secrets var/reports && chown -R scw:scw var
# Writable state lives in volumes so the root filesystem can be mounted read-only.
VOLUME ["/app/var/runs", "/app/var/secrets", "/app/var/reports"]
USER scw
EXPOSE 5120
HEALTHCHECK --interval=10s --timeout=3s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:5120/readyz', timeout=2).status == 200 else 1)"
CMD ["scw", "serve", "--host", "0.0.0.0", "--port", "5120", "--bootstrap-fixture", "container"]
