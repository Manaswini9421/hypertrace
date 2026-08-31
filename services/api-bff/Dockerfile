# Build from the repo root: docker build -f services/api-bff/Dockerfile -t hypertrace/api-bff:dev .
FROM python:3.11-slim

WORKDIR /app

COPY shared/hypertrace_common /tmp/hypertrace_common
RUN pip install --no-cache-dir "/tmp/hypertrace_common[db]" && rm -rf /tmp/hypertrace_common

COPY services/api-bff/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY services/api-bff/app ./app

ENV PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
