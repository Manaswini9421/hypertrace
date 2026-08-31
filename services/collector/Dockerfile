# Build from the repo root: docker build -f services/collector/Dockerfile -t hypertrace/collector:dev .
FROM python:3.11-slim

WORKDIR /app

COPY shared/hypertrace_common /tmp/hypertrace_common
RUN pip install --no-cache-dir /tmp/hypertrace_common && rm -rf /tmp/hypertrace_common

COPY services/collector/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY services/collector/app ./app

ENV PYTHONUNBUFFERED=1
CMD ["python", "-m", "app.main"]
