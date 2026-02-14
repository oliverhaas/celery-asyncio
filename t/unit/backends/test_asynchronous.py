import socket
import threading
import time
from unittest.mock import Mock

import pytest
from celery.utils.promises import promise

from celery.backends.asynchronous import BaseResultConsumer
from celery.backends.base import Backend
from celery.utils import cached_property


class test_Drainer:
    """Tests for the default (threading-based) drainer."""

    interval = 0.1  # Check every tenth of a second
    MAX_TIMEOUT = 10  # Specify a max timeout so it doesn't run forever

    @pytest.fixture(autouse=True)
    def setup_drainer(self):
        backend = Backend(self.app)
        consumer = BaseResultConsumer(backend, self.app, backend.accept,
                                      pending_results={},
                                      pending_messages={})
        consumer.drain_events = Mock(side_effect=self.result_consumer_drain_events)
        self.drainer = consumer.drainer

    @cached_property
    def sleep(self):
        from time import sleep
        return sleep

    def result_consumer_drain_events(self, timeout=None):
        time.sleep(timeout)

    def schedule_thread(self, thread):
        t = threading.Thread(target=thread)
        t.start()
        return t

    def teardown_thread(self, thread):
        thread.join()

    def test_drain_checks_on_interval(self):
        p = promise()

        def fulfill_promise_thread():
            self.sleep(self.interval * 2)
            p('done')

        fulfill_thread = self.schedule_thread(fulfill_promise_thread)

        on_interval = Mock()
        for _ in self.drainer.drain_events_until(p,
                                                 on_interval=on_interval,
                                                 interval=self.interval,
                                                 timeout=self.MAX_TIMEOUT):
            pass

        self.teardown_thread(fulfill_thread)

        assert p.ready, 'Should have terminated with promise being ready'
        assert on_interval.call_count < 20, 'Should have limited number of calls to on_interval'

    def test_drain_does_not_block_event_loop(self):
        p = promise()
        liveness_mock = Mock()

        def fulfill_promise_thread():
            self.sleep(self.interval * 2)
            p('done')

        def liveness_thread():
            while 1:
                if p.ready:
                    return
                self.sleep(self.interval / 10)
                liveness_mock()

        fulfill_thread = self.schedule_thread(fulfill_promise_thread)
        liveness_t = self.schedule_thread(liveness_thread)

        on_interval = Mock()
        for _ in self.drainer.drain_events_until(p,
                                                 on_interval=on_interval,
                                                 interval=self.interval,
                                                 timeout=self.MAX_TIMEOUT):
            pass

        self.teardown_thread(fulfill_thread)
        self.teardown_thread(liveness_t)

        assert p.ready, 'Should have terminated with promise being ready'

    def test_drain_timeout(self):
        p = promise()
        on_interval = Mock()

        with pytest.raises(socket.timeout):
            for _ in self.drainer.drain_events_until(p,
                                                     on_interval=on_interval,
                                                     interval=self.interval,
                                                     timeout=self.interval * 5):
                pass

        assert not p.ready, 'Promise should remain un-fulfilled'
        assert on_interval.call_count < 20, 'Should have limited number of calls to on_interval'
