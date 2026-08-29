from unittest.mock import ANY, AsyncMock, Mock, PropertyMock, patch
from uuid import uuid4

import pytest
from kombu.exceptions import EncodeError

from celery import group, signals, states, uuid
from celery.app.task import Context
from celery.app.trace import (
    TraceInfo,
    build_async_tracer,
    build_tracer,
    fast_trace_task,
    get_actual_ignore_result,
    get_log_policy,
    get_task_name,
    log_policy_expected,
    log_policy_ignore,
    log_policy_internal,
    log_policy_reject,
    log_policy_unexpected,
    reset_worker_optimizations,
    setup_worker_optimizations,
    trace_task,
    trace_task_ret,
    traceback_clear,
)
from celery.backends.base import BaseBackend
from celery.backends.cache import CacheBackend
from celery.exceptions import BackendGetMetaError, ExceptionInfo, Ignore, InvalidTaskError, Reject, Retry
from celery.states import PENDING
from celery.worker.state import successful_requests


def trace(app, task, args=(), kwargs=None, propagate=False, eager=True, request=None, task_id="id-1", **opts):
    if kwargs is None:
        kwargs = {}
    t = build_tracer(task.name, task, eager=eager, propagate=propagate, app=app, **opts)
    ret = t(task_id, args, kwargs, request)
    return ret.retval, ret.info


async def atrace(app, task, args=(), kwargs=None, propagate=False, eager=True, request=None, task_id="id-1", **opts):
    """``trace``, but through the async tracer the aio pool actually runs."""
    if kwargs is None:
        kwargs = {}
    t = build_async_tracer(task.name, task, eager=eager, propagate=propagate, app=app, **opts)
    ret = await t(task_id, args, kwargs, request)
    return ret.retval, ret.info


class TraceCase:
    def setup_method(self):
        @self.app.task(shared=False)
        def add(x, y):
            return x + y

        self.add = add

        @self.app.task(shared=False, ignore_result=True)
        def add_cast(x, y):
            return x + y

        self.add_cast = add_cast

        @self.app.task(shared=False)
        def raises(exc):
            raise exc

        self.raises = raises

    def trace(self, *args, **kwargs):
        return trace(self.app, *args, **kwargs)


class test_trace(TraceCase):
    def teardown_method(self):
        # successful_requests is module-global, and the dedup fast path now
        # writes to it, so a test that takes that path would leak its id.
        successful_requests.clear()
        self.app.conf.worker_deduplicate_successful_tasks = False

    def test_trace_successful(self):
        retval, info = self.trace(self.add, (2, 2), {})
        assert info is None
        assert retval == 4

    def test_trace_before_start(self):
        @self.app.task(shared=False, before_start=Mock())
        def add_with_before_start(x, y):
            return x + y

        self.trace(add_with_before_start, (2, 2), {})
        add_with_before_start.before_start.assert_called()

    def test_trace_on_success(self):
        @self.app.task(shared=False, on_success=Mock())
        def add_with_success(x, y):
            return x + y

        self.trace(add_with_success, (2, 2), {})
        add_with_success.on_success.assert_called()

    def test_get_actual_ignore_result(self):
        # Context defaults ignore_result to False at class level, so only an
        # instance attribute counts as the caller having said anything.
        self.add.ignore_result = True

        assert get_actual_ignore_result(self.add, None) is True
        assert get_actual_ignore_result(self.add, Context({})) is True
        assert get_actual_ignore_result(self.add, Context({"ignore_result": False})) is False
        assert get_actual_ignore_result(self.add, Context({"ignore_result": True})) is True

        self.add.ignore_result = False
        assert get_actual_ignore_result(self.add, Context({})) is False
        assert get_actual_ignore_result(self.add, Context({"ignore_result": True})) is True

    def test_trace_request_ignore_result_beats_the_task(self):
        # The tracer is built once and reused for every message, so these used
        # to be frozen from the task definition at build time.
        self.add.ignore_result = True
        self.add.backend = Mock(name="backend")

        self.trace(self.add, (2, 2), {}, eager=False, request={"ignore_result": False})

        assert self.add.backend.mark_as_done.called

    def test_get_log_policy(self):
        einfo = Mock(name="einfo")
        einfo.internal = False
        assert get_log_policy(self.add, einfo, Reject()) is log_policy_reject
        assert get_log_policy(self.add, einfo, Ignore()) is log_policy_ignore

        self.add.throws = (TypeError,)
        assert get_log_policy(self.add, einfo, KeyError()) is log_policy_unexpected
        assert get_log_policy(self.add, einfo, TypeError()) is log_policy_expected

        einfo2 = Mock(name="einfo2")
        einfo2.internal = True
        assert get_log_policy(self.add, einfo2, KeyError()) is log_policy_internal

    def test_get_task_name(self):
        assert get_task_name(Context({}), "default") == "default"
        assert get_task_name(Context({"shadow": None}), "default") == "default"
        assert get_task_name(Context({"shadow": ""}), "default") == "default"
        assert get_task_name(Context({"shadow": "test"}), "default") == "test"

    def test_trace_after_return(self):
        @self.app.task(shared=False, after_return=Mock())
        def add_with_after_return(x, y):
            return x + y

        self.trace(add_with_after_return, (2, 2), {})
        add_with_after_return.after_return.assert_called()

    def test_with_prerun_receivers(self):
        on_prerun = Mock()
        signals.task_prerun.connect(on_prerun)
        try:
            self.trace(self.add, (2, 2), {})
            on_prerun.assert_called()
        finally:
            signals.task_prerun.receivers[:] = []

    def test_with_postrun_receivers(self):
        on_postrun = Mock()
        signals.task_postrun.connect(on_postrun)
        try:
            self.trace(self.add, (2, 2), {})
            on_postrun.assert_called()
        finally:
            signals.task_postrun.receivers[:] = []

    def test_with_success_receivers(self):
        on_success = Mock()
        signals.task_success.connect(on_success)
        try:
            self.trace(self.add, (2, 2), {})
            on_success.assert_called()
        finally:
            signals.task_success.receivers[:] = []

    def test_when_chord_part(self):
        @self.app.task(shared=False)
        def add(x, y):
            return x + y

        add.backend = Mock()

        request = {"chord": uuid()}
        self.trace(add, (2, 2), {}, request=request)
        add.backend.mark_as_done.assert_called()
        args, kwargs = add.backend.mark_as_done.call_args
        assert args[0] == "id-1"
        assert args[1] == 4
        assert args[2].chord == request["chord"]
        assert not args[3]

    def test_when_backend_cleanup_raises(self):
        @self.app.task(shared=False)
        def add(x, y):
            return x + y

        add.backend = Mock(name="backend")
        add.backend.process_cleanup.side_effect = KeyError()
        self.trace(add, (2, 2), {}, eager=False)
        add.backend.process_cleanup.assert_called_with()
        add.backend.process_cleanup.side_effect = MemoryError()
        with pytest.raises(MemoryError):
            self.trace(add, (2, 2), {}, eager=False)

    def test_eager_task_does_not_store_result_even_if_not_ignore_result(self):
        @self.app.task(shared=False)
        def add(x, y):
            return x + y

        add.backend = Mock(name="backend")
        add.ignore_result = False

        self.trace(add, (2, 2), {}, eager=True)

        add.backend.mark_as_done.assert_called_once_with(
            "id-1",  # task_id
            4,  # result
            ANY,  # request
            False,  # store_result
        )

    def test_eager_task_does_not_call_store_result(self):
        @self.app.task(shared=False)
        def add(x, y):
            return x + y

        backend = BaseBackend(app=self.app)
        backend.store_result = Mock()
        add.backend = backend
        add.ignore_result = False

        self.trace(add, (2, 2), {}, eager=True)

        add.backend.store_result.assert_not_called()

    def test_eager_task_will_store_result_if_proper_setting_is_set(self):
        @self.app.task(shared=False)
        def add(x, y):
            return x + y

        add.backend = Mock(name="backend")
        add.store_eager_result = True
        add.ignore_result = False

        self.trace(add, (2, 2), {}, eager=True)

        add.backend.mark_as_done.assert_called_once_with(
            "id-1",  # task_id
            4,  # result
            ANY,  # request
            True,  # store_result
        )

    def test_eager_task_with_setting_will_call_store_result(self):
        @self.app.task(shared=False)
        def add(x, y):
            return x + y

        backend = BaseBackend(app=self.app)
        backend.store_result = Mock()
        add.backend = backend
        add.store_eager_result = True
        add.ignore_result = False

        self.trace(add, (2, 2), {}, eager=True)

        add.backend.store_result.assert_called_once_with("id-1", 4, states.SUCCESS, request=ANY)

    def test_when_backend_raises_exception(self):
        @self.app.task(shared=False)
        def add(x, y):
            return x + y

        add.backend = Mock(name="backend")
        add.backend.mark_as_done.side_effect = Exception()
        add.backend.mark_as_failure.side_effect = Exception("failed mark_as_failure")

        with pytest.raises(Exception):
            self.trace(add, (2, 2), {}, eager=False)

    def test_traceback_clear(self):
        import inspect
        import sys

        sys.exc_clear = Mock()
        frame_list = []

        def raise_dummy():
            frame_str_temp = str(inspect.currentframe().__repr__)
            frame_list.append(frame_str_temp)
            raise KeyError("foo")

        try:
            raise_dummy()
        except KeyError as exc:
            traceback_clear(exc)

            tb_ = exc.__traceback__
            while tb_ is not None:
                if str(tb_.tb_frame.__repr__) == frame_list[0]:
                    assert len(tb_.tb_frame.f_locals) == 0
                tb_ = tb_.tb_next

        try:
            raise_dummy()
        except KeyError as exc:
            traceback_clear()

            tb_ = exc.__traceback__
            while tb_ is not None:
                if str(tb_.tb_frame.__repr__) == frame_list[0]:
                    assert len(tb_.tb_frame.f_locals) == 0
                tb_ = tb_.tb_next

        try:
            raise_dummy()
        except KeyError as exc:
            traceback_clear(str(exc))

            tb_ = exc.__traceback__
            while tb_ is not None:
                if str(tb_.tb_frame.__repr__) == frame_list[0]:
                    assert len(tb_.tb_frame.f_locals) == 0
                tb_ = tb_.tb_next

    @patch("celery.app.trace.traceback_clear")
    def test_when_Ignore(self, mock_traceback_clear):
        @self.app.task(shared=False)
        def ignored():
            raise Ignore()

        retval, info = self.trace(ignored, (), {})
        assert info.state == states.IGNORED
        mock_traceback_clear.assert_called()

    @patch("celery.app.trace.traceback_clear")
    def test_when_Reject(self, mock_traceback_clear):
        @self.app.task(shared=False)
        def rejecting():
            raise Reject()

        retval, info = self.trace(rejecting, (), {})
        assert info.state == states.REJECTED
        mock_traceback_clear.assert_called()

    def test_backend_cleanup_raises(self):
        self.add.backend.process_cleanup = Mock()
        self.add.backend.process_cleanup.side_effect = RuntimeError()
        self.trace(self.add, (2, 2), {})

    @patch("celery.canvas.maybe_signature")
    def test_callbacks__scalar(self, maybe_signature):
        sig = Mock(name="sig")
        request = {"callbacks": [sig], "root_id": "root"}
        maybe_signature.return_value = sig
        retval, _ = self.trace(self.add, (2, 2), {}, request=request)
        sig.apply_async.assert_called_with((4,), parent_id="id-1", root_id="root", priority=None)

    @patch("celery.canvas.maybe_signature")
    def test_chain_proto2(self, maybe_signature):
        sig = Mock(name="sig")
        sig2 = Mock(name="sig2")
        request = {"chain": [sig2, sig], "root_id": "root"}
        maybe_signature.return_value = sig
        retval, _ = self.trace(self.add, (2, 2), {}, request=request)
        sig.apply_async.assert_called_with((4,), parent_id="id-1", root_id="root", chain=[sig2], priority=None)

    @patch("celery.canvas.maybe_signature")
    def test_chain_inherit_parent_priority(self, maybe_signature):
        self.app.conf.task_inherit_parent_priority = True
        sig = Mock(name="sig")
        sig2 = Mock(name="sig2")
        request = {
            "chain": [sig2, sig],
            "root_id": "root",
            "delivery_info": {"priority": 42},
        }
        maybe_signature.return_value = sig
        retval, _ = self.trace(self.add, (2, 2), {}, request=request)
        sig.apply_async.assert_called_with((4,), parent_id="id-1", root_id="root", chain=[sig2], priority=42)

    @patch("celery.canvas.maybe_signature")
    def test_callbacks__EncodeError(self, maybe_signature):
        sig = Mock(name="sig")
        request = {"callbacks": [sig], "root_id": "root"}
        maybe_signature.return_value = sig
        sig.apply_async.side_effect = EncodeError()
        retval, einfo = self.trace(self.add, (2, 2), {}, request=request)
        assert einfo.state == states.FAILURE

    @patch("celery.canvas.maybe_signature")
    @patch("celery.app.trace.group.apply_async")
    def test_callbacks__sigs(self, group_, maybe_signature):
        sig1 = Mock(name="sig")
        sig2 = Mock(name="sig2")
        sig3 = group([Mock(name="g1"), Mock(name="g2")], app=self.app)
        sig3.apply_async = Mock(name="gapply")
        request = {"callbacks": [sig1, sig3, sig2], "root_id": "root"}

        def pass_value(s, *args, **kwargs):
            return s

        maybe_signature.side_effect = pass_value
        retval, _ = self.trace(self.add, (2, 2), {}, request=request)
        group_.assert_called_with((4,), parent_id="id-1", root_id="root", priority=None)
        sig3.apply_async.assert_called_with((4,), parent_id="id-1", root_id="root", priority=None)

    @patch("celery.canvas.maybe_signature")
    @patch("celery.app.trace.group.apply_async")
    def test_callbacks__only_groups(self, group_, maybe_signature):
        sig1 = group([Mock(name="g1"), Mock(name="g2")], app=self.app)
        sig2 = group([Mock(name="g3"), Mock(name="g4")], app=self.app)
        sig1.apply_async = Mock(name="gapply")
        sig2.apply_async = Mock(name="gapply")
        request = {"callbacks": [sig1, sig2], "root_id": "root"}

        def pass_value(s, *args, **kwargs):
            return s

        maybe_signature.side_effect = pass_value
        retval, _ = self.trace(self.add, (2, 2), {}, request=request)
        sig1.apply_async.assert_called_with((4,), parent_id="id-1", root_id="root", priority=None)
        sig2.apply_async.assert_called_with((4,), parent_id="id-1", root_id="root", priority=None)

    def test_trace_SystemExit(self):
        with pytest.raises(SystemExit):
            self.trace(self.raises, (SystemExit(),), {})

    @patch("celery.app.trace.traceback_clear")
    def test_trace_Retry(self, mock_traceback_clear):
        exc = Retry("foo", "bar")
        _, info = self.trace(self.raises, (exc,), {})
        assert info.state == states.RETRY
        assert info.retval is exc
        mock_traceback_clear.assert_called()

    @patch("celery.app.trace.traceback_clear")
    def test_trace_exception(self, mock_traceback_clear):
        exc = KeyError("foo")
        _, info = self.trace(self.raises, (exc,), {})
        assert info.state == states.FAILURE
        assert info.retval is exc
        mock_traceback_clear.assert_called()

    def test_trace_task_ret__no_content_type(self):
        trace_task_ret(
            self.add.name,
            "id1",
            {},
            ((2, 2), {}, {}),
            None,
            None,
            app=self.app,
        )

    def test_fast_trace_task__no_content_type(self):
        self.app.tasks[self.add.name].__trace__ = build_tracer(
            self.add.name,
            self.add,
            app=self.app,
        )
        fast_trace_task(
            self.add.name,
            "id1",
            {},
            ((2, 2), {}, {}),
            None,
            None,
            app=self.app,
            _loc=[self.app.tasks, {}, "hostname"],
        )

    def test_trace_exception_propagate(self):
        with pytest.raises(KeyError):
            self.trace(self.raises, (KeyError("foo"),), {}, propagate=True)

    @patch("celery.app.trace.signals.task_internal_error.send")
    @patch("celery.app.trace.build_tracer")
    @patch("celery.app.trace.report_internal_error")
    def test_outside_body_error(self, report_internal_error, build_tracer, send):
        tracer = Mock()
        tracer.side_effect = KeyError("foo")
        build_tracer.return_value = tracer

        @self.app.task(shared=False)
        def xtask():
            pass

        trace_task(xtask, "uuid", (), {})
        assert report_internal_error.call_count
        assert send.call_count
        assert xtask.__trace__ is tracer

    def test_backend_error_should_report_failure(self):
        """check internal error is reported as failure.

        In case of backend error, an exception may bubble up from trace and be
        caught by trace_task.
        """

        @self.app.task(shared=False)
        def xtask():
            pass

        xtask.backend = BaseBackend(app=self.app)
        xtask.backend.mark_as_done = Mock()
        xtask.backend.mark_as_done.side_effect = Exception()
        xtask.backend.mark_as_failure = Mock()
        xtask.backend.mark_as_failure.side_effect = Exception()

        ret, info, _, _ = trace_task(xtask, "uuid", (), {}, app=self.app)
        assert info is not None
        assert isinstance(ret, ExceptionInfo)

    def test_deduplicate_successful_tasks__deduplication(self):
        @self.app.task(shared=False)
        def add(x, y):
            return x + y

        backend = CacheBackend(app=self.app, backend="memory")
        add.backend = backend
        add.store_eager_result = True
        add.ignore_result = False
        add.acks_late = True

        self.app.conf.worker_deduplicate_successful_tasks = True
        task_id = str(uuid4())
        request = {"id": task_id, "delivery_info": {"redelivered": True}}

        assert trace(self.app, add, (1, 1), task_id=task_id, request=request) == (2, None)
        assert trace(self.app, add, (1, 1), task_id=task_id, request=request) == (None, None)

        self.app.conf.worker_deduplicate_successful_tasks = False

    def test_deduplicate_successful_tasks__no_deduplication(self):
        @self.app.task(shared=False)
        def add(x, y):
            return x + y

        backend = CacheBackend(app=self.app, backend="memory")
        add.backend = backend
        add.store_eager_result = True
        add.ignore_result = False
        add.acks_late = True

        self.app.conf.worker_deduplicate_successful_tasks = True
        task_id = str(uuid4())
        request = {"id": task_id, "delivery_info": {"redelivered": True}}

        with patch("celery.app.trace.AsyncResult") as async_result_mock:
            async_result_mock().state.return_value = PENDING
            assert trace(self.app, add, (1, 1), task_id=task_id, request=request) == (2, None)
            assert trace(self.app, add, (1, 1), task_id=task_id, request=request) == (2, None)

        self.app.conf.worker_deduplicate_successful_tasks = False

    def test_deduplicate_successful_tasks__result_not_found(self):
        @self.app.task(shared=False)
        def add(x, y):
            return x + y

        backend = CacheBackend(app=self.app, backend="memory")
        add.backend = backend
        add.store_eager_result = True
        add.ignore_result = False
        add.acks_late = True

        self.app.conf.worker_deduplicate_successful_tasks = True
        task_id = str(uuid4())
        request = {"id": task_id, "delivery_info": {"redelivered": True}}

        with patch("celery.app.trace.AsyncResult") as async_result_mock:
            assert trace(self.app, add, (1, 1), task_id=task_id, request=request) == (2, None)
            state_property = PropertyMock(side_effect=BackendGetMetaError)
            type(async_result_mock()).state = state_property
            assert trace(self.app, add, (1, 1), task_id=task_id, request=request) == (2, None)

        self.app.conf.worker_deduplicate_successful_tasks = False

    def test_deduplicate_successful_tasks__cached_request(self):
        @self.app.task(shared=False)
        def add(x, y):
            return x + y

        backend = CacheBackend(app=self.app, backend="memory")
        add.backend = backend
        add.store_eager_result = True
        add.ignore_result = False
        add.acks_late = True

        self.app.conf.worker_deduplicate_successful_tasks = True

        task_id = str(uuid4())
        request = {"id": task_id, "delivery_info": {"redelivered": True}}

        successful_requests.add(task_id)

        assert trace(self.app, add, (1, 1), task_id=task_id, request=request) == (None, None)

        successful_requests.clear()
        self.app.conf.worker_deduplicate_successful_tasks = False

    # -- dedup fast path: chain and callback dispatch (upstream 865922abd) --

    def _redelivered_after_success(self):
        """A task whose result is already stored, as a redelivery finds it.

        Returns the task and its id. The id is dropped from
        ``successful_requests`` again: a real redelivery is picked up by a
        worker that has never seen the message, so only the backend knows.
        """

        @self.app.task(shared=False)
        def add(x, y):
            return x + y

        add.backend = CacheBackend(app=self.app, backend="memory")
        add.store_eager_result = True
        add.ignore_result = False
        add.acks_late = True

        self.app.conf.worker_deduplicate_successful_tasks = True
        task_id = str(uuid4())
        request = {"id": task_id, "delivery_info": {"redelivered": True}}

        trace(self.app, add, (1, 1), task_id=task_id, request=request)
        successful_requests.discard(task_id)
        return add, task_id

    def _redelivery(self, task_id, **extra):
        return dict({"id": task_id, "delivery_info": {"redelivered": True}}, **extra)

    def test_dedup_dispatches_the_chain(self):
        # The first worker stored the result and died before the ack, so the
        # rest of the chain never went out. Returning early here strands it.
        add, task_id = self._redelivered_after_success()

        with patch("celery.canvas.maybe_signature") as signature:
            apply_async = signature.return_value.apply_async = Mock()
            trace(self.app, add, (1, 1), task_id=task_id, request=self._redelivery(task_id, chain=[self.add.s(10)]))

        apply_async.assert_called_once()
        args, kwargs = apply_async.call_args
        # The stored result, not a fresh one: the task did not run again.
        assert args == ((2,),)
        assert kwargs["parent_id"] == task_id
        assert kwargs["root_id"] == task_id

    def test_dedup_chain_dispatch_keeps_the_remaining_steps(self):
        add, task_id = self._redelivered_after_success()
        step2, step3 = self.add.s(20), self.add.s(30)

        with patch("celery.canvas.maybe_signature") as signature:
            apply_async = signature.return_value.apply_async = Mock()
            trace(self.app, add, (1, 1), task_id=task_id, request=self._redelivery(task_id, chain=[step3, step2]))

        assert apply_async.call_args[1]["chain"] == [step3]

    def test_dedup_dispatches_the_callbacks(self):
        add, task_id = self._redelivered_after_success()

        with patch("celery.canvas.maybe_signature") as signature:
            apply_async = signature.return_value.apply_async = Mock()
            trace(self.app, add, (1, 1), task_id=task_id, request=self._redelivery(task_id, callbacks=[self.add.s(99)]))

        apply_async.assert_called_once()
        assert apply_async.call_args[0] == ((2,),)
        assert apply_async.call_args[1]["parent_id"] == task_id

    def test_dedup_dispatches_the_callbacks_and_the_chain(self):
        add, task_id = self._redelivered_after_success()

        with patch("celery.canvas.maybe_signature") as signature:
            apply_async = signature.return_value.apply_async = Mock()
            trace(
                self.app,
                add,
                (1, 1),
                task_id=task_id,
                request=self._redelivery(task_id, chain=[self.add.s(10)], callbacks=[self.add.s(99)]),
            )

        assert apply_async.call_count == 2

    def test_dedup_marks_the_request_successful(self):
        add, task_id = self._redelivered_after_success()

        with patch("celery.canvas.maybe_signature"):
            trace(self.app, add, (1, 1), task_id=task_id, request=self._redelivery(task_id))

        assert task_id in successful_requests

    def test_dedup_skips_dispatch_when_the_result_has_children(self):
        # Children mean the first worker did get the callbacks out before it
        # stored the result, so sending them again would duplicate them.
        add, task_id = self._redelivered_after_success()
        meta = {"status": "SUCCESS", "result": 2, "children": [("some-child-id", None)]}

        with patch("celery.canvas.maybe_signature") as signature:
            apply_async = signature.return_value.apply_async = Mock()
            with patch("celery.result.AsyncResult._get_task_meta", return_value=meta):
                trace(self.app, add, (1, 1), task_id=task_id, request=self._redelivery(task_id, chain=[self.add.s(10)]))

        apply_async.assert_not_called()

    def test_dedup_skips_dispatch_when_there_is_nothing_to_dispatch(self):
        add, task_id = self._redelivered_after_success()

        with patch("celery.canvas.maybe_signature") as signature:
            apply_async = signature.return_value.apply_async = Mock()
            trace(self.app, add, (1, 1), task_id=task_id, request=self._redelivery(task_id, chain=[], callbacks=[]))

        apply_async.assert_not_called()

    def test_dedup_dispatch_failure_requeues_instead_of_acking(self):
        # Acking here would strand the chain: the result is stored, so the
        # next delivery is the only chance left to send it.
        add, task_id = self._redelivered_after_success()

        with patch("celery.canvas.maybe_signature") as signature:
            signature.return_value.apply_async.side_effect = RuntimeError("broker down")
            with patch("celery.app.trace.logger") as logger:
                with pytest.raises(Reject) as exc_info:
                    trace(
                        self.app,
                        add,
                        (1, 1),
                        task_id=task_id,
                        request=self._redelivery(task_id, chain=[self.add.s(10)]),
                    )

        assert exc_info.value.requeue is True
        logger.error.assert_called_once()
        assert "deduplicated task" in logger.error.call_args[0][0]
        # Not marked successful, or the redelivery would take the early return
        # and never retry the dispatch.
        assert task_id not in successful_requests

    def test_dedup_dispatch_memory_error_is_not_turned_into_a_reject(self):
        add, task_id = self._redelivered_after_success()

        with patch("celery.canvas.maybe_signature") as signature:
            signature.return_value.apply_async.side_effect = MemoryError()
            with pytest.raises(MemoryError):
                trace(self.app, add, (1, 1), task_id=task_id, request=self._redelivery(task_id, chain=[self.add.s(10)]))

    def test_dedup_reject_survives_the_trace_task_wrapper(self):
        # trace_task() turns stray exceptions into a failure result. A Reject
        # is control flow for the consumer and has to get past it.
        add, task_id = self._redelivered_after_success()
        add.__trace__ = None

        with patch("celery.canvas.maybe_signature") as signature:
            signature.return_value.apply_async.side_effect = RuntimeError("broker down")
            with patch("celery.app.trace.logger"):
                with pytest.raises(Reject):
                    trace_task(
                        add,
                        task_id,
                        (1, 1),
                        {},
                        request=self._redelivery(task_id, chain=[self.add.s(10)]),
                        app=self.app,
                    )

    def test_dedup_from_memory_skips_dispatch(self):
        # This worker ran the task itself, so it already sent the chain.
        add, task_id = self._redelivered_after_success()
        successful_requests.add(task_id)

        with patch("celery.canvas.maybe_signature") as signature:
            apply_async = signature.return_value.apply_async = Mock()
            trace(self.app, add, (1, 1), task_id=task_id, request=self._redelivery(task_id, chain=[self.add.s(10)]))

        apply_async.assert_not_called()

    def test_chain_dispatch_does_not_mutate_the_request_chain(self):
        # pop() emptied the caller's list, so a retry or a redelivery traced
        # the same request with a chain one step short.
        @self.app.task(shared=False)
        def add(x, y):
            return x + y

        add.backend = CacheBackend(app=self.app, backend="memory")
        add.store_eager_result = True
        add.ignore_result = False

        chain = [self.add.s(10), self.add.s(20)]
        task_id = str(uuid4())

        with patch("celery.canvas.maybe_signature") as signature:
            signature.return_value.apply_async = Mock()
            trace(
                self.app,
                add,
                (1, 1),
                task_id=task_id,
                request={"id": task_id, "delivery_info": {"redelivered": False}, "chain": chain},
            )
            assert signature.return_value.apply_async.call_args[1]["chain"] == chain[:-1]

        assert len(chain) == 2

    @pytest.mark.asyncio
    async def test_async_dedup_dispatches_the_chain(self):
        add, task_id = self._redelivered_after_success()

        with patch("celery.canvas.maybe_signature") as signature:
            aapply_async = signature.return_value.aapply_async = AsyncMock()
            await atrace(
                self.app, add, (1, 1), task_id=task_id, request=self._redelivery(task_id, chain=[self.add.s(10)])
            )

        aapply_async.assert_awaited_once()
        args, kwargs = aapply_async.call_args
        assert args == ((2,),)
        assert kwargs["parent_id"] == task_id
        assert kwargs["root_id"] == task_id

    @pytest.mark.asyncio
    async def test_async_dedup_dispatch_failure_requeues_instead_of_acking(self):
        add, task_id = self._redelivered_after_success()

        with patch("celery.canvas.maybe_signature") as signature:
            signature.return_value.aapply_async = AsyncMock(side_effect=RuntimeError("broker down"))
            with patch("celery.app.trace.logger"):
                with pytest.raises(Reject) as exc_info:
                    await atrace(
                        self.app,
                        add,
                        (1, 1),
                        task_id=task_id,
                        request=self._redelivery(task_id, chain=[self.add.s(10)]),
                    )

        assert exc_info.value.requeue is True
        assert task_id not in successful_requests

    @pytest.mark.asyncio
    async def test_async_dedup_skips_dispatch_when_the_result_has_children(self):
        add, task_id = self._redelivered_after_success()
        meta = {"status": states.SUCCESS, "result": 2, "children": [self.add.s(10)]}

        with patch("celery.canvas.maybe_signature") as signature:
            aapply_async = signature.return_value.aapply_async = AsyncMock()
            with patch("celery.result.AsyncResult._get_task_meta", return_value=meta):
                await atrace(
                    self.app, add, (1, 1), task_id=task_id, request=self._redelivery(task_id, chain=[self.add.s(10)])
                )

        aapply_async.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_async_dedup_dispatch_memory_error_is_not_turned_into_a_reject(self):
        add, task_id = self._redelivered_after_success()

        with patch("celery.canvas.maybe_signature") as signature:
            signature.return_value.aapply_async = AsyncMock(side_effect=MemoryError())
            with pytest.raises(MemoryError):
                await atrace(
                    self.app, add, (1, 1), task_id=task_id, request=self._redelivery(task_id, chain=[self.add.s(10)])
                )

    @pytest.mark.asyncio
    async def test_async_chain_dispatch_does_not_mutate_the_request_chain(self):
        @self.app.task(shared=False)
        def add(x, y):
            return x + y

        add.backend = CacheBackend(app=self.app, backend="memory")
        add.store_eager_result = True
        add.ignore_result = False

        chain = [self.add.s(10), self.add.s(20)]
        task_id = str(uuid4())

        with patch("celery.canvas.maybe_signature") as signature:
            signature.return_value.aapply_async = AsyncMock()
            await atrace(
                self.app,
                add,
                (1, 1),
                task_id=task_id,
                request={"id": task_id, "delivery_info": {"redelivered": False}, "chain": chain},
            )
            assert signature.return_value.aapply_async.call_args[1]["chain"] == chain[:-1]

        assert len(chain) == 2


@pytest.mark.asyncio
class test_async_trace(TraceCase):
    """The aio pool runs build_async_tracer, not build_tracer.

    Every branch below has a sync twin in ``test_trace``; the two tracers are
    separate code paths, so a fix applied to one silently misses the other.
    """

    def teardown_method(self):
        successful_requests.clear()
        self.app.conf.worker_deduplicate_successful_tasks = False

    def atrace(self, *args, **kwargs):
        return atrace(self.app, *args, **kwargs)

    async def test_trace_successful(self):
        retval, info = await self.atrace(self.add, (2, 2), {})
        assert info is None
        assert retval == 4

    async def test_trace_before_start(self):
        @self.app.task(shared=False, before_start=Mock())
        def add_with_before_start(x, y):
            return x + y

        await self.atrace(add_with_before_start, (2, 2), {})
        add_with_before_start.before_start.assert_called()

    async def test_trace_on_success(self):
        @self.app.task(shared=False, on_success=Mock())
        def add_with_success(x, y):
            return x + y

        await self.atrace(add_with_success, (2, 2), {})
        add_with_success.on_success.assert_called()

    async def test_trace_after_return(self):
        @self.app.task(shared=False, after_return=Mock())
        def add_with_after_return(x, y):
            return x + y

        await self.atrace(add_with_after_return, (2, 2), {})
        add_with_after_return.after_return.assert_called()

    async def test_kwargs_that_are_not_a_mapping(self):
        with pytest.raises(InvalidTaskError):
            await self.atrace(self.add, (2, 2), ["not", "a", "mapping"])

    async def test_with_prerun_receivers(self):
        on_prerun = Mock()
        signals.task_prerun.connect(on_prerun)
        try:
            await self.atrace(self.add, (2, 2), {})
            on_prerun.assert_called()
        finally:
            signals.task_prerun.receivers[:] = []

    async def test_with_postrun_receivers(self):
        on_postrun = Mock()
        signals.task_postrun.connect(on_postrun)
        try:
            await self.atrace(self.add, (2, 2), {})
            on_postrun.assert_called()
        finally:
            signals.task_postrun.receivers[:] = []

    async def test_with_success_receivers(self):
        on_success = Mock()
        signals.task_success.connect(on_success)
        try:
            await self.atrace(self.add, (2, 2), {})
            on_success.assert_called()
        finally:
            signals.task_success.receivers[:] = []

    async def test_track_started_stores_the_started_state(self):
        self.add.track_started = True
        self.add.ignore_result = False
        self.add.backend = Mock(name="backend")
        self.add.backend.astore_result = AsyncMock()
        self.add.backend.amark_as_done = AsyncMock()

        await self.atrace(self.add, (2, 2), {}, eager=False)

        args, _ = self.add.backend.astore_result.call_args
        assert args[2] == states.STARTED

    async def test_when_backend_cleanup_raises(self):
        @self.app.task(shared=False)
        def add(x, y):
            return x + y

        add.backend = Mock(name="backend")
        add.backend.amark_as_done = AsyncMock()
        add.backend.process_cleanup.side_effect = KeyError()

        await self.atrace(add, (2, 2), {}, eager=False)

        add.backend.process_cleanup.assert_called_with()
        add.backend.process_cleanup.side_effect = MemoryError()
        with pytest.raises(MemoryError):
            await self.atrace(add, (2, 2), {}, eager=False)

    @patch("celery.app.trace.traceback_clear")
    async def test_when_Ignore(self, mock_traceback_clear):
        @self.app.task(shared=False)
        def ignored():
            raise Ignore()

        _, info = await self.atrace(ignored, (), {})
        assert info.state == states.IGNORED
        mock_traceback_clear.assert_called()

    @patch("celery.app.trace.traceback_clear")
    async def test_when_Reject(self, mock_traceback_clear):
        @self.app.task(shared=False)
        def rejecting():
            raise Reject()

        _, info = await self.atrace(rejecting, (), {})
        assert info.state == states.REJECTED
        mock_traceback_clear.assert_called()

    @patch("celery.app.trace.traceback_clear")
    async def test_trace_Retry(self, mock_traceback_clear):
        exc = Retry("foo", "bar")
        _, info = await self.atrace(self.raises, (exc,), {})
        assert info.state == states.RETRY
        assert info.retval is exc
        mock_traceback_clear.assert_called()

    @patch("celery.app.trace.traceback_clear")
    async def test_trace_exception(self, mock_traceback_clear):
        exc = KeyError("foo")
        _, info = await self.atrace(self.raises, (exc,), {})
        assert info.state == states.FAILURE
        assert info.retval is exc
        mock_traceback_clear.assert_called()

    async def test_trace_exception_propagate(self):
        with pytest.raises(KeyError):
            await self.atrace(self.raises, (KeyError("foo"),), {}, propagate=True)

    async def test_trace_SystemExit(self):
        with pytest.raises(SystemExit):
            await self.atrace(self.raises, (SystemExit(),), {})

    @patch("celery.canvas.maybe_signature")
    async def test_callbacks__scalar(self, maybe_signature):
        sig = Mock(name="sig")
        sig.aapply_async = AsyncMock()
        maybe_signature.return_value = sig

        await self.atrace(self.add, (2, 2), {}, request={"callbacks": [sig], "root_id": "root"})

        sig.aapply_async.assert_awaited_with((4,), parent_id="id-1", root_id="root", priority=None)

    @patch("celery.canvas.maybe_signature")
    async def test_chain_proto2(self, maybe_signature):
        sig, sig2 = Mock(name="sig"), Mock(name="sig2")
        sig.aapply_async = AsyncMock()
        maybe_signature.return_value = sig

        await self.atrace(self.add, (2, 2), {}, request={"chain": [sig2, sig], "root_id": "root"})

        sig.aapply_async.assert_awaited_with((4,), parent_id="id-1", root_id="root", chain=[sig2], priority=None)

    @patch("celery.canvas.maybe_signature")
    async def test_chain_inherit_parent_priority(self, maybe_signature):
        self.app.conf.task_inherit_parent_priority = True
        sig, sig2 = Mock(name="sig"), Mock(name="sig2")
        sig.aapply_async = AsyncMock()
        maybe_signature.return_value = sig
        request = {"chain": [sig2, sig], "root_id": "root", "delivery_info": {"priority": 42}}

        await self.atrace(self.add, (2, 2), {}, request=request)

        sig.aapply_async.assert_awaited_with((4,), parent_id="id-1", root_id="root", chain=[sig2], priority=42)

    @patch("celery.canvas.maybe_signature")
    async def test_callbacks__EncodeError(self, maybe_signature):
        sig = Mock(name="sig")
        sig.aapply_async = AsyncMock(side_effect=EncodeError())
        maybe_signature.return_value = sig

        _, einfo = await self.atrace(self.add, (2, 2), {}, request={"callbacks": [sig], "root_id": "root"})

        assert einfo.state == states.FAILURE

    @patch("celery.canvas.maybe_signature")
    @patch("celery.app.trace.group.aapply_async")
    async def test_callbacks__sigs(self, group_, maybe_signature):
        """A group among the callbacks is applied on its own, so the trail is stored once."""
        sig1, sig2 = Mock(name="sig1"), Mock(name="sig2")
        sig3 = group([Mock(name="g1"), Mock(name="g2")], app=self.app)
        sig3.aapply_async = AsyncMock(name="gapply")
        maybe_signature.side_effect = lambda s, *args, **kwargs: s
        request = {"callbacks": [sig1, sig3, sig2], "root_id": "root"}

        await self.atrace(self.add, (2, 2), {}, request=request)

        group_.assert_awaited_with((4,), parent_id="id-1", root_id="root", priority=None)
        sig3.aapply_async.assert_awaited_with((4,), parent_id="id-1", root_id="root", priority=None)

    @patch("celery.canvas.maybe_signature")
    async def test_callbacks__only_groups(self, maybe_signature):
        sig1 = group([Mock(name="g1"), Mock(name="g2")], app=self.app)
        sig2 = group([Mock(name="g3"), Mock(name="g4")], app=self.app)
        sig1.aapply_async = AsyncMock(name="gapply1")
        sig2.aapply_async = AsyncMock(name="gapply2")
        maybe_signature.side_effect = lambda s, *args, **kwargs: s

        await self.atrace(self.add, (2, 2), {}, request={"callbacks": [sig1, sig2], "root_id": "root"})

        sig1.aapply_async.assert_awaited_with((4,), parent_id="id-1", root_id="root", priority=None)
        sig2.aapply_async.assert_awaited_with((4,), parent_id="id-1", root_id="root", priority=None)

    @patch("celery.app.trace.signals.task_internal_error.send")
    async def test_error_outside_the_body_is_reported_as_internal(self, send):
        """The body never ran, so there is no einfo: report it and fail the task."""
        # The real backend, not a Mock: report_internal_error runs the exception
        # through prepare_exception, and a Mock return value is not an exception.
        self.add.backend.amark_as_done = AsyncMock(side_effect=RuntimeError("backend down"))
        self.add.backend.amark_as_failure = AsyncMock()

        _, info = await self.atrace(self.add, (2, 2), {}, eager=False)

        assert send.call_count
        assert info.state == states.FAILURE

    async def test_an_internal_error_propagates_when_eager(self):
        self.add.backend = Mock(name="backend")
        self.add.backend.amark_as_done = AsyncMock(side_effect=RuntimeError("backend down"))
        self.add.ignore_result = False
        self.add.store_eager_result = True

        with pytest.raises(RuntimeError):
            await self.atrace(self.add, (2, 2), {})

    async def test_dedup_returns_early_for_a_request_already_known_successful(self):
        self.app.conf.worker_deduplicate_successful_tasks = True
        self.add.backend = Mock(name="backend", persistent=True)
        self.add.backend.amark_as_done = AsyncMock()
        self.add.acks_late = True
        successful_requests.add("id-1")

        retval, _ = await self.atrace(
            self.add,
            (2, 2),
            {},
            eager=False,
            request={"id": "id-1", "delivery_info": {"redelivered": True}},
        )

        assert retval is None
        self.add.backend.amark_as_done.assert_not_called()

    async def test_dedup_runs_the_task_when_the_result_is_not_stored(self):
        """BackendGetMetaError means nothing is known, so the redelivery is a real run."""
        self.app.conf.worker_deduplicate_successful_tasks = True
        self.add.backend = Mock(name="backend", persistent=True)
        self.add.backend.amark_as_done = AsyncMock()
        self.add.acks_late = True

        with patch("celery.app.trace.AsyncResult") as result:
            type(result.return_value).state = PropertyMock(side_effect=BackendGetMetaError("no meta"))
            retval, _ = await self.atrace(
                self.add,
                (2, 2),
                {},
                eager=False,
                request={"id": "id-1", "delivery_info": {"redelivered": True}},
            )

        assert retval == 4


class test_TraceInfo(TraceCase):
    class TI(TraceInfo):
        __slots__ = TraceInfo.__slots__ + ("__dict__",)

    def test_handle_error_state(self):
        x = self.TI(states.FAILURE)
        x.handle_failure = Mock()
        x.handle_error_state(self.add_cast, self.add_cast.request)
        x.handle_failure.assert_called_with(
            self.add_cast,
            self.add_cast.request,
            store_errors=self.add_cast.store_errors_even_if_ignored,
            call_errbacks=True,
        )

    def test_handle_error_state_for_eager_task(self):
        x = self.TI(states.FAILURE)
        x.handle_failure = Mock()

        x.handle_error_state(self.add, self.add.request, eager=True)
        x.handle_failure.assert_called_once_with(
            self.add,
            self.add.request,
            store_errors=False,
            call_errbacks=True,
        )

    def test_handle_error_for_eager_saved_to_backend(self):
        x = self.TI(states.FAILURE)
        x.handle_failure = Mock()

        self.add.store_eager_result = True

        x.handle_error_state(self.add, self.add.request, eager=True)
        x.handle_failure.assert_called_with(
            self.add,
            self.add.request,
            store_errors=True,
            call_errbacks=True,
        )

    @patch("celery.app.trace.ExceptionInfo")
    def test_handle_reject(self, ExceptionInfo):
        x = self.TI(states.FAILURE)
        x._log_error = Mock(name="log_error")
        req = Mock(name="req")
        x.handle_reject(self.add, req)
        x._log_error.assert_called_with(self.add, req, ExceptionInfo())


@pytest.mark.asyncio
class test_TraceInfo_async(TraceCase):
    """The ``a``-prefixed handlers the async tracer dispatches to.

    Twins of the ones above, and separate code paths, so a change to one
    silently misses the other.
    """

    class TI(TraceInfo):
        __slots__ = TraceInfo.__slots__ + ("__dict__",)

    async def test_ahandle_error_state(self):
        x = self.TI(states.FAILURE)
        x.ahandle_failure = AsyncMock()
        await x.ahandle_error_state(self.add_cast, self.add_cast.request)
        x.ahandle_failure.assert_awaited_with(
            self.add_cast,
            self.add_cast.request,
            store_errors=self.add_cast.store_errors_even_if_ignored,
            call_errbacks=True,
        )

    async def test_ahandle_error_state_for_eager_task(self):
        x = self.TI(states.FAILURE)
        x.ahandle_failure = AsyncMock()

        await x.ahandle_error_state(self.add, self.add.request, eager=True)
        x.ahandle_failure.assert_awaited_once_with(
            self.add,
            self.add.request,
            store_errors=False,
            call_errbacks=True,
        )

    async def test_ahandle_error_for_eager_saved_to_backend(self):
        x = self.TI(states.FAILURE)
        x.ahandle_failure = AsyncMock()
        self.add.store_eager_result = True

        await x.ahandle_error_state(self.add, self.add.request, eager=True)
        x.ahandle_failure.assert_awaited_with(
            self.add,
            self.add.request,
            store_errors=True,
            call_errbacks=True,
        )

    async def test_ahandle_error_state_dispatches_a_retry(self):
        x = self.TI(states.RETRY)
        x.ahandle_retry = AsyncMock()
        await x.ahandle_error_state(self.add, self.add.request)
        x.ahandle_retry.assert_awaited_once()

    async def test_ahandle_retry_stores_the_reason(self):
        self.add.backend.amark_as_retry = AsyncMock()
        reason = Retry("retry me", KeyError("the cause"))
        x = self.TI(states.RETRY, reason)

        try:
            raise reason
        except Retry:
            einfo = await x.ahandle_retry(self.add, self.add.request)

        assert einfo.type is Retry
        args = self.add.backend.amark_as_retry.await_args
        assert args.args[1] is reason.exc

    async def test_ahandle_retry_without_store_errors_skips_the_backend(self):
        self.add.backend.amark_as_retry = AsyncMock()
        reason = Retry("retry me", KeyError("the cause"))
        x = self.TI(states.RETRY, reason)

        try:
            raise reason
        except Retry:
            await x.ahandle_retry(self.add, self.add.request, store_errors=False)

        self.add.backend.amark_as_retry.assert_not_awaited()

    async def test_ahandle_failure_marks_the_task_failed(self):
        self.add.backend.amark_as_failure = AsyncMock()
        x = self.TI(states.FAILURE)

        try:
            raise KeyError("the cause")
        except KeyError as exc:
            x.retval = exc
            einfo = await x.ahandle_failure(self.add, self.add.request)

        assert einfo.type is KeyError
        self.add.backend.amark_as_failure.assert_awaited_once()
        assert self.add.backend.amark_as_failure.await_args.kwargs["store_result"] is True

    async def test_ahandle_failure_borrows_the_traceback_being_handled(self):
        """An exception that was never raised carries no traceback of its own."""
        self.add.backend.amark_as_failure = AsyncMock()
        x = self.TI(states.FAILURE, KeyError("never raised"))

        try:
            raise RuntimeError("the one actually being handled")
        except RuntimeError:
            einfo = await x.ahandle_failure(self.add, self.add.request)

        assert einfo.exception.__traceback__ is not None


class test_stackprotection:
    def test_stackprotection(self):
        setup_worker_optimizations(self.app)
        try:

            @self.app.task(shared=False, bind=True)
            def foo(self, i):
                if i:
                    return foo(0)
                return self.request

            assert foo(1).called_directly
        finally:
            reset_worker_optimizations(self.app)

    def test_stackprotection_headers_passed_on_new_request_stack(self):
        setup_worker_optimizations(self.app)
        try:

            @self.app.task(shared=False, bind=True)
            def foo(self, i):
                if i:
                    return foo.apply(args=(i - 1,), headers=456)
                return self.request

            task = foo.apply(args=(2,), headers=123, loglevel=5)
            assert task.result.result.result.args == (0,)
            assert task.result.result.result.headers == 456
            assert task.result.result.result.loglevel == 0
        finally:
            reset_worker_optimizations(self.app)

    def test_stackprotection_headers_persisted_calling_task_directly(self):
        setup_worker_optimizations(self.app)
        try:

            @self.app.task(shared=False, bind=True)
            def foo(self, i):
                if i:
                    return foo(i - 1)
                return self.request

            task = foo.apply(args=(2,), headers=123, loglevel=5)
            assert task.result.args == (0,)
            assert task.result.headers == 123
            assert task.result.loglevel == 5
        finally:
            reset_worker_optimizations(self.app)
