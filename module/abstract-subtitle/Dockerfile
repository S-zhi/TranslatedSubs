FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    WHISPER_DEVICE=cpu \
    WHISPER_MODEL=small \
    WHISPER_DOWNLOAD_ROOT=/models \
    ABSTRACT_SUBTITLE_REQUIRE_API_KEY=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg git libgomp1 util-linux \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py entrypoint.sh ./

RUN useradd --system --uid 10001 whisper \
    && mkdir -p /models \
    && chmod +x /app/entrypoint.sh

USER root

EXPOSE 8010
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8010/health', timeout=3)"

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8010"]
ENTRYPOINT ["/app/entrypoint.sh"]
