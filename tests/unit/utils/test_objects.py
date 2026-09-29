from contextlib import contextmanager

import pytest

from celery.utils.objects import Bunch, FallbackContext


class test_Bunch:
    def test(self):
        x = Bunch(foo="foo", bar=2)
        assert x.foo == "foo"
        assert x.bar == 2


class test_FallbackContext:
    @pytest.mark.parametrize("resource", [None, "resource"])
    def test_exits_the_fallback_after_the_body(self, resource):
        events = []

        @contextmanager
        def fallback():
            events.append("enter")
            try:
                yield resource
            finally:
                events.append("exit")

        with FallbackContext(None, fallback) as value:
            events.append(("body", value))

        assert events == ["enter", ("body", resource), "exit"]
