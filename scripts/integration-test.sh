#!/usr/bin/env bash
# Runs the integration suite against the live cluster, setting up the
# port-forwards it needs and tearing them down afterwards.
#
# Usage:
#   ./scripts/integration-test.sh              # everything
#   ./scripts/integration-test.sh -m "not slow"  # skip the slow e2e cases
set -euo pipefail

NS=hypertrace
PIDS=()

cleanup() {
  for pid in "${PIDS[@]:-}"; do
    kill "$pid" 2>/dev/null || true
  done
}
trap cleanup EXIT

if ! kubectl -n "$NS" get deployment api-bff >/dev/null 2>&1; then
  echo "The hypertrace namespace isn't up. Bring the cluster up first (see README)." >&2
  exit 1
fi

echo "Opening port-forwards..."
kubectl -n "$NS" port-forward svc/rabbitmq    5672:5672  >/dev/null 2>&1 & PIDS+=($!)
kubectl -n "$NS" port-forward svc/timescaledb 5432:5432  >/dev/null 2>&1 & PIDS+=($!)
kubectl -n "$NS" port-forward svc/api-bff    18000:8000  >/dev/null 2>&1 & PIDS+=($!)

# Wait for all three to accept connections rather than sleeping a fixed
# amount — port-forward startup time varies with cluster load.
python - <<'PY'
import socket, sys, time
targets = [("RabbitMQ", 5672), ("TimescaleDB", 5432), ("api-bff", 18000)]
deadline = time.time() + 30
pending = list(targets)
while pending and time.time() < deadline:
    still = []
    for name, port in pending:
        s = socket.socket(); s.settimeout(1)
        try:
            s.connect(("127.0.0.1", port))
        except OSError:
            still.append((name, port))
        finally:
            s.close()
    pending = still
    if pending:
        time.sleep(1)
if pending:
    print("Never became reachable: " + ", ".join(f"{n}:{p}" for n, p in pending), file=sys.stderr)
    sys.exit(1)
print("All services reachable.")
PY

python -m pytest tests/integration -v "$@"
