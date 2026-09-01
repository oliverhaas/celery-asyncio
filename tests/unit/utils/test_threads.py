import contextvars
import threading
from unittest.mock import patch

import pytest
from asgiref.sync import sync_to_async

from celery.utils.threads import Local, LocalManager, LocalStack, bgThread
from tests.unit import conftest


class test_bgThread:
    def test_crash(self):

        class T(bgThread):
            def body(self):
                raise KeyError()

        with patch("os._exit") as _exit, conftest.stdouts():
            _exit.side_effect = ValueError()
            t = T()
            with pytest.raises(ValueError):
                t.run()
            _exit.assert_called_with(1)

    def test_interface(self):
        x = bgThread()
        with pytest.raises(NotImplementedError):
            x.body()


class test_Local:
    def test_iter(self):
        x = Local()
        x.foo = "bar"
        ident = x.__ident_func__()
        assert (ident, {"foo": "bar"}) in list(iter(x))

        delattr(x, "foo")
        assert (ident, {"foo": "bar"}) not in list(iter(x))
        with pytest.raises(AttributeError):
            delattr(x, "foo")

        assert x(lambda: "foo") is not None


class test_LocalStack:
    def test_stack(self):
        x = LocalStack()
        x.push(["foo"])
        x.push(["bar"])
        assert x.top == ["bar"]
        assert len(x) == 2
        x.pop()
        assert x.top == ["foo"]
        x.pop()
        assert x.top is None

    def test_pop_empty(self):
        assert LocalStack().pop() is None

    def test_isolated_in_a_fresh_context(self):
        # Whether a new thread starts from an empty context or a copy of its
        # parent's is Python's call, not this class's: free-threaded builds
        # default sys.flags.thread_inherit_context to 1 and the rest to 0. What
        # the stack has to guarantee is that an empty context is an empty
        # stack, and that a thread pushing onto its own copy cannot be seen by
        # the thread that spawned it.
        x = LocalStack()
        x.push("main")
        seen = []

        def run():
            seen.append(x.top)
            x.push("thread")

        thread = threading.Thread(target=run, context=contextvars.Context())
        thread.start()
        thread.join()
        assert seen == [None]
        assert x.top == "main"

    async def test_visible_from_sync_to_async(self):
        # The async worker runs sync task bodies in a thread through
        # sync_to_async, which copies the context but not thread locals. The
        # body has to see the request the trace pushed for it.
        x = LocalStack()
        x.push("request")
        assert await sync_to_async(lambda: x.top, thread_sensitive=False)() == "request"


class test_LocalManager:
    def test_init(self):
        x = LocalManager()
        assert x.locals == []
        assert x.ident_func

        def ident():
            return 1

        loc = Local()
        x = LocalManager([loc], ident_func=ident)
        assert x.locals == [loc]
        x = LocalManager(loc, ident_func=ident)
        assert x.locals == [loc]
        assert x.ident_func is ident
        assert x.locals[0].__ident_func__ is ident
        assert x.get_ident() == 1

        with patch("celery.utils.threads.release_local") as release:
            x.cleanup()
            release.assert_called_with(loc)

        assert repr(x)
