"""Checks that a real result backend hands a compressed payload back byte for byte."""

from celery import states, uuid
from celery.backends.base import COMPRESSED_PAYLOAD_MAGIC


def _backend(app, compression, **kwargs):
    conf = app.conf
    previous = conf.get("result_compression")
    conf.result_compression = compression
    try:
        return app.backend.__class__(app=app, url=conf.result_backend, **kwargs)
    finally:
        conf.result_compression = previous


class test_result_compression:
    def test_a_compressed_result_reads_back(self, manager):
        backend = _backend(manager.app, "gzip")
        task_id = uuid()

        backend.store_result(task_id, {"answer": 42}, states.SUCCESS)

        assert backend.get(backend.get_key_for_task(task_id)).startswith(COMPRESSED_PAYLOAD_MAGIC)
        assert backend.get_result(task_id) == {"answer": 42}

    def test_a_compressed_client_reads_what_an_uncompressed_worker_wrote(self, manager):
        result = manager.app.tasks["tests.integration.tasks.add"].delay(4, 4)
        assert result.get(timeout=10) == 8

        reader = _backend(manager.app, "gzip")
        assert reader.get_result(result.id) == 8
