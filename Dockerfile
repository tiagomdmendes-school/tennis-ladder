# The app has no dependencies, so this image is just Python plus the source.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    LADDER_DATA_DIR=/data

WORKDIR /app
COPY ladder/ ./ladder/
COPY tools/ ./tools/
COPY run.py ./

# Everything that must survive a redeploy lives here: the SQLite file and
# config.json. Mount a volume at /data or you lose the ladder on every deploy.
VOLUME ["/data"]
RUN mkdir -p /data

# Run as a non-root user, and let it write to the data directory.
RUN useradd --create-home --uid 10001 ladder && chown -R ladder /data /app
USER ladder

EXPOSE 8000
CMD ["python3", "run.py", "--host", "0.0.0.0", "--port", "8000", "--data-dir", "/data"]
