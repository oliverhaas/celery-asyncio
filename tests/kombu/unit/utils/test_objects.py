from unittest import mock

from kombu.utils.objects import cached_property


class test_cached_property:
    def test_deleting(self):
        class X:
            xx = False

            @cached_property
            def foo(self):
                return 42

            @foo.deleter
            def foo(self, value):
                self.xx = value

        x = X()
        del x.foo
        assert not x.xx
        x.__dict__["foo"] = "here"
        del x.foo
        assert x.xx == "here"

    def test_when_access_from_class(self):
        class X:
            xx = None

            @cached_property
            def foo(self):
                return 42

            @foo.setter
            def foo(self, value):
                self.xx = 10

        desc = X.__dict__["foo"]
        assert X.foo is desc

        assert desc.__get__(None, X) is desc
        assert desc.setter(1)

        x = X()
        x.foo = 30
        assert x.xx == 10

        del x.foo

    def test_locks_on_access(self):
        class X:
            @cached_property
            def foo(self):
                return 42

        x = X()

        # Getting does not take the lock: functools.cached_property dropped
        # its own in 3.12, and the documented consequence is that a racing
        # first access may compute the value twice, not that it corrupts.
        with mock.patch.object(X.foo, "lock") as mock_lock:
            assert x.foo == 42
        mock_lock.__enter__.assert_not_called()

        # Setting a value does acquire the lock.
        with mock.patch.object(X.foo, "lock") as mock_lock:
            x.foo = 314
        assert x.foo == 314
        mock_lock.__enter__.assert_called_once()
        mock_lock.__exit__.assert_called_once()

        # .. as does clearing the cached value to recompute it.
        with mock.patch.object(X.foo, "lock") as mock_lock:
            del x.foo
        assert x.foo == 42
        mock_lock.__enter__.assert_called_once()
        mock_lock.__exit__.assert_called_once()
