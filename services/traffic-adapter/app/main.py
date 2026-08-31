"""Traffic Adapter — the business signal (dossier §18.2).

Publishes per-workload request rates onto the bus so the Behaviour Analysis
Engine can compute `traffic_z`, the denominator of the decoupling test in
§24.2. Without this the detector can only observe that cost moved, not
whether that movement was explained by demand — which is the difference
between "cost is up" (what a billing tool tells you) and "cost is up and
nothing asked it to be" (the thing this project claims to detect).

Reads from Prometheus rather than scraping applications directly: Prometheus
already collects any pod annotated `prometheus.io/scrape`, so this service
needs no per-application configuration and adds no load to the workloads it
measures. It runs on its own timer rather than in the hot path, and the
Behaviour Engine keeps the latest value per service in memory, so scoring
never blocks on an HTTP call.
"""

from __future__ import annotations

import logging
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from hypertrace_common.messaging import ROUTING_KEY_TRAFFIC, RabbitMQClient
from hypertrace_common.schemas import TrafficSample

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("traffic-adapter")

PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://prometheus.hypertrace.svc.cluster.local:9090")
POLL_INTERVAL_SECONDS = float(os.environ.get("POLL_INTERVAL_SECONDS", "10"))

# Aggregated by namespace and the workload label rather than by pod, so the
# series key matches the identity the rest of the pipeline uses and does not
# grow with every rollout (§21.5).
QUERY = os.environ.get(
    "TRAFFIC_QUERY",
    'sum by (namespace, app) (rate(http_requests_total[1m]))',
)


def _fetch_rates() -> dict[str, float]:
    url = f"{PROMETHEUS_URL}/api/v1/query?{urllib.parse.urlencode({'query': QUERY})}"
    with urllib.request.urlopen(url, timeout=10) as response:
        payload = __import__("json").loads(response.read())

    if payload.get("status") != "success":
        raise RuntimeError(f"Prometheus returned {payload.get('status')}")

    rates: dict[str, float] = {}
    for series in payload["data"]["result"]:
        metric = series["metric"]
        namespace, workload = metric.get("namespace"), metric.get("app")
        if not namespace or not workload:
            continue
        rates[f"{namespace}/{workload}"] = float(series["value"][1])
    return rates


def main() -> None:
    mq = RabbitMQClient()
    logger.info("traffic-adapter starting: %s every %ss", PROMETHEUS_URL, POLL_INTERVAL_SECONDS)

    while True:
        started = time.monotonic()
        try:
            rates = _fetch_rates()
            now = datetime.now(timezone.utc)
            for service, rps in rates.items():
                mq.publish(
                    ROUTING_KEY_TRAFFIC,
                    TrafficSample(timestamp=now, service=service, requests_per_second=rps).model_dump(),
                )
            if rates:
                logger.info("published traffic for %d service(s)", len(rates))
            else:
                # Worth logging: silence here looks identical to "no traffic",
                # and a detector that reads absent data as zero would treat
                # every service as decoupled and flag everything.
                logger.warning("no traffic series matched %r — is any workload annotated for scraping?", QUERY)
        except Exception:
            logger.exception("traffic poll failed")

        time.sleep(max(0.0, POLL_INTERVAL_SECONDS - (time.monotonic() - started)))


if __name__ == "__main__":
    main()
