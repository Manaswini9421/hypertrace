"""Minimal stdlib-only HTTP server used as a synthetic "victim" workload for
demoing/testing HyperTrace's anomaly detection — specifically doc Section
2.2's "runaway retry / infinite loop bug" scenario: a request handler that
starts consuming far more CPU than normal, while nothing about real
request/traffic volume changes. POST /burn pegs ~1 CPU core for N seconds
in a background thread so a demo can trigger a controllable, repeatable
cost/CPU spike on command.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


def _burn_cpu(seconds: float) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline:
        pass  # tight loop: pegs one core until the deadline


class Handler(BaseHTTPRequestHandler):
    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if urlparse(self.path).path == "/health":
            self._send_json(200, {"status": "ok"})
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
        pass  # quiet: this is a synthetic load generator, not a real service


if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", 8080), Handler)
    server.serve_forever()
