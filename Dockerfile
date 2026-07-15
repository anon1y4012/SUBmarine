FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# gosu lets the entrypoint drop privileges after fixing /data ownership,
# which keeps upgrades from older (root-owned) volumes working.
RUN apt-get update \
    && apt-get install -y --no-install-recommends gosu \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY entrypoint.sh /entrypoint.sh
COPY templates/ templates/

RUN chmod +x /entrypoint.sh \
    && groupadd --system submarine \
    && useradd --system --gid submarine --no-create-home submarine \
    && mkdir -p /data \
    && chown submarine:submarine /data

VOLUME ["/data"]
EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:5000/api/health', timeout=4).status == 200 else 1)"]

CMD ["/entrypoint.sh"]
