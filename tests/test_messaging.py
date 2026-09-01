"""Regression tests for the RabbitMQ publish and consume paths.

TestPublishReconnect covers the stale-connection bug found in live testing:
the api-bff only publishes when a human clicks approve or rollback, so its
AMQP connection sits idle for long stretches and gets reaped by the broker.
pika only discovers this on the next write — `connection.is_closed` still
reads False — so the reconnect check in _ensure_channel never fired and the
first click after an idle period returned a 500.

TestConsumeReconnect covers a second live bug found via the demo dashboard:
consume() had no retry at all, so a connection failure on the very first
connect attempt (e.g. DNS not ready yet) permanently killed that consumer's
bare daemon thread while the pod stayed "healthy".
"""

import socket

import pika
import pytest

from hypertrace_common.messaging import EXCHANGE_NAME, RabbitMQClient


class FakeChannel:
    def __init__(self, fail_times: int = 0):
        self.fail_times = fail_times
        self.publish_calls = 0
        self.declared_exchanges: list[str] = []

    def exchange_declare(self, exchange, **_kwargs):
        self.declared_exchanges.append(exchange)

    def basic_publish(self, **_kwargs):
        self.publish_calls += 1
        if self.publish_calls <= self.fail_times:
            raise pika.exceptions.StreamLostError("Stream connection lost")


class FakeConnection:
    def __init__(self, channel: FakeChannel):
        self._channel = channel
        self.is_closed = False
        self.is_open = True
        self.close_calls = 0

    def channel(self):
        return self._channel

    def close(self):
        self.close_calls += 1
        self.is_closed = True
        self.is_open = False


@pytest.fixture
def connect_recorder(monkeypatch):
    """Replaces pika's connection factory and records every connection made,
    so a test can assert whether a reconnect actually happened.
    """
    made: list[FakeConnection] = []
    channels: list[FakeChannel] = []

    def factory(fail_times_per_connection):
        def _connect(_params):
            channel = FakeChannel(fail_times=fail_times_per_connection.pop(0) if fail_times_per_connection else 0)
            connection = FakeConnection(channel)
            channels.append(channel)
            made.append(connection)
            return connection

        return _connect

    def install(fail_times_per_connection):
        monkeypatch.setattr(pika, "BlockingConnection", factory(list(fail_times_per_connection)))
        return made, channels

    return install


class TestPublishReconnect:
    def test_publishes_over_a_healthy_connection(self, connect_recorder):
        made, channels = connect_recorder([0])
        RabbitMQClient().publish("metric.raw", {"hello": "world"})
        assert len(made) == 1, "a healthy publish should not reconnect"
        assert channels[0].publish_calls == 1
        assert channels[0].declared_exchanges == [EXCHANGE_NAME]

    def test_reconnects_and_retries_after_a_stale_connection(self, connect_recorder):
        """The actual regression: first write fails because the broker reaped
        the idle connection, so the client must rebuild it and retry rather
        than surfacing a 500 to the user.
        """
        made, channels = connect_recorder([1, 0])  # first connection fails once, second is healthy
        RabbitMQClient().publish("remediation.requested", {"action": "rollback"})
        assert len(made) == 2, "should have discarded the dead connection and made a new one"
        assert made[0].close_calls == 1, "the dead connection should be closed, not leaked"
        assert channels[1].publish_calls == 1, "the retry should succeed on the fresh connection"

    def test_gives_up_after_one_retry(self, connect_recorder):
        """A broker that is genuinely down must surface an error rather than
        retrying forever and hanging the request.
        """
        made, _ = connect_recorder([1, 1])
        with pytest.raises(pika.exceptions.AMQPError):
            RabbitMQClient().publish("remediation.requested", {"action": "rollback"})
        assert len(made) == 2, "exactly one retry, then propagate"

    def test_reuses_the_connection_across_publishes(self, connect_recorder):
        made, channels = connect_recorder([0])
        client = RabbitMQClient()
        client.publish("metric.raw", {"n": 1})
        client.publish("metric.raw", {"n": 2})
        assert len(made) == 1, "a healthy client should not reconnect per message"
        assert channels[0].publish_calls == 2


class _StopTest(Exception):
    """Marks 'the loop reached the point under test' without being one of
    the (AMQPError, OSError) types consume() retries on, so it always
    propagates out and ends the test instead of looping forever.
    """


class FakeConsumeChannel:
    """A channel double for exercising RabbitMQClient.consume in isolation.

    `start_consuming_raises` is a list of exceptions to raise on successive
    calls (looped/reused on the last entry once exhausted) — modelling a
    connection that drops one or more times before settling, or before the
    test's sentinel ends the call.
    """

    def __init__(self, start_consuming_raises: list[Exception]):
        self._start_consuming_raises = start_consuming_raises
        self.start_consuming_calls = 0
        self.declared_queues: list[str] = []
        self.bound_keys: list[str] = []
        self.qos_calls = 0
        self.consume_calls = 0

    def exchange_declare(self, exchange, **_kwargs):
        pass

    def queue_declare(self, queue, **_kwargs):
        self.declared_queues.append(queue)

    def queue_bind(self, exchange, queue, routing_key):
        self.bound_keys.append(routing_key)

    def basic_qos(self, **_kwargs):
        self.qos_calls += 1

    def basic_consume(self, **_kwargs):
        self.consume_calls += 1

    def start_consuming(self):
        index = min(self.start_consuming_calls, len(self._start_consuming_raises) - 1)
        self.start_consuming_calls += 1
        raise self._start_consuming_raises[index]


class FakeConsumeConnection:
    def __init__(self, channel: FakeConsumeChannel):
        self._channel = channel
        self.is_closed = False
        self.is_open = True
        self.close_calls = 0

    def channel(self):
        return self._channel

    def close(self):
        self.close_calls += 1
        self.is_closed = True
        self.is_open = False


class TestConsumeReconnect:
    """Regression tests for bug 10: consume() used to let any connection
    failure — including the very first connect attempt — propagate straight
    out of the bare daemon thread every caller runs it on. That silently
    killed the consumer forever while the pod stayed 'healthy', which is
    exactly what happened live: a transient DNS failure while RabbitMQ was
    still starting up killed decision-policy's security-signal consumer,
    and every anomaly for the next 17 hours misclassified because the
    corroborating signal was never seen.
    """

    @pytest.fixture
    def consume_recorder(self, monkeypatch):
        made: list[FakeConsumeConnection] = []

        def install(behaviors: list[list[Exception]]):
            queue = list(behaviors)

            def _connect(_params):
                if not queue:
                    raise AssertionError("consume() reconnected more times than the test expected")
                channel = FakeConsumeChannel(queue.pop(0))
                connection = FakeConsumeConnection(channel)
                made.append(connection)
                return connection

            monkeypatch.setattr(pika, "BlockingConnection", _connect)
            monkeypatch.setattr("hypertrace_common.messaging.time.sleep", lambda _seconds: None)
            return made

        return install

    def test_consumes_normally_on_a_healthy_connection(self, consume_recorder):
        made = consume_recorder([[_StopTest()]])
        with pytest.raises(_StopTest):
            RabbitMQClient().consume(queue="q", routing_keys=["a.b"], on_message=lambda _m: None)
        assert len(made) == 1, "a healthy connection should not reconnect"
        channel = made[0].channel()
        assert channel.declared_queues == ["q"]
        assert channel.bound_keys == ["a.b"]
        assert channel.qos_calls == 1
        assert channel.consume_calls == 1

    def test_reconnects_after_the_first_connection_attempt_fails(self, consume_recorder):
        """The actual live bug: a transient failure (DNS not ready yet, in
        production) on the *first* connect must not kill the consumer.
        """
        made = consume_recorder(
            [
                [socket.gaierror("Temporary failure in name resolution")],
                [_StopTest()],
            ]
        )
        with pytest.raises(_StopTest):
            RabbitMQClient().consume(queue="q", routing_keys=["a.b"], on_message=lambda _m: None)
        assert len(made) == 2, "should have retried the connection instead of dying"

    def test_reconnects_after_the_connection_drops_mid_stream(self, consume_recorder):
        made = consume_recorder(
            [
                [pika.exceptions.StreamLostError("Stream connection lost")],
                [_StopTest()],
            ]
        )
        with pytest.raises(_StopTest):
            RabbitMQClient().consume(queue="q", routing_keys=["a.b"], on_message=lambda _m: None)
        assert len(made) == 2, "should have rebuilt the connection and resumed consuming"
        assert made[0].close_calls == 1, "the dead connection should be closed, not leaked"
        # Rebinds on the fresh connection too, not just the first one.
        assert made[1].channel().bound_keys == ["a.b"]

    def test_stops_without_reconnecting_once_close_is_called(self, consume_recorder):
        """Models another thread calling client.close() while consume() is
        blocked in start_consuming(): the resulting connection error must be
        treated as a deliberate stop, not a failure to retry.
        """
        made = consume_recorder([[pika.exceptions.ConnectionClosed(320, "closed by client")]])
        client = RabbitMQClient()
        client._stopping = True  # what close() sets before the error surfaces
        client.consume(queue="q", routing_keys=["a.b"], on_message=lambda _m: None)
        assert len(made) == 1, "a deliberate close must not trigger a reconnect"
