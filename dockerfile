FROM python:3.12-slim

WORKDIR /app

# Dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App files
COPY . .

# Non-root gebruiker; uid 1000 matcht de PUID uit het compose-voorbeeld.
# Zorg dat /config bestaat en schrijfbaar is voor de app.
RUN useradd --create-home --uid 1000 mpwatcher \
    && mkdir -p /config \
    && chown -R mpwatcher:mpwatcher /config /app
USER mpwatcher

ENV MPWATCHER_CONFIG_DIR=/config

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4)" || exit 1

# Eén worker: de scheduler draait in-process en mag maar één keer bestaan.
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "1", "--threads", "8", "--timeout", "120", "app:app"]
