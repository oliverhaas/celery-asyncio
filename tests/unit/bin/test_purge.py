from itertools import chain
from unittest.mock import patch

from click.testing import CliRunner

from celery.bin.celery import celery

from .proj.app import app as proj_app

_GLOBAL_OPTIONS = ["-A", "tests.unit.bin.proj.app"]


class ChannelError(Exception):
    pass


class FakeChannel:
    """Channel that the broker closes on the first error, as AMQP does."""

    def __init__(self, missing):
        self._missing = missing
        self._closed = False
        self.purged = []

    async def queue_purge(self, queue):
        if self._closed:
            raise ChannelError("CHANNEL_ERROR - expected 'channel.open'")
        if queue in self._missing:
            self._closed = True
            raise ChannelError(f"NOT_FOUND - no queue {queue!r}")
        self.purged.append(queue)
        return 2


class FakeConnection:
    """Connection whose channel dies on a queue the broker does not have."""

    channel_errors = (ChannelError,)

    def __init__(self, missing=()):
        self._missing = set(missing)
        self.channels = []

    async def default_channel(self):
        return await self.channel()

    async def channel(self):
        channel = FakeChannel(self._missing)
        self.channels.append(channel)
        return channel

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False


def run_purge(cli_runner, connection, argv):
    with patch.object(proj_app, "connection_for_write", return_value=connection):
        return cli_runner.invoke(celery, [*_GLOBAL_OPTIONS, "purge", "-f", *argv], catch_exceptions=False)


def test_purge_reports_the_number_of_messages(cli_runner: CliRunner):
    connection = FakeConnection()

    res = run_purge(cli_runner, connection, ["-Q", "one,two"])

    assert res.exit_code == 0, (res, res.output)
    assert "Purged 4 messages from 2 known task queues." in res.stdout
    assert len(connection.channels) == 1
    assert sorted(connection.channels[0].purged) == ["one", "two"]


def test_a_queue_the_broker_does_not_have_stops_neither_the_others_nor_the_report(cli_runner: CliRunner):
    # The broker closes the channel it raised the error on, so every queue
    # after the missing one used to fail as well, without a word about it.
    connection = FakeConnection(missing={"one"})

    res = run_purge(cli_runner, connection, ["-Q", "one,two,three"])

    assert res.exit_code == 0, (res, res.output)
    assert list(chain.from_iterable(channel.purged for channel in connection.channels)) == ["three", "two"]
    assert "Cannot purge one: NOT_FOUND - no queue 'one'" in res.stderr
    assert "Purged 4 messages from 3 known task queues." in res.stdout
