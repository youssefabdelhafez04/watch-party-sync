FROM python:3.12-slim AS runtime

WORKDIR /app

ARG INSTALL_FFMPEG=1
RUN apt-get update \
    && if [ "$INSTALL_FFMPEG" = "1" ]; then apt-get install -y --no-install-recommends ffmpeg; fi \
    && rm -rf /var/lib/apt/lists/* \
    && addgroup --system app \
    && adduser --system --ingroup app --home /app app \
    && install -d -o app -g app /app/data

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=app:app server.py ./server.py
COPY --chown=app:app watchparty_telemetry.py ./watchparty_telemetry.py
COPY --chown=app:app static ./static
COPY --chown=app:app tools/usage_summary.py ./tools/usage_summary.py

USER app
ENV PORT=8000 PYTHONUNBUFFERED=1 FFMPEG_VIDEO_ENCODER=libx264 FFMPEG_PRESET=veryfast
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.getenv('PORT','8000')+'/health', timeout=3)"
CMD ["sh", "-c", "exec uvicorn server:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1 --proxy-headers"]
