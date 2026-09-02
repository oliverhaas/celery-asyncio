"""Integration tests for kombu.pidbox against live brokers."""

import os
import uuid

import pytest

from kombu import Connection
from kombu.pidbox import Mailbox

pytestmark = pytest.mark.asyncio(loop_scope="function")

AMQP_URL = os.environ.get("KOMBU_TEST_AMQP_URL", "amqp://guest:guest@localhost:5672//")
REDIS_URL = os.environ.get("KOMBU_TEST_REDIS_URL", "redis://localhost:6379/15")

URLS = {"amqp": AMQP_URL, "redis": REDIS_URL}


@pytest.fixture(params=sorted(URLS), ids=sorted(URLS))
async def connection(request):
    conn = Connection(URLS[request.param])
    await conn.connect()
    yield conn
    await conn.close()


async def _mailbox(connection):
    return Mailbox(f"it{uuid.uuid4().hex}", type="fanout", accept=["json"])(connection)


async def _listen(mailbox, hostname, handlers):
    channel = await mailbox.connection.default_channel()
    node = mailbox.Node(hostname, state=hostname, channel=channel, handlers=handlers)
    await node.listen(channel=channel)


class TestMailboxCall:
    async def test_the_reply_of_an_async_handler_reaches_the_caller(self, connection):
        async def ping(state):
            return f"pong from {state}"

        mailbox = await _mailbox(connection)
        await _listen(mailbox, "worker-a1", {"ping": ping})

        replies = await mailbox.multi_call("ping", timeout=2)

        assert replies == [{"worker-a1": "pong from worker-a1"}]

    async def test_a_pattern_decides_which_nodes_answer(self, connection):
        mailbox = await _mailbox(connection)
        await _listen(mailbox, "worker-a1", {"ping": lambda state: state})
        await _listen(mailbox, "worker-b1", {"ping": lambda state: state})

        replies = await mailbox._broadcast(
            "ping",
            {},
            reply=True,
            timeout=2,
            pattern="worker-a*",
            matcher="glob",
        )

        assert replies == [{"worker-a1": "worker-a1"}]
