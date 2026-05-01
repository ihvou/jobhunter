FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /jobbot

RUN useradd --create-home --uid 10001 jobbot

COPY jobbot ./jobbot
COPY config ./config
COPY input ./input

RUN mkdir -p /jobbot/data /jobbot/workspace/discovery /jobbot/workspace/tuning && chown -R jobbot:jobbot /jobbot

USER jobbot

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "from pathlib import Path; import time; p=Path('/jobbot/data/heartbeat'); raise SystemExit(0 if p.exists() and time.time() - p.stat().st_mtime < 120 else 1)"

CMD ["python", "-m", "jobbot", "serve"]
