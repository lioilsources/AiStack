# services/audio — orchestrátor. Žádný torch: modely běží ve vlastních
# kontejnerech (audio-music, audio-sfx), tenhle image jen frontuje joby a
# pouští ffmpeg, takže se rebuilduje v sekundách místo v desítkách minut.
FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

ENV AUDIO_DATA_DIR=/data/audio \
    AUDIO_DB_PATH=/data/audio/jobs.db \
    PYTHONUNBUFFERED=1
EXPOSE 8093

HEALTHCHECK --interval=15s --timeout=5s --retries=3 --start-period=10s \
    CMD curl -sf http://localhost:8093/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8093"]
