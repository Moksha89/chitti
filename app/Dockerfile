FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY app/requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt
COPY app/pyproject.toml alembic.ini ./
COPY app/chitti ./chitti
COPY alembic /app/alembic
COPY profile /app/profile
COPY projects /app/projects
COPY app/templates /app/templates
CMD ["uvicorn", "chitti.main:app", "--host", "0.0.0.0", "--port", "8000"]
