"""Tests for celery.utils.eventloop - the re-export of kombu's loop runner."""

from celery.utils import eventloop as celery_eventloop
from kombu.utils import eventloop as kombu_eventloop


class test_reexport:
    def test_names_are_the_same_objects(self):
        # Both packages must share one runner: a connection opened by kombu and
        # used by celery has to stay on the loop that opened it.
        for name in celery_eventloop.__all__:
            assert getattr(celery_eventloop, name) is getattr(kombu_eventloop, name)
