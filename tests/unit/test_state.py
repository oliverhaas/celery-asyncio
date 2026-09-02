from unittest.mock import patch

import pytest

from celery import _state


class test_app_or_default_trace:
    def test_returns_the_thread_local_app(self):
        sentinel = object()
        with patch.object(_state._tls, "current_app", sentinel):
            assert _state._app_or_default_trace() is sentinel

    def test_raises_in_the_main_process_without_a_current_app(self):
        with patch.object(_state._tls, "current_app", None):
            with pytest.raises(Exception, match="DEFAULT APP"):
                _state._app_or_default_trace()

    def test_returns_the_given_app_unchanged(self):
        sentinel = object()
        assert _state._app_or_default_trace(sentinel) is sentinel
