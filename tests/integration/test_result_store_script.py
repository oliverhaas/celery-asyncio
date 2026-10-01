"""Checks the result store script on a real Valkey or Redis, which the unit tests only emulate."""

import asyncio
import logging

import pytest

from celery import states, uuid
from celery.backends.valkey_redis import RedisBackend
from kombu.transport._valkey_redis_compat import get_all_channel_errors


@pytest.fixture
def backend(app):
    if not isinstance(app.backend, RedisBackend):
        pytest.skip("the store script is the Valkey and Redis backend's")
    return app.backend


@pytest.fixture
def task_ids(backend):
    task_ids = [uuid(), uuid()]
    yield task_ids
    for task_id in task_ids:
        backend.delete(backend.get_key_for_task(task_id))


class test_store_result_script:
    def test_a_write_retried_after_it_landed_is_not_logged_as_dropped(self, backend, task_ids, caplog):
        task_id = task_ids[0]
        script = backend._store_result_script
        replies = []

        def reply_lost_once(**kwargs):
            replies.append(script(**kwargs))
            if len(replies) == 1:
                raise backend.connection_errors[0]("reply lost")
            return replies[-1]

        backend.__dict__["_store_result_script"] = reply_lost_once
        with caplog.at_level(logging.ERROR, logger="celery.backends.valkey_redis"):
            backend.store_result(task_id, 1, states.SUCCESS)
            assert "Dropped duplicate result write" not in caplog.text
            backend.store_result(task_id, 2, states.SUCCESS)

        assert replies == [[None], [None], [b"SUCCESS"]]
        assert "Dropped duplicate result write" in caplog.text
        assert backend.get_result(task_id) == 1

    async def test_a_key_of_another_type_fails_only_its_own_write(self, backend, task_ids):
        wrong, right = task_ids
        backend.client.rpush(backend.get_key_for_task(wrong), "x")

        failed, stored = await asyncio.gather(
            backend.astore_result(wrong, 1, states.SUCCESS),
            backend.astore_result(right, 2, states.SUCCESS),
            return_exceptions=True,
        )
        await backend.async_client.aclose(close_connection_pool=True)

        assert isinstance(failed, get_all_channel_errors())
        assert "WRONGTYPE" in str(failed)
        assert stored == 2
        assert backend.get_result(right) == 2
        with pytest.raises(get_all_channel_errors(), match="WRONGTYPE"):
            backend.store_result(wrong, 1, states.SUCCESS)
