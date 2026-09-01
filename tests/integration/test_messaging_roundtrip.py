"""Integration tests for the real RabbitMQ publish/consume path.

The unit tests in tests/test_messaging.py drive RabbitMQClient against a
fake connection. These drive it against a real broker, so they cover what
the fake cannot: that the topic exchange actually exists with the declared
type, that routing keys bind the way every service assumes, and that a
published MetricEvent survives serialisation intact.
"""

import json
import threading
import time
import uuid
from datetime import datetime, timezone

import pika
import pytest

from hypertrace_common.messaging import (
    EXCHANGE_NAME,
    ROUTING_KEY_COST,
    ROUTING_KEY_METRIC,
    RabbitMQClient,
)
from hypertrace_common.schemas import MetricEvent, ResourceRef


@pytest.fixture
def temp_queue(mq_settings):
    """An exclusive, auto-deleting queue bound to the given routing keys.

    Exclusive so it disappears with the connection even if a test fails —
    these run against the live demo cluster and must not leave queues behind.
    """
    connections = []

    def _make(routing_keys: list[str]) -> tuple[pika.BlockingConnection, str]:
        conn = pika.BlockingConnection(pika.URLParameters(mq_settings.url))
        connections.append(conn)
        channel = conn.channel()
        channel.exchange_declare(exchange=EXCHANGE_NAME, exchange_type="topic", durable=True)
        name = f"itest-{uuid.uuid4().hex[:10]}"
        channel.queue_declare(queue=name, exclusive=True, auto_delete=True)
        for key in routing_keys:
            channel.queue_bind(exchange=EXCHANGE_NAME, queue=name, routing_key=key)
        return conn, name

    yield _make

    for conn in connections:
        if conn.is_open:
            conn.close()


def _drain(conn: pika.BlockingConnection, queue: str, timeout: float = 10.0) -> list[dict]:
    """Polls a queue until it stops yielding messages or the timeout expires."""
    channel = conn.channel()
    messages, deadline = [], time.monotonic() + timeout
    while time.monotonic() < deadline:
        method, _props, body = channel.basic_get(queue=queue, auto_ack=True)
        if method is None:
            if messages:
                break
            time.sleep(0.2)
            continue
        messages.append(json.loads(body))
    return messages


class TestPublishRoundTrip:
    def test_metric_event_survives_the_round_trip(self, mq_settings, temp_queue):
        conn, queue = temp_queue([ROUTING_KEY_METRIC])
        event = MetricEvent(
            timestamp=datetime.now(timezone.utc),
            resource=ResourceRef(
                cluster="itest", namespace="itest-ns", node="itest-node", pod="itest-pod", service="itest-svc"
            ),
            cpu_usage_cores=0.25,
            memory_working_set_bytes=123456789,
            network_rx_bytes_total=42,
        )
        RabbitMQClient(mq_settings).publish(ROUTING_KEY_METRIC, event.model_dump())

        received = _drain(conn, queue)
        assert len(received) == 1
        payload = received[0]
        assert payload["cpu_usage_cores"] == 0.25
        assert payload["memory_working_set_bytes"] == 123456789
        assert payload["resource"]["pod"] == "itest-pod"
        # The consumer side must be able to rebuild the model, not just read
        # loose JSON — this is the contract every downstream service relies on.
        assert MetricEvent.model_validate(payload).resource.service == "itest-svc"

    def test_routing_keys_isolate_consumers(self, mq_settings, temp_queue):
        """cost-intelligence binds only to metric.raw and must never receive
        cost.event, or it would consume its own output in a loop.
        """
        conn, metrics_queue = temp_queue([ROUTING_KEY_METRIC])
        client = RabbitMQClient(mq_settings)
        client.publish(ROUTING_KEY_COST, {"marker": "cost-should-not-arrive"})
        client.publish(ROUTING_KEY_METRIC, {"marker": "metric-should-arrive"})

        markers = [m.get("marker") for m in _drain(conn, metrics_queue)]
        assert "metric-should-arrive" in markers
        assert "cost-should-not-arrive" not in markers

    def test_publish_survives_a_broker_dropped_connection(self, mq_settings, temp_queue):
        """The live regression (bug 5): a connection the broker has closed
        must be rebuilt on the next publish rather than raising. Simulated by
        closing the client's connection out from under it, which is what an
        idle-timeout reap looks like to the next write.
        """
        conn, queue = temp_queue([ROUTING_KEY_METRIC])
        client = RabbitMQClient(mq_settings)
        client.publish(ROUTING_KEY_METRIC, {"marker": "before"})

        client._connection.close()  # what the broker's reaper effectively does

        client.publish(ROUTING_KEY_METRIC, {"marker": "after"})
        markers = [m.get("marker") for m in _drain(conn, queue)]
        assert "before" in markers
        assert "after" in markers, "publish should have reconnected instead of failing"


class TestConsume:
    def test_consume_delivers_published_messages(self, mq_settings):
        """Exercises RabbitMQClient.consume itself — the path every stream
        processor runs on — rather than only the publish half.

        Binds a routing key of its own rather than `metric.raw`: the live
        collector publishes to that key continuously, so a queue bound to it
        picks up real cluster traffic and the assertion becomes a race
        against whichever message arrives first.
        """
        marker = uuid.uuid4().hex
        routing_key = f"itest.consume.{marker[:8]}"
        queue = f"itest-consume-{marker[:8]}"
        received: list[dict] = []
        ready = threading.Event()

        consumer = RabbitMQClient(mq_settings)

        def run():
            def on_message(message):
                received.append(message)
                ready.set()

            try:
                consumer.consume(queue=queue, routing_keys=[routing_key], on_message=on_message)
            except Exception:
                pass  # connection torn down during cleanup

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        time.sleep(2)  # let the consumer declare and bind before publishing

        try:
            RabbitMQClient(mq_settings).publish(routing_key, {"marker": marker})
            assert ready.wait(timeout=15), "consumer did not receive the message"
            assert received[0]["marker"] == marker
        finally:
            # consume() now retries a dropped connection with backoff (bug
            # 10 fix), so deleting the queue out from under it would just
            # look like a transient failure and get silently recreated —
            # unless the loop already knows to expect a stop. Flip the flag
            # directly (a plain attribute write, safe cross-thread under the
            # GIL) rather than calling consumer.close(): that drives pika's
            # BlockingConnection from this thread while the consumer thread
            # has it live inside start_consuming(), which isn't thread-safe
            # and races the connection's internal state.
            consumer._stopping = True
            cleanup = pika.BlockingConnection(pika.URLParameters(mq_settings.url))
            cleanup.channel().queue_delete(queue=queue)
            cleanup.close()
            thread.join(timeout=5)
