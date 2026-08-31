"""Regression tests for the RabbitMQ publish path.

These cover the stale-connection bug found in live testing: the api-bff only
publishes when a human clicks approve or rollback, so its AMQP connection
sits idle for long stretches and gets reaped by the broker. pika only
discovers this on the next write — `connection.is_closed` still reads
False — so the reconnect check in _ensure_channel never fired and the first
click after an idle period returned a 500.
"""

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
