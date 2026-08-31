# Build from the repo root: docker build -f services/cost-intelligence/Dockerfile -t hypertrace/cost-intelligence:dev .
FROM python:3.11-slim

WORKDIR /app

COPY shared/hypertrace_common /tmp/hypertrace_common
RUN pip install --no-cache-dir "/tmp/hypertrace_common[db]" && rm -rf /tmp/hypertrace_common

COPY services/cost-intelligence/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY services/cost-intelligence/app ./app

ENV PYTHONUNBUFFERED=1
CMD ["python", "-m", "app.main"]
