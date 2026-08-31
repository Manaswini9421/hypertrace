# Build from the repo root: docker build -f services/workload-simulator/victim/Dockerfile -t hypertrace/victim:dev .
FROM python:3.11-slim

WORKDIR /app
COPY services/workload-simulator/victim/server.py .

ENV PYTHONUNBUFFERED=1
CMD ["python", "server.py"]
