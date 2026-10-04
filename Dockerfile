FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt gunicorn

COPY . .

RUN useradd -m -u 1000 reclip && \
    mkdir -p /app/downloads && \
    chown -R reclip:reclip /app
USER reclip

# Put the reclip user's --user installs first so startup yt-dlp updates take effect.
ENV PATH=/home/reclip/.local/bin:$PATH

EXPOSE 8899

HEALTHCHECK --interval=30s --timeout=10s --start-period=30s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8899/api/v1/health', timeout=5)" || exit 1

ENTRYPOINT ["sh", "/app/docker-entrypoint.sh"]
# One worker process (jobs and the download pool live in it); threads serve
# requests, including agents that block on ?wait=.
CMD ["gunicorn", "-b", "0.0.0.0:8899", "-w", "1", "--threads", "16", "--timeout", "600", "--access-logfile", "-", "app:app"]
