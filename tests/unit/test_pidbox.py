"""Tests for kombu.pidbox - Mailbox and Node."""

from unittest.mock import AsyncMock, Mock

import pytest

from kombu.exceptions import InconsistencyError
from kombu.pidbox import Mailbox


class Locked(Exception):
    """Stand-in for the driver's RESOURCE_LOCKED (405) exception."""


def _node(consume_raises=None, locked_errors=(Locked,)):
    """A Node whose Consumer is a stub, so listen() does no I/O."""
    mailbox = Mailbox("testns")
    mailbox.connection = Mock()
    mailbox.connection.resource_locked_errors = locked_errors
    node = mailbox.Node("test.node")
    consumer = Mock()
    consumer.consume = AsyncMock(side_effect=consume_raises)
    node.Consumer = Mock(return_value=consumer)
    return node


def test_mailbox_queues_are_exclusive_by_default():
    # RabbitMQ 4.3.0 refuses transient non-exclusive queues.
    mailbox = Mailbox("testns")
    assert mailbox.queue_exclusive is True
    assert mailbox.get_queue("test.node").exclusive is True
    assert mailbox.get_reply_queue().exclusive is True


def test_asking_only_for_a_durable_mailbox_still_works():
    # The default must not turn into a ValueError for a caller that only
    # asked for durability.
    mailbox = Mailbox("testns", queue_durable=True)
    assert mailbox.queue_exclusive is False
    assert mailbox.get_queue("test.node").durable is True


def test_explicitly_asking_for_both_exclusive_and_durable_is_rejected():
    with pytest.raises(ValueError, match="cannot both be True"):
        Mailbox("testns", queue_durable=True, queue_exclusive=True)


def test_explicitly_asking_for_a_shared_mailbox_is_honoured():
    assert Mailbox("testns", queue_exclusive=False).queue_exclusive is False


async def test_listen_reports_a_locked_pidbox_queue_by_hostname():
    node = _node(Locked("RESOURCE_LOCKED"))

    with pytest.raises(InconsistencyError, match=r"test\.node") as excinfo:
        await node.listen()

    assert isinstance(excinfo.value.__cause__, Locked)


async def test_listen_does_not_swallow_other_channel_errors():
    node = _node(RuntimeError("something else"))

    with pytest.raises(RuntimeError, match="something else"):
        await node.listen()


async def test_listen_on_a_transport_without_exclusive_queues_passes_errors_through():
    node = _node(Locked("RESOURCE_LOCKED"), locked_errors=())

    with pytest.raises(Locked):
        await node.listen()
