#!/usr/bin/env bash
# Measures the two non-functional targets the dossier commits to
# (docs/report.html Section 12.2), so they can be reported as measured
# numbers rather than claims — see defense-kit Q6.
#
#   NFR-2  collector agent CPU overhead per node   (target <= 3%)
#   NFR-1  ingestion -> anomaly flag latency, p95  (target <= 5s)
#
# Usage: ./scripts/benchmark.sh
set -euo pipefail
NS=hypertrace

echo "=== NFR-2: collector agent overhead ==="
echo "Sampling collector CPU from cAdvisor via Prometheus over 5 minutes..."

kubectl -n "$NS" port-forward svc/prometheus 19090:9090 >/dev/null 2>&1 &
PF=$!
trap 'kill $PF 2>/dev/null || true' EXIT
sleep 4

# Collector CPU as a percentage of one core, per node.
CPU=$(curl -sS -G 'http://localhost:19090/api/v1/query' \
  --data-urlencode 'query=100 * avg(rate(container_cpu_usage_seconds_total{namespace="hypertrace",pod=~"collector-.*",container!=""}[5m]))' \
  | python -c "import json,sys; r=json.load(sys.stdin)['data']['result']; print(f\"{float(r[0]['value'][1]):.3f}\" if r else 'n/a')")
echo "collector CPU per node: ${CPU}% of one core   (target: <= 3%)"

MEM=$(curl -sS -G 'http://localhost:19090/api/v1/query' \
  --data-urlencode 'query=avg(container_memory_working_set_bytes{namespace="hypertrace",pod=~"collector-.*",container!=""}) / 1024 / 1024' \
  | python -c "import json,sys; r=json.load(sys.stdin)['data']['result']; print(f\"{float(r[0]['value'][1]):.1f}\" if r else 'n/a')")
echo "collector memory per node: ${MEM} MiB"

echo
echo "=== NFR-1: detection latency ==="
echo "Comparing each anomaly's creation time against the cost sample that triggered it."
kubectl -n "$NS" exec timescaledb-0 -- psql -U hypertrace -d hypertrace -tA -F'|' -c "
WITH matched AS (
  SELECT a.created_at,
         (SELECT MAX(c.time) FROM cost_events c
           WHERE c.service = a.service AND c.time <= a.created_at) AS sample_time
    FROM anomalies a
   WHERE a.created_at > now() - interval '1 hour'
)
SELECT count(*),
       ROUND(AVG(EXTRACT(EPOCH FROM (created_at - sample_time)))::numeric, 2),
       ROUND(MAX(EXTRACT(EPOCH FROM (created_at - sample_time)))::numeric, 2)
  FROM matched WHERE sample_time IS NOT NULL;" \
| awk -F'|' '{printf "anomalies measured: %s\nmean latency: %ss\nworst latency: %ss   (target: <= 5s p95)\n", $1, $2, $3}'

echo
echo "Note: latency here is cost-sample -> anomaly-flag. Add the collector's"
echo "poll interval (default 10s, COLLECT_INTERVAL_SECONDS) for true"
echo "metric-emission -> flag latency; the pipeline itself is the part"
echo "measured above."
