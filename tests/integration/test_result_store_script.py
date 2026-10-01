"""Checks the result store script on a real Valkey or Redis, which the unit tests only emulate."""

import logging

import pytest

from celery import states, uuid
from celery.backends.valkey_redis import RedisBackend


@pytest.fixture
def backend(app):
    if not isinstance(app.backend, RedisBackend):
        pytest.skip("the store script is the Valkey and Redis backend's")
    return app.backend


@pytest.fixture
def task_id(backend):
    task_id = uuid()
    yield task_id
    backend.delete(backend.get_key_for_task(task_id))


class test_store_result_script:
    def test_a_write_retried_after_it_landed_is_not_logged_as_dropped(self, backend, task_id, caplog):
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
