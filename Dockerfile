FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml README.md alembic.ini ./
COPY app ./app
COPY migrations ./migrations
RUN pip install --upgrade pip && pip install ".[dev]"

# Nur die erfundenen Demo-Mails - nie data/imports oder data/exports, die echte
# Gastdaten enthalten koennen (siehe .dockerignore). Zur Laufzeit kommt data/
# als Volume dazu.
COPY data/sample_emails ./data/sample_emails
COPY tests ./tests

RUN mkdir -p /app/data/exports /app/data/imports

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
