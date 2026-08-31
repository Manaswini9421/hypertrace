"""Minimal stdlib-only HTTP server used as a synthetic "victim" workload.

Serves two purposes for the demo:

  * `/work` is ordinary traffic. Every request increments a counter exposed
    at `/metrics` in Prometheus text format, which gives the detector its
    business signal — the denominator of the decoupling test in dossier
    §24.2. Each request also does a small slice of real CPU work, so a
    traffic surge genuinely raises cost: that is the
    `legitimate_traffic_growth` case, where cost and traffic move together
    and the detector must decline to act.

  * `/burn` pegs a CPU core for N seconds *without* serving any extra
    traffic, reproducing §2.2's "runaway retry" incident: resources and
    cost climb while request volume stays flat.

The two endpoints exist separately precisely so a demo can move cost and
traffic independently, which is the whole point of the decoupling test.
"""

from __future__ import annotations

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

_requests_total = 0
_counter_lock = threading.Lock()


def _record_request() -> None:
    global _requests_total
    with _counter_lock:
        _requests_total += 1


# Tuned so one request is a small but measurable slice of CPU: at the
# demo's baseline rate the cost signal stays low, and a large traffic surge
# lifts it clearly.
WORK_ITERATIONS = int(os.environ.get("WORK_ITERATIONS", "20000"))


def _work(iterations: int) -> int:
    total = 0
    for i in range(iterations):
        total += i * i
    return total


def _burn_cpu(seconds: float) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline:
        pass  # tight loop: pegs one core until the deadline


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: int, payload: dict) -> None:
        self._send(status, json.dumps(payload).encode("utf-8"), "application/json")

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/health":
            self._send_json(200, {"status": "ok"})
        elif path == "/metrics":
            with _counter_lock:
                total = _requests_total
            body = (
                "# HELP http_requests_total Requests served by the victim workload.\n"
                "# TYPE http_requests_total counter\n"
                f"http_requests_total {total}\n"
            ).encode("utf-8")
            self._send(200, body, "text/plain; version=0.0.4")
        elif path == "/work":
            # Does a small, fixed amount of real work per request, so serving
            # more traffic genuinely costs more CPU. That is what makes
            # `legitimate_traffic_growth` demonstrable: cost and traffic rise
            # together and the detector must decline to act. A free endpoint
            # would leave cost flat under load and the decoupling test would
            # never see the case it exists to recognise.
            _record_request()
            _work(WORK_ITERATIONS)
            self._send_json(200, {"ok": True})
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != "/burn":
            self._send_json(404, {"error": "not found"})
            return
        params = parse_qs(parsed.query)
        seconds = float(params.get("seconds", ["30"])[0])
        threading.Thread(target=_burn_cpu, args=(seconds,), daemon=True).start()
        self._send_json(202, {"burning_seconds": seconds})

    def log_message(self, format: str, *args) -> None:
        pass  # quiet: this is a synthetic load target, not a real service


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
