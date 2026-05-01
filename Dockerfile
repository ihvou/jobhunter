FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

RUN useradd --create-home --uid 10001 jobbot

COPY jobbot ./jobbot
COPY config ./config
COPY input ./input

RUN mkdir -p /app/data && chown -R jobbot:jobbot /app

USER jobbot

CMD ["python", "-m", "jobbot", "serve"]

