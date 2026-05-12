ARG OPENCLAW_BASE_IMAGE=ghcr.io/openclaw/openclaw:2026.5.7-slim
FROM ${OPENCLAW_BASE_IMAGE}

USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 ca-certificates \
    && rm -rf /var/lib/apt/lists/*

USER node
