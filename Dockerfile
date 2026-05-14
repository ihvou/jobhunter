FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /jobhunter

RUN useradd --create-home --uid 10001 jobhunter

COPY jobhunter ./jobhunter
COPY config ./config
COPY input ./input
COPY skills ./skills

RUN mkdir -p /jobhunter/data && chown -R jobhunter:jobhunter /jobhunter

USER jobhunter

EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/health', timeout=2).read()"

CMD ["python", "-m", "jobhunter", "service"]
