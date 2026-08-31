# Build from the repo root: docker build -f services/workload-simulator/loadgen/Dockerfile -t hypertrace/loadgen:dev .
FROM python:3.11-slim
WORKDIR /app
COPY services/workload-simulator/loadgen/loadgen.py .
ENV PYTHONUNBUFFERED=1
CMD ["python", "loadgen.py"]
