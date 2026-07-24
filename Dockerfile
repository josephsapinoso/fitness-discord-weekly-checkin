# Fitness check-in bot — Discord HTTP interactions server for Cloud Run
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# Cloud Run sets $PORT (default 8080).
# 1 worker + threads: low traffic, and matplotlib is not fork-safe anyway.
CMD exec gunicorn --bind :${PORT:-8080} --workers 1 --threads 8 --timeout 60 app:app
