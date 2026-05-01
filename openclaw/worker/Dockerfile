FROM node:22-slim

ENV NODE_ENV=production

WORKDIR /openclaw

RUN apt-get update \
  && apt-get install -y --no-install-recommends ca-certificates \
  && rm -rf /var/lib/apt/lists/* \
  && useradd --create-home --uid 10002 openclaw \
  && npm install -g @openai/codex

COPY watcher.js /openclaw/worker/watcher.js

RUN mkdir -p /openclaw/workspace/discovery /openclaw/workspace/tuning /openclaw/codex-home \
  && chown -R openclaw:openclaw /openclaw

USER openclaw

ENV CODEX_HOME=/openclaw/codex-home
ENV HOME=/openclaw/codex-home

CMD ["node", "/openclaw/worker/watcher.js"]
