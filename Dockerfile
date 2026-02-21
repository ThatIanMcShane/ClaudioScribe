FROM python:3.11-slim

ENV LANG=C.UTF-8

RUN apt-get update && apt-get install -y \
    ffmpeg \
    git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip "setuptools<71" wheel && \
    pip install --no-cache-dir torch torchaudio --index-url https://download.pytorch.org/whl/cpu && \
    pip install --no-cache-dir --no-build-isolation -r requirements.txt

COPY pipeline.py watcher.py doc_writer.py config.py settings_app.py start.sh plaud_client.py plaud_watcher.py gdrive_client.py ./
COPY templates/ templates/

RUN useradd -r -m pipeline \
    && mkdir -p /watch /tmp/claudioscribe /app/config \
    && chown -R pipeline:pipeline /watch /tmp/claudioscribe /app/config /app

EXPOSE 8080

USER pipeline

CMD ["bash", "start.sh"]
