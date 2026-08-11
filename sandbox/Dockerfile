FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir playwright && playwright install --with-deps chromium
RUN groupadd --gid 65532 worker && useradd --uid 65532 --gid 65532 --create-home --shell /usr/sbin/nologin worker
COPY screenshot.py /opt/screenshot.py
USER 65532:65532
WORKDIR /workspace
