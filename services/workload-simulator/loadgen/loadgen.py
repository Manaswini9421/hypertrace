"""Generates steady baseline traffic against the victim workload.

The detector's decoupling test compares a cost Z-score against a traffic
Z-score, and a Z-score needs variance to be meaningful. With no traffic at
all every sample is zero, the traffic baseline has zero standard deviation,
and `traffic_z` is always 0 — which would make the decoupling condition
trivially true and the test worthless.

So this exists to give the demo a believable business signal: a steady
request rate with mild jitter, which the simulator can then either leave
alone (cost rises, traffic flat -> an incident) or amplify (cost and
traffic rise together -> legitimate growth, no action).
"""

from __future__ import annotations

import os
import random
import time
import urllib.error
import urllib.request

TARGET = os.environ.get("TARGET_URL", "http://victim:8080/work")
BASE_RPS = float(os.environ.get("BASE_RPS", "5"))
JITTER = float(os.environ.get("JITTER_FRACTION", "0.2"))


def main() -> None:
    print(f"loadgen: {BASE_RPS} req/s (±{JITTER:.0%}) against {TARGET}", flush=True)
    while True:
        # Jitter matters: a perfectly constant rate gives the traffic
        # baseline zero variance, and a zero-stddev baseline can never
        # produce a non-zero Z-score for the detector to compare against.
        rate = max(0.1, random.gauss(BASE_RPS, BASE_RPS * JITTER))
        interval = 1.0 / rate
        try:
            urllib.request.urlopen(TARGET, timeout=2).read()
        except (urllib.error.URLError, TimeoutError, OSError):
            pass  # the victim restarts during throttle tests; keep going
        time.sleep(interval)


if __name__ == "__main__":
    main()
