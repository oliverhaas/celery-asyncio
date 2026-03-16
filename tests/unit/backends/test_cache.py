from unittest.mock import Mock, patch

import pytest

from celery import signature, states, uuid
from celery.backends.cache import CacheBackend, backends
from celery.exceptions import ImproperlyConfigured


class SomeClass:
    def __init__(self, data):
        self.data = data


class test_CacheBackend:
    def setup_method(self):
        self.app.conf.result_serializer = "pickle"
        self.tb = CacheBackend(backend="memory://", app=self.app)
        self.tid = uuid()

    def test_no_backend(self):
        self.app.conf.cache_backend = None
        with pytest.raises(ImproperlyConfigured):
            CacheBackend(backend=None, app=self.app)

    def test_memory_client_is_shared(self):
        """This test verifies that memory:// backend state is shared over multiple threads"""
        from threading import Thread

        t = Thread(target=lambda: CacheBackend(backend="memory://", app=self.app).set("test", 12345))
        t.start()
        t.join()
        assert self.tb.client.get("test") == 12345

    def test_mark_as_done(self):
        assert self.tb.get_state(self.tid) == states.PENDING
        assert self.tb.get_result(self.tid) is None

        self.tb.mark_as_done(self.tid, 42)
        assert self.tb.get_state(self.tid) == states.SUCCESS
        assert self.tb.get_result(self.tid) == 42

    def test_is_pickled(self):
        result = {"foo": "baz", "bar": SomeClass(12345)}
        self.tb.mark_as_done(self.tid, result)
        # is serialized properly.
        rindb = self.tb.get_result(self.tid)
        assert rindb.get("foo") == "baz"
        assert rindb.get("bar").data == 12345

    def test_mark_as_failure(self):
        try:
            raise KeyError("foo")
        except KeyError as exception:
            self.tb.mark_as_failure(self.tid, exception)
            assert self.tb.get_state(self.tid) == states.FAILURE
            assert isinstance(self.tb.get_result(self.tid), KeyError)

    def test_apply_chord(self):
        tb = CacheBackend(backend="memory://", app=self.app)
        result_args = (
            uuid(),
            [self.app.AsyncResult(uuid()) for _ in range(3)],
        )
        tb.apply_chord(result_args, None)
        assert self.app.GroupResult.restore(result_args[0], backend=tb) == self.app.GroupResult(*result_args)

    @patch("celery.result.GroupResult.restore")
    def test_on_chord_part_return(self, restore):
        tb = CacheBackend(backend="memory://", app=self.app)

        deps = Mock()
        deps.__len__ = Mock()
        deps.__len__.return_value = 2
        restore.return_value = deps
        task = Mock()
        task.name = "foobarbaz"
        self.app.tasks["foobarbaz"] = task
        task.request.chord = signature(task)

        result_args = (
            uuid(),
            [self.app.AsyncResult(uuid()) for _ in range(3)],
        )
        task.request.group = result_args[0]
        tb.apply_chord(result_args, None)

        deps.join_native.assert_not_called()
        tb.on_chord_part_return(task.request, "SUCCESS", 10)
        deps.join_native.assert_not_called()

        tb.on_chord_part_return(task.request, "SUCCESS", 10)
        deps.join_native.assert_called_with(propagate=True, timeout=3.0)
        deps.delete.assert_called_with()

    def test_mget(self):
        self.tb._set_with_state("foo", 1, states.SUCCESS)
        self.tb._set_with_state("bar", 2, states.SUCCESS)

        assert self.tb.mget(["foo", "bar"]) == {"foo": 1, "bar": 2}

    def test_forget(self):
        self.tb.mark_as_done(self.tid, {"foo": "bar"})
        x = self.app.AsyncResult(self.tid, backend=self.tb)
        x.forget()
        assert x.result is None

    def test_process_cleanup(self):
        self.tb.process_cleanup()

    def test_expires_as_int(self):
        tb = CacheBackend(backend="memory://", expires=10, app=self.app)
        assert tb.expires == 10

    def test_unknown_backend_raises_ImproperlyConfigured(self):
        with pytest.raises(ImproperlyConfigured):
            CacheBackend(backend="unknown://", app=self.app)

    def test_as_uri_no_servers(self):
        assert self.tb.as_uri() == "memory:///"

    def test_backends(self):
        for name, fun in backends.items():
            assert fun()
