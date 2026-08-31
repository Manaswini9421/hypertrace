# Build from the repo root: docker build -f services/security-signal-adapter/Dockerfile -t hypertrace/security-signal-adapter:dev .
FROM python:3.11-slim

WORKDIR /app

COPY shared/hypertrace_common /tmp/hypertrace_common
RUN pip install --no-cache-dir /tmp/hypertrace_common && rm -rf /tmp/hypertrace_common

COPY services/security-signal-adapter/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY services/security-signal-adapter/app ./app

ENV PYTHONUNBUFFERED=1
EXPOSE 8090
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8090"]
