"""Security Signal Adapter — the integration point where a runtime-security
tool's alerts enter HyperTrace's decision pipeline (doc Section 7.4 / 14.3).

Read services/security-signal-adapter/README.md before demoing or writing
about this: the correlation downstream is real, this producer is synthetic.
In production this service is replaced by Falco or Tetragon forwarding real
eBPF alerts onto the same `security.signal` routing key.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import FastAPI
from pydantic import BaseModel, Field

from hypertrace_common.messaging import ROUTING_KEY_SECURITY, RabbitMQClient
from hypertrace_common.schemas import SecuritySignal

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("security-signal-adapter")

app = FastAPI(title="HyperTrace Security Signal Adapter", version="0.1.0")
publish_mq = RabbitMQClient()


class SignalIn(BaseModel):
    service: str
    rule: str = "unexpected_outbound_connection"
    severity: str = "critical"
    detail: dict = Field(default_factory=dict)


@app.post("/signal", status_code=202)
def emit_signal(signal: SignalIn) -> dict[str, str]:
    event = SecuritySignal(
        timestamp=datetime.now(timezone.utc),
        service=signal.service,
        rule=signal.rule,
        severity=signal.severity,
        detail=signal.detail,
    )
    publish_mq.publish(ROUTING_KEY_SECURITY, event.model_dump())
    logger.info("emitted security signal service=%s rule=%s", signal.service, signal.rule)
    return {"status": "emitted", "service": signal.service, "rule": signal.rule}


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}
