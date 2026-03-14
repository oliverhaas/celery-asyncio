import os
import re
from datetime import UTC, datetime, timedelta
from time import sleep
from unittest.mock import ANY

import pytest

from .tasks import add, sleeping

_flaky = pytest.mark.flaky(reruns=5, reruns_delay=2)
_timeout = pytest.mark.timeout(timeout=300)


def flaky(fn):
    return _timeout(_flaky(fn))


@pytest.fixture
def inspect(manager):
    return manager.app.control.inspect()


def _get_nodename(ret):
    """Get a node name from an inspect response.

    When multiple workers are connected (e.g. from concurrent test sessions),
    just pick the first one.
    """
    assert len(ret) >= 1
    return next(iter(ret))


class test_Inspect:
    """Integration tests to app.control.inspect() API"""

    @flaky
    def test_ping(self, inspect):
        """Tests pinging the worker"""
        ret = inspect.ping()
        nodename = _get_nodename(ret)
        assert ret[nodename] == {"ok": "pong"}

    @flaky
    def test_clock(self, inspect):
        """Tests getting clock information from worker"""
        ret = inspect.clock()
        nodename = _get_nodename(ret)
        assert ret[nodename]["clock"] > 0

    @flaky
    def test_registered(self, inspect):
        """Tests listing registered tasks"""
        ret = inspect.registered()
        nodename = _get_nodename(ret)
        assert len(ret[nodename]) > 0
        for task_name in ret[nodename]:
            assert isinstance(task_name, str)

        ret = inspect.registered("name")
        nodename = _get_nodename(ret)
        for task_info in ret[nodename]:
            # task_info is in form 'TASK_NAME [name=TASK_NAME]'
            assert re.fullmatch(r"\S+ \[name=\S+\]", task_info)

    @flaky
    def test_active_queues(self, inspect):
        """Tests listing active queues"""
        ret = inspect.active_queues()
        nodename = _get_nodename(ret)
        queues = ret[nodename]
        assert len(queues) == 1
        q = queues[0]
        assert q["name"] == "celery"
        assert q["routing_key"] == "celery"
        assert q["durable"] is True
        assert q["exclusive"] is False
        assert q["auto_delete"] is False
        assert q["exchange"]["name"] == "celery"
        assert q["exchange"]["type"] == "direct"

    @flaky
    def test_active(self, inspect):
        """Tests listing active tasks"""
        res = sleeping.delay(5)
        sleep(1)
        ret = inspect.active()
        nodename = _get_nodename(ret)
        tasks = ret[nodename]
        assert len(tasks) == 1
        task = tasks[0]
        assert task["id"] == res.task_id
        assert task["name"] == "tests.integration.tasks.sleeping"
        assert task["args"] == [5]
        assert task["kwargs"] == {}
        assert task["acknowledged"] is True
        assert task["time_start"] is not None
        assert task["delivery_info"]["routing_key"] == "celery"

    @flaky
    def test_scheduled(self, inspect):
        """Tests listing scheduled tasks"""
        exec_time = datetime.now(UTC) + timedelta(seconds=5)
        res = add.apply_async([1, 2], {"z": 3}, eta=exec_time)
        ret = inspect.scheduled()
        nodename = _get_nodename(ret)
        scheduled = ret[nodename]
        assert len(scheduled) == 1
        entry = scheduled[0]
        assert entry["eta"].startswith(exec_time.strftime("%Y-%m-%dT%H:%M:%S"))
        assert entry["request"]["id"] == res.task_id
        assert entry["request"]["name"] == "tests.integration.tasks.add"
        assert entry["request"]["args"] == [1, 2]
        assert entry["request"]["kwargs"] == {"z": 3}
        assert entry["request"]["acknowledged"] is False
        assert entry["request"]["time_start"] is None

    @flaky
    def test_query_task(self, inspect):
        """Task that does not exist or is finished"""
        ret = inspect.query_task("d08b257e-a7f1-4b92-9fea-be911441cb2a")
        nodename = _get_nodename(ret)
        assert ret[nodename] == {}

        # Task in progress
        res = sleeping.delay(5)
        sleep(1)
        ret = inspect.query_task(res.task_id)
        nodename = _get_nodename(ret)
        result = ret[nodename]
        assert res.task_id in result
        state, info = result[res.task_id]
        assert state == "active"
        assert info["id"] == res.task_id
        assert info["name"] == "tests.integration.tasks.sleeping"
        assert info["acknowledged"] is True

    @flaky
    def test_stats(self, inspect):
        """tests fetching statistics"""
        ret = inspect.stats()
        nodename = _get_nodename(ret)
        stats = ret[nodename]
        assert stats["pool"]["max-concurrency"] == 1
        assert stats["uptime"] > 0
        # worker is running in the same process as separate thread
        assert stats["pid"] == os.getpid()

    @flaky
    def test_report(self, inspect):
        """Tests fetching report"""
        ret = inspect.report()
        nodename = _get_nodename(ret)
        assert ret[nodename] == {"ok": ANY}

    @flaky
    def test_revoked(self, inspect):
        """Testing revoking of task"""
        # Fill the queue with tasks to fill the queue
        for _ in range(4):
            sleeping.delay(2)
        # Execute task and revoke it
        result = add.apply_async((1, 1))
        result.revoke()
        ret = inspect.revoked()
        nodename = _get_nodename(ret)
        assert result.task_id in ret[nodename]

    @flaky
    def test_conf(self, inspect):
        """Tests getting configuration"""
        ret = inspect.conf()
        nodename = _get_nodename(ret)
        assert ret[nodename]["worker_hijack_root_logger"] == ANY
        assert ret[nodename]["worker_log_color"] == ANY
        assert ret[nodename]["accept_content"] == ANY
        assert ret[nodename]["enable_utc"] == ANY
        assert ret[nodename]["timezone"] == ANY
        assert ret[nodename]["broker_url"] == ANY
        assert ret[nodename]["result_backend"] == ANY
        assert ret[nodename]["broker_heartbeat"] == ANY
        assert ret[nodename]["deprecated_settings"] == ANY
        assert ret[nodename]["include"] == ANY
