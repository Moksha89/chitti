FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY app/requirements.txt .
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --upgrade pip \
    && pip install -r requirements.txt \
    && mkdir -p /app/models \
    && curl -L --fail --silent --show-error \
       -o /app/models/u2net.onnx \
       https://github.com/danielgatis/rembg/releases/download/v0.0.0/u2net.onnx \
    && echo "8d10d2f3bb75ae3b6d527c77944fc5e7dcd94b29809d47a739a7a728a912b491  /app/models/u2net.onnx" \
       | sha256sum -c -
COPY app/pyproject.toml alembic.ini ./
COPY app/chitti ./chitti
COPY alembic /app/alembic
COPY profile /app/profile
COPY projects /app/projects
COPY sandbox/available_fonts.txt /app/sandbox/available_fonts.txt
COPY app/templates /app/templates
CMD ["uvicorn", "chitti.main:app", "--host", "0.0.0.0", "--port", "8000"]
