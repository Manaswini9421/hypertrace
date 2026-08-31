"""Thin RabbitMQ wrapper shared by every HyperTrace service.

All services publish/consume through one durable topic exchange
(`hypertrace.events`) so a new consumer (e.g. the Phase 2 Cost Intelligence
Engine) can bind its own queue to the routing keys it cares about without
the publisher (the Phase 1 collector) knowing anything about it — this is
the decoupling the dossier's Section 4.1 argues for.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

import pika
from pika.adapters.blocking_connection import BlockingChannel

from .config import RabbitMQSettings

logger = logging.getLogger(__name__)

EXCHANGE_NAME = "hypertrace.events"
EXCHANGE_TYPE = "topic"

ROUTING_KEY_METRIC = "metric.raw"
ROUTING_KEY_LIFECYCLE = "event.lifecycle"
ROUTING_KEY_COST = "cost.event"
ROUTING_KEY_ANOMALY = "anomaly.flagged"
ROUTING_KEY_REMEDIATION = "remediation.requested"
ROUTING_KEY_SECURITY = "security.signal"
ROUTING_KEY_TRAFFIC = "traffic.sample"


class RabbitMQClient:
    """Wraps a single blocking pika connection/channel.

    Not thread-safe — create one instance per thread/process (the collector
    uses one for its metric-collection loop and a separate one for its event
    watch thread). Reconnects lazily on the next publish/consume call, which
    is sufficient at this message volume.
    """

    def __init__(self, settings: RabbitMQSettings | None = None) -> None:
        self._settings = settings or RabbitMQSettings()
        self._connection: pika.BlockingConnection | None = None
        self._channel: BlockingChannel | None = None

    def _ensure_channel(self) -> BlockingChannel:
        if self._connection is None or self._connection.is_closed:
            params = pika.URLParameters(self._settings.url)
            self._connection = pika.BlockingConnection(params)
            self._channel = self._connection.channel()
            self._channel.exchange_declare(
                exchange=EXCHANGE_NAME, exchange_type=EXCHANGE_TYPE, durable=True
            )
        assert self._channel is not None
        return self._channel

    def publish(self, routing_key: str, payload: dict[str, Any]) -> None:
        """Publishes one message, reconnecting once if the connection died.

        An idle AMQP connection gets reaped by the broker (heartbeat timeout
        or peer reset), and pika only discovers this on the next write —
        `connection.is_closed` still reads False, so the reconnect check in
        _ensure_channel doesn't catch it. Services that publish rarely are
        the ones that get hit: api-bff only publishes when a human clicks
        approve or rollback, so its first click after an idle period failed
        with a 500 until this retry existed.

        This is the AMQP equivalent of SQLAlchemy's `pool_pre_ping`.
        """
        body = json.dumps(payload, default=str).encode("utf-8")
        properties = pika.BasicProperties(content_type="application/json", delivery_mode=2)

        for attempt in (1, 2):
            try:
                self._ensure_channel().basic_publish(
                    exchange=EXCHANGE_NAME, routing_key=routing_key, body=body, properties=properties
                )
                return
            except (pika.exceptions.AMQPError, OSError):
                # Drop the dead connection so _ensure_channel rebuilds it.
                self._discard_connection()
                if attempt == 2:
                    raise
                logger.warning("publish to %s failed on a stale connection, reconnecting", routing_key)

    def _discard_connection(self) -> None:
        try:
            if self._connection is not None and self._connection.is_open:
                self._connection.close()
        except Exception:
            pass  # already dead; nothing to salvage
        finally:
            self._connection = None
            self._channel = None

    def consume(
        self,
        queue: str,
        routing_keys: list[str],
        on_message: Callable[[dict[str, Any]], None],
    ) -> None:
        """Blocking consume loop.

        Declares `queue` durable, binds it to each of `routing_keys`, and
        calls `on_message` with the decoded JSON body for each delivery,
        acking only after the callback returns without raising.
        """
        channel = self._ensure_channel()
        channel.queue_declare(queue=queue, durable=True)
        for key in routing_keys:
            channel.queue_bind(exchange=EXCHANGE_NAME, queue=queue, routing_key=key)

        def _callback(ch, method, _properties, body):
            try:
                on_message(json.loads(body))
            except Exception:
                logger.exception("Failed processing message from %s, dropping (no requeue)", queue)
                ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
            else:
                ch.basic_ack(delivery_tag=method.delivery_tag)

        channel.basic_qos(prefetch_count=20)
        channel.basic_consume(queue=queue, on_message_callback=_callback)
        channel.start_consuming()

    def close(self) -> None:
        if self._connection is not None and self._connection.is_open:
            self._connection.close()
