"""Tests for kombu.message - async Message."""

from unittest.mock import Mock

import pytest

from kombu.exceptions import MessageStateError
from kombu.message import Message


class test_Message:
    """Tests for Message class."""

    def test_init(self):
        m = Message(body=b"hello", delivery_tag="tag1")
        assert m.body == b"hello"
        assert m.delivery_tag == "tag1"
        assert m._state == "RECEIVED"
        assert not m.acknowledged

    def test_init_defaults(self):
        m = Message()
        assert m.body is None
        assert m.delivery_tag is None
        assert m.content_type is None
        assert m.headers == {}
        assert m.properties == {}
        assert m.delivery_info == {}

    def test_repr(self):
        m = Message(body=b"hello", delivery_tag="tag1")
        r = repr(m)
        assert "RECEIVED" in r
        assert "tag1" in r

    def test_decode_caches(self):
        m = Message(body=b"hello")
        m._decode = Mock(return_value="decoded")
        result1 = m.decode()
        result2 = m.decode()
        assert result1 == "decoded"
        assert result2 == "decoded"
        m._decode.assert_called_once()  # Only called once, then cached

    def test_payload_property(self):
        m = Message(body=b"test")
        m._decoded_cache = "cached_value"
        assert m.payload == "cached_value"

    def test_acknowledged_states(self):
        m = Message(body=b"test")
        assert not m.acknowledged
        m._state = "ACK"
        assert m.acknowledged
        m._state = "REJECTED"
        assert m.acknowledged
        m._state = "REQUEUED"
        assert m.acknowledged

    async def test_ack(self, mock_channel):
        m = Message(body=b"test", delivery_tag="tag1", channel=mock_channel)
        await m.ack()
        assert m._state == "ACK"
        assert m.acknowledged
        assert any(c[0] == "basic_ack" for c in mock_channel.calls)

    async def test_ack_no_channel(self):
        m = Message(body=b"test")
        with pytest.raises(MessageStateError, match="does not have a receiving channel"):
            await m.ack()

    async def test_ack_already_acked(self, mock_channel):
        m = Message(body=b"test", delivery_tag="tag1", channel=mock_channel)
        await m.ack()
        with pytest.raises(MessageStateError, match="already acknowledged"):
            await m.ack()

    async def test_reject(self, mock_channel):
        m = Message(body=b"test", delivery_tag="tag1", channel=mock_channel)
        await m.reject()
        assert m._state == "REJECTED"
        assert m.acknowledged

    async def test_reject_no_channel(self):
        m = Message(body=b"test")
        with pytest.raises(MessageStateError, match="does not have a receiving channel"):
            await m.reject()

    async def test_reject_already_acked(self, mock_channel):
        m = Message(body=b"test", delivery_tag="tag1", channel=mock_channel)
        await m.ack()
        with pytest.raises(MessageStateError, match="already acknowledged"):
            await m.reject()

    async def test_requeue(self, mock_channel):
        m = Message(body=b"test", delivery_tag="tag1", channel=mock_channel)
        await m.requeue()
        assert m._state == "REQUEUED"
        assert m.acknowledged
        # Should call basic_reject with requeue=True
        reject_calls = [c for c in mock_channel.calls if c[0] == "basic_reject"]
        assert len(reject_calls) == 1
        assert reject_calls[0][1][1] is True  # requeue=True

    async def test_requeue_no_channel(self):
        m = Message(body=b"test")
        with pytest.raises(MessageStateError, match="does not have a receiving channel"):
            await m.requeue()

    async def test_ack_log_error(self, mock_channel):
        m = Message(body=b"test", delivery_tag="tag1", channel=mock_channel)
        logger = Mock()
        await m.ack_log_error(logger, (Exception,))
        assert m._state == "ACK"

    async def test_ack_log_error_catches(self, mock_channel):
        m = Message(body=b"test", delivery_tag="tag1", channel=mock_channel)
        await m.ack()  # Already acked
        m._state = "ACK"

        # Now create a fresh message but make channel.basic_ack raise
        m2 = Message(body=b"test", delivery_tag="tag2", channel=mock_channel)
        # Override to raise
        original = mock_channel.basic_ack

        async def failing_ack(*args, **kwargs):
            raise RuntimeError("ack failed")

        mock_channel.basic_ack = failing_ack
        logger = Mock()
        await m2.ack_log_error(logger, (RuntimeError,))
        logger.critical.assert_called()
        mock_channel.basic_ack = original

    async def test_reject_log_error(self, mock_channel):
        m = Message(body=b"test", delivery_tag="tag1", channel=mock_channel)
        logger = Mock()
        await m.reject_log_error(logger, (Exception,), requeue=True)
        assert m._state == "REJECTED"

    def test_reraise_error(self):
        m = Message(body=b"test")
        try:
            raise KeyError("test_error")
        except KeyError:
            import sys

            m.errors = [sys.exc_info()]

        with pytest.raises(KeyError, match="test_error"):
            m._reraise_error()

    def test_reraise_error_with_callback(self):
        m = Message(body=b"test")
        try:
            raise KeyError("test_error")
        except KeyError:
            import sys

            m.errors = [sys.exc_info()]

        callback = Mock()
        m._reraise_error(callback=callback)
        callback.assert_called_once()

    async def test_no_ack_consumer(self, mock_channel):
        """Messages from no_ack consumers skip ack."""
        mock_channel.no_ack_consumers = {"my_tag"}
        m = Message(
            body=b"test",
            delivery_tag="tag1",
            channel=mock_channel,
            delivery_info={"consumer_tag": "my_tag"},
        )
        await m.ack()  # Should not raise, silently skipped
        # Message state should still be RECEIVED since ack was skipped
        assert m._state == "RECEIVED"
