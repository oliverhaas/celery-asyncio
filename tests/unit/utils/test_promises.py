import gc
import logging

import pytest

from celery.utils.promises import barrier, promise, starpromise


class Recorder:
    def __init__(self):
        self.calls = []

    def record(self, value=None):
        self.calls.append(value)
        return value


class test_promise:
    def test_calls_the_function_and_fulfils(self):
        p = promise(lambda x: x * 2)

        assert p(21) == 42
        assert p.ready
        assert p._value == 42

    def test_weak_promise_calls_a_bound_method(self):
        recorder = Recorder()
        p = promise(recorder.record, weak=True)

        assert p("x") == "x"
        assert recorder.calls == ["x"]

    def test_weak_promise_forgets_a_bound_method_once_its_object_dies(self):
        recorder = Recorder()
        p = promise(recorder.record, weak=True)
        calls = recorder.calls

        del recorder
        gc.collect()
        p("x")

        assert calls == []

    def test_weak_promise_calls_a_plain_function(self):
        calls = []

        def record(value):
            calls.append(value)

        p = promise(record, weak=True)
        p("x")

        assert calls == ["x"]

    def test_default_args_are_used_when_called_without_any(self):
        seen = []
        p = promise(None, "payload")
        p.then(seen.append)

        assert p() == "payload"
        assert seen == ["payload"]

    def test_call_arguments_win_over_the_defaults(self):
        p = promise(None, "payload")

        assert p("override") == "override"

    def test_then_after_fulfilment_calls_back_immediately(self):
        seen = []
        p = promise(None, "payload")
        p()

        p.then(seen.append)

        assert seen == ["payload"]

    def test_a_failing_callback_is_logged_and_the_others_still_run(self, caplog):
        seen = []
        p = promise()
        p.then(lambda value: 1 / 0)
        p.then(seen.append)

        with caplog.at_level(logging.ERROR, logger="celery.utils.promises"):
            p("payload")

        assert seen == ["payload"]
        assert "ZeroDivisionError" in caplog.text

    def test_a_failing_callback_goes_to_its_own_error_handler(self, caplog):
        errors = []
        p = promise()
        p.then(lambda value: 1 / 0, errors.append)

        with caplog.at_level(logging.ERROR, logger="celery.utils.promises"):
            p("payload")

        assert [type(exc) for exc in errors] == [ZeroDivisionError]
        assert caplog.text == ""

    def test_throw_reports_to_on_error_and_reraises(self):
        errors = []
        p = promise(on_error=errors.append)
        exc = KeyError("boom")

        with pytest.raises(KeyError):
            p.throw(exc)

        assert errors == [exc]

    def test_starpromise_unpacks_a_single_iterable_argument(self):
        p = starpromise(lambda x, y: x + y)

        assert p((2, 3)) == 5


class test_barrier:
    def test_does_not_fire_before_every_result_arrived(self):
        seen = []
        b = barrier()
        b.then(lambda: seen.append("fired"))
        b.size = 2

        b()
        b.finalize()

        assert not b.ready
        assert seen == []

    def test_fires_once_the_last_result_arrives(self):
        seen = []
        b = barrier()
        b.then(lambda: seen.append("fired"))
        b.size = 2
        b.finalize()

        b()
        assert seen == []
        b()

        assert b.ready
        assert seen == ["fired"]

    def test_finalize_does_not_fire_a_barrier_that_is_still_waiting(self):
        seen = []
        b = barrier()
        b.then(lambda: seen.append("fired"))
        b.size = 3

        b.finalize()

        assert not b.ready
        assert seen == []

    def test_does_not_fire_before_it_is_finalized(self):
        seen = []
        b = barrier()
        b.then(lambda: seen.append("fired"))
        b.size = 1

        b()

        assert not b.ready
        assert seen == []

    def test_add_subscribes_to_the_result(self):
        seen = []
        b = barrier()
        b.then(lambda: seen.append("fired"))
        p = promise()
        b.add(p)
        b.finalize()

        assert seen == []
        p("done")

        assert b.ready
        assert seen == ["fired"]

    def test_a_complete_result_list_fires_the_barrier(self):
        seen = []
        done = promise()
        done("already done")

        b = barrier([done])
        b.then(lambda: seen.append("fired"))

        assert b.ready
        assert seen == ["fired"]

    def test_add_reopens_a_barrier_that_already_fired(self):
        seen = []
        done = promise()
        done("already done")
        b = barrier([done])
        b.then(lambda: seen.append("fired"))

        pending = promise()
        b.add(pending)

        assert not b.ready
        pending("done")
        assert b.ready
        assert seen == ["fired", "fired"]

    def test_cancel_keeps_the_barrier_from_firing(self):
        seen = []
        b = barrier()
        b.then(lambda: seen.append("fired"))
        b.size = 1
        b.finalize()

        b.cancel()
        b()

        assert not b.ready
        assert seen == []
