FROM node:22-bookworm-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    NEXT_TELEMETRY_DISABLED=1 HOME=/workspace/.home NPM_CONFIG_CACHE=/workspace/.npm-cache
RUN apt-get update && apt-get install -y --no-install-recommends git python3 python3-pip \
    && rm -rf /var/lib/apt/lists/* \
    && python3 -m pip install --break-system-packages --no-cache-dir playwright \
    && python3 -m playwright install --with-deps chromium
RUN groupadd --gid 65532 worker && useradd --uid 65532 --gid 65532 --create-home --shell /usr/sbin/nologin worker
COPY screenshot.py /opt/screenshot.py
COPY next_screenshot.py /opt/next_screenshot.py
COPY fixture /opt/fixture
COPY available_fonts.txt /opt/available_fonts.txt

RUN set -eu; while IFS= read -r family; do \
      test -z "$family" || fc-match --format='%{family}\n' "$family" | tr ',' '\n' | grep -Fqx "$family"; \
    done < /opt/available_fonts.txt

USER 65532:65532
WORKDIR /workspace
