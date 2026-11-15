# The Python SDK wheel bundles the Claude Code CLI it spawns, so no Node install is needed.
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    ORDER_AGENT_DB=/data/orders.db \
    ORDER_AGENT_ARTIFACTS=/data/artifacts

RUN useradd --create-home --uid 10001 app && mkdir -p /data && chown app:app /data
WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install .

USER app
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s \
  CMD python -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:8000/healthz'); sys.exit(0)"

CMD ["order-agent", "serve", "--host", "0.0.0.0", "--port", "8000"]
