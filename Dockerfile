FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /jobhunter

RUN useradd --create-home --uid 10001 jobhunter

COPY jobhunter ./jobhunter
COPY config ./config
COPY input ./input
COPY openclaw/prompts ./openclaw/prompts

RUN mkdir -p /jobhunter/data /jobhunter/workspace/discovery /jobhunter/workspace/tuning && chown -R jobhunter:jobhunter /jobhunter

USER jobhunter

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "from pathlib import Path; import time; p=Path('/jobhunter/data/heartbeat'); raise SystemExit(0 if p.exists() and time.time() - p.stat().st_mtime < 120 else 1)"

CMD ["python", "-m", "jobhunter", "serve"]
