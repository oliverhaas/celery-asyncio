import asyncio
import gc
import importlib
import inspect
import os
import ssl
import typing
import uuid
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pickle import dumps, loads
from unittest.mock import DEFAULT, Mock, call, patch
from zoneinfo import ZoneInfo

import pytest
from kombu import Connection, Exchange, Queue

pydantic = pytest.importorskip("pydantic")
from pydantic import BaseModel, ValidationInfo, model_validator

from celery import Celery, Task, _state, current_app, group, shared_task
from celery import app as _app
from celery.app import defaults
from celery.app.amqp import AMQP
from celery.backends.base import Backend
from celery.exceptions import ImproperlyConfigured
from celery.loaders.base import unconfigured
from celery.platforms import pyimplementation
from celery.utils.collections import DictAttribute
from celery.utils.objects import Bunch
from celery.utils.promises import promise
from celery.utils.serialization import pickle
from celery.utils.time import LocalTimezone, localize, timezone, to_utc
from tests.unit import conftest

THIS_IS_A_KEY = "this is a value"


class ObjectConfig:
    FOO = 1
    BAR = 2


object_config = ObjectConfig()
dict_config = {"FOO": 10, "BAR": 20}


class ObjectConfig2:
    LEAVE_FOR_WORK = True
    MOMENT_TO_STOP = True
    CALL_ME_BACK = 123456789
    WANT_ME_TO = False
    UNDERSTAND_ME = True


class test_module:
    def test_default_app(self):
        assert _app.default_app == _state.default_app

    def test_bugreport(self, app):
        assert _app.bugreport(app=app)


class test_task_join_will_block:
    def test_task_join_will_block(self, patching):
        patching("celery._state._task_join_will_block", 0)
        assert _state._task_join_will_block == 0
        _state._set_task_join_will_block(True)
        assert _state._task_join_will_block is True
        # fixture 'app' sets this, so need to use orig_ function
        # set there by that fixture.
        res = _state.orig_task_join_will_block()
        assert res is True


class test_App:
    def setup_method(self):
        self.app.add_defaults(deepcopy(self.CELERY_TEST_CONFIG))

    def test_now(self):
        timezone_setting_value = "US/Eastern"
        tz_utc = timezone.get_timezone("UTC")
        tz_us_eastern = timezone.get_timezone(timezone_setting_value)

        now = to_utc(datetime.now(UTC))
        app_now = self.app.now()

        assert app_now.tzinfo is tz_utc
        assert app_now - now <= timedelta(seconds=1)

        # Check that timezone conversion is applied from configuration
        self.app.conf.enable_utc = False
        self.app.conf.timezone = timezone_setting_value
        # timezone is a cached property
        del self.app.timezone

        app_now = self.app.now()

        assert app_now.tzinfo == tz_us_eastern

        diff = to_utc(datetime.now(UTC)) - localize(app_now, tz_utc)
        assert diff <= timedelta(seconds=1)

        # Verify that timezone setting overrides enable_utc=on setting
        self.app.conf.enable_utc = True
        del self.app.timezone
        app_now = self.app.now()
        assert self.app.timezone == tz_us_eastern
        assert app_now.tzinfo == tz_us_eastern

    @patch("celery.app.base.set_default_app")
    def test_set_default(self, set_default_app):
        self.app.set_default()
        set_default_app.assert_called_with(self.app)

    def test_setup_security(self):
        with pytest.raises(NotImplementedError):
            self.app.setup_security()

    def test_task_autofinalize_disabled(self):
        with self.Celery("xyzibari", autofinalize=False) as app:

            @app.task
            def ttafd():
                return 42

            with pytest.raises(RuntimeError):
                ttafd()

        with self.Celery("xyzibari", autofinalize=False) as app:

            @app.task
            def ttafd2():
                return 42

            app.finalize()
            assert ttafd2() == 42

    def test_registry_autofinalize_disabled(self):
        with self.Celery("xyzibari", autofinalize=False) as app:
            with pytest.raises(RuntimeError):
                app.tasks["celery.chain"]
            app.finalize()
            assert app.tasks["celery.chain"]

    def test_task(self):
        with self.Celery("foozibari") as app:

            def fun():
                pass

            fun.__module__ = "__main__"
            task = app.task(fun)
            assert task.name == app.main + ".fun"

    def test_task_too_many_args(self):
        with pytest.raises(TypeError):
            self.app.task(Mock(name="fun"), True)
        with pytest.raises(TypeError):
            self.app.task(Mock(name="fun"), True, 1, 2)

    def test_with_config_source(self):
        with self.Celery(config_source=ObjectConfig) as app:
            assert app.conf.FOO == 1
            assert app.conf.BAR == 2

    def test_task_takes_no_args(self):
        with pytest.raises(TypeError):

            @self.app.task(1)
            def foo():
                pass

    def test_add_defaults(self):
        assert not self.app.configured
        _conf = {"foo": 300}

        def conf():
            return _conf

        self.app.add_defaults(conf)
        assert conf in self.app._pending_defaults
        assert not self.app.configured
        assert self.app.conf.foo == 300
        assert self.app.configured
        assert not self.app._pending_defaults

        # defaults not pickled
        appr = loads(dumps(self.app))
        with pytest.raises(AttributeError):
            appr.conf.foo

        # add more defaults after configured
        conf2 = {"foo": "BAR"}
        self.app.add_defaults(conf2)
        assert self.app.conf.foo == "BAR"

        assert _conf in self.app.conf.defaults
        assert conf2 in self.app.conf.defaults

    def test_using_v1_reduce(self):
        self.app._using_v1_reduce = True
        assert loads(dumps(self.app))

    def test_autodiscover_tasks_force_fixup_fallback(self):
        self.app.loader.autodiscover_tasks = Mock()
        self.app.autodiscover_tasks([], force=True)
        self.app.loader.autodiscover_tasks.assert_called_with(
            [],
            "tasks",
        )

    def test_autodiscover_tasks_force(self):
        self.app.loader.autodiscover_tasks = Mock()
        self.app.autodiscover_tasks(["proj.A", "proj.B"], force=True)
        self.app.loader.autodiscover_tasks.assert_called_with(
            ["proj.A", "proj.B"],
            "tasks",
        )
        self.app.loader.autodiscover_tasks = Mock()

        def lazy_list():
            return ["proj.A", "proj.B"]

        self.app.autodiscover_tasks(
            lazy_list,
            related_name="george",
            force=True,
        )
        self.app.loader.autodiscover_tasks.assert_called_with(
            ["proj.A", "proj.B"],
            "george",
        )

    def test_autodiscover_tasks_lazy(self):
        with patch("celery.signals.import_modules") as import_modules:

            def lazy_list():
                return [1, 2, 3]

            self.app.autodiscover_tasks(lazy_list)
            import_modules.connect.assert_called()
            prom = import_modules.connect.call_args[0][0]
            assert isinstance(prom, promise)

    def test_autodiscover_tasks__no_packages(self):
        fixup1 = Mock(name="fixup")
        fixup2 = Mock(name="fixup")
        self.app._autodiscover_tasks_from_names = Mock(name="auto")
        self.app._fixups = [fixup1, fixup2]
        fixup1.autodiscover_tasks.return_value = ["A", "B", "C"]
        fixup2.autodiscover_tasks.return_value = ["D", "E", "F"]
        self.app.autodiscover_tasks(force=True)
        self.app._autodiscover_tasks_from_names.assert_called_with(
            ["A", "B", "C", "D", "E", "F"],
            related_name="tasks",
        )

    def test_with_broker(self, patching):
        patching.setenv("CELERY_BROKER_URL", "")
        with self.Celery(broker="foo://baribaz") as app:
            assert app.conf.broker_url == "foo://baribaz"

    def test_pending_configuration_non_true__kwargs(self):
        with self.Celery(task_create_missing_queues=False) as app:
            assert app.conf.task_create_missing_queues is False

    def test_pending_configuration__kwargs(self):
        with self.Celery(foo="bar") as app:
            assert app.conf.foo == "bar"

    def test_pending_configuration__setattr(self):
        with self.Celery(broker="foo://bar") as app:
            app.conf.task_default_delivery_mode = 44
            app.conf.worker_state_db = "foo.state"
            assert not app.configured
            assert app.conf.worker_state_db == "foo.state"
            assert app.conf.broker_url == "foo://bar"
            assert app._preconf["worker_state_db"] == "foo.state"

            assert app.configured
            reapp = pickle.loads(pickle.dumps(app))
            assert reapp._preconf["worker_state_db"] == "foo.state"
            assert not reapp.configured
            assert reapp.conf.worker_state_db == "foo.state"
            assert reapp.configured
            assert reapp.conf.broker_url == "foo://bar"
            assert reapp._preconf["worker_state_db"] == "foo.state"

    def test_pending_configuration__update(self):
        with self.Celery(broker="foo://bar") as app:
            app.conf.update(
                task_default_delivery_mode=44,
                worker_state_db="foo.state",
            )
            assert not app.configured
            assert app.conf.worker_state_db == "foo.state"
            assert app.conf.broker_url == "foo://bar"
            assert app._preconf["worker_state_db"] == "foo.state"

    def test_pending_configuration__compat_settings(self):
        with self.Celery(broker="foo://bar", backend="foo") as app:
            app.conf.update(
                CELERY_ALWAYS_EAGER=4,
                CELERY_DEFAULT_DELIVERY_MODE=63,
                CELERYD_STATE_DB="foo.statez",
            )
            assert app.conf.task_always_eager == 4
            assert app.conf.task_default_delivery_mode == 63
            assert app.conf.worker_state_db == "foo.statez"
            assert app.conf.broker_url == "foo://bar"
            assert app.conf.result_backend == "foo"

    def test_pending_configuration__compat_settings_mixing(self):
        with self.Celery(broker="foo://bar", backend="foo") as app:
            app.conf.update(
                CELERY_ALWAYS_EAGER=4,
                CELERY_DEFAULT_DELIVERY_MODE=63,
                CELERYD_STATE_DB="foo.statez",
                worker_consumer="foo:Fooz",
            )
            with pytest.raises(ImproperlyConfigured):
                assert app.conf.task_always_eager == 4

    def test_pending_configuration__django_settings(self):
        with self.Celery(broker="foo://bar", backend="foo") as app:
            app.config_from_object(
                DictAttribute(
                    Bunch(
                        CELERY_TASK_ALWAYS_EAGER=4,
                        CELERY_TASK_DEFAULT_DELIVERY_MODE=63,
                        CELERY_WORKER_STATE_DB="foo.statez",
                        CELERY_RESULT_SERIALIZER="pickle",
                    )
                ),
                namespace="CELERY",
            )
            assert app.conf.result_serializer == "pickle"
            assert app.conf.CELERY_RESULT_SERIALIZER == "pickle"
            assert app.conf.task_always_eager == 4
            assert app.conf.task_default_delivery_mode == 63
            assert app.conf.worker_state_db == "foo.statez"
            assert app.conf.broker_url == "foo://bar"
            assert app.conf.result_backend == "foo"

    def test_pending_configuration__compat_settings_mixing_new(self):
        with self.Celery(broker="foo://bar", backend="foo") as app:
            app.conf.update(
                task_always_eager=4,
                task_default_delivery_mode=63,
                CELERYD_CONSUMER="foo:Fooz",
            )
            with pytest.raises(ImproperlyConfigured):
                assert app.conf.worker_consumer == "foo:Fooz"

    def test_pending_configuration__compat_settings_mixing_alt(self):
        # An old name alongside the new one for the same setting is the one
        # mix that is allowed, as long as the two agree.
        with self.Celery(broker="foo://bar", backend="foo") as app:
            app.conf.update(
                task_always_eager=4,
                task_default_delivery_mode=63,
                CELERYD_CONSUMER="foo:Fooz",
                worker_consumer="foo:Fooz",
            )

            assert app.conf.worker_consumer == "foo:Fooz"
            assert app.conf.task_always_eager == 4
            assert app.conf.task_default_delivery_mode == 63

    def test_pending_configuration__setdefault(self):
        with self.Celery(broker="foo://bar") as app:
            assert not app.configured
            app.conf.setdefault("worker_state_db", "foo.state")
            assert not app.configured

    def test_pending_configuration__iter(self):
        with self.Celery(broker="foo://bar") as app:
            app.conf.worker_state_db = "foo.state"
            assert not app.configured
            assert list(app.conf.keys())
            assert app.configured
            assert "worker_state_db" in app.conf
            assert dict(app.conf)

    def test_pending_configuration__raises_ImproperlyConfigured(self):
        with self.Celery(set_as_current=False) as app:
            app.conf.worker_state_db = "foo.state"
            app.conf.task_default_delivery_mode = 44
            app.conf.CELERY_ALWAYS_EAGER = 5
            with pytest.raises(ImproperlyConfigured):
                app.finalize()

        with self.Celery() as app:
            assert not self.app.conf.task_always_eager

    def test_pending_configuration__ssl_settings(self):
        with self.Celery(
            broker="foo://bar",
            broker_use_ssl={
                "ssl_cert_reqs": ssl.CERT_REQUIRED,
                "ssl_ca_certs": "/path/to/ca.crt",
                "ssl_certfile": "/path/to/client.crt",
                "ssl_keyfile": "/path/to/client.key",
            },
            redis_backend_use_ssl={
                "ssl_cert_reqs": ssl.CERT_REQUIRED,
                "ssl_ca_certs": "/path/to/ca.crt",
                "ssl_certfile": "/path/to/client.crt",
                "ssl_keyfile": "/path/to/client.key",
            },
        ) as app:
            assert not app.configured
            assert app.conf.broker_url == "foo://bar"
            assert app.conf.broker_use_ssl["ssl_certfile"] == "/path/to/client.crt"
            assert app.conf.broker_use_ssl["ssl_keyfile"] == "/path/to/client.key"
            assert app.conf.broker_use_ssl["ssl_ca_certs"] == "/path/to/ca.crt"
            assert app.conf.broker_use_ssl["ssl_cert_reqs"] == ssl.CERT_REQUIRED
            assert app.conf.redis_backend_use_ssl["ssl_certfile"] == "/path/to/client.crt"
            assert app.conf.redis_backend_use_ssl["ssl_keyfile"] == "/path/to/client.key"
            assert app.conf.redis_backend_use_ssl["ssl_ca_certs"] == "/path/to/ca.crt"
            assert app.conf.redis_backend_use_ssl["ssl_cert_reqs"] == ssl.CERT_REQUIRED

    def test_repr(self):
        assert repr(self.app)

    def test_custom_task_registry(self):
        with self.Celery(tasks=self.app.tasks) as app2:
            assert app2.tasks is self.app.tasks

    def test_include_argument(self):
        with self.Celery(include=("foo", "bar.foo")) as app:
            assert app.conf.include, "foo" == "bar.foo"

    def test_set_as_current(self):
        current = _state._tls.current_app
        try:
            app = self.Celery(set_as_current=True)
            assert _state._tls.current_app is app
        finally:
            _state._tls.current_app = current

    def test_current_task(self):
        @self.app.task
        def foo(shared=False):
            pass

        _state._task_stack.push(foo)
        try:
            assert self.app.current_task.name == foo.name
        finally:
            _state._task_stack.pop()

    def test_task_not_shared(self):
        with patch("celery.app.base.connect_on_app_finalize") as sh:

            @self.app.task(shared=False)
            def foo():
                pass

            sh.assert_not_called()

    def test_task_compat_with_filter(self):
        with self.Celery() as app:
            check = Mock()

            def filter(task):
                check(task)
                return task

            @app.task(filter=filter, shared=False)
            def foo():
                pass

            check.assert_called_with(foo)

    def test_task_with_filter(self):
        with self.Celery() as app:
            check = Mock()

            def filter(task):
                check(task)
                return task

            @app.task(filter=filter, shared=False)
            def foo():
                pass

            check.assert_called_with(foo)

    def test_task_with_pydantic_with_no_args(self):
        """Test a pydantic task with no arguments or return value."""
        with self.Celery() as app:
            check = Mock()

            @app.task(pydantic=True)
            def foo():
                check()

            assert foo() is None
            check.assert_called_once()

    def test_task_with_pydantic_with_arg_and_kwarg(self):
        """Test a pydantic task with simple (non-pydantic) arg/kwarg and return value."""
        with self.Celery() as app:
            check = Mock()

            @app.task(pydantic=True)
            def foo(arg: int, kwarg: bool = True) -> int:
                check(arg, kwarg=kwarg)
                return 1

            assert foo(0) == 1
            check.assert_called_once_with(0, kwarg=True)

    def test_task_with_pydantic_with_optional_args(self):
        """Test pydantic task receiving and returning an optional argument."""
        with self.Celery() as app:
            check = Mock()

            @app.task(pydantic=True)
            def foo(arg: int | None, kwarg: bool | None = True) -> int | None:
                check(arg, kwarg=kwarg)
                if isinstance(arg, int):
                    return 1
                return 2

            assert foo(0) == 1
            check.assert_called_once_with(0, kwarg=True)

            assert foo(None) == 2
            check.assert_called_with(None, kwarg=True)

    def test_task_with_pydantic_with_dict_args(self):
        """Test pydantic task receiving and returning a generic dict argument."""
        with self.Celery() as app:
            check = Mock()

            @app.task(pydantic=True)
            def foo(arg: dict[str, str], kwarg: dict[str, str]) -> dict[str, str]:
                check(arg, kwarg=kwarg)
                return {"x": "y"}

            assert foo({"a": "b"}, kwarg={"c": "d"}) == {"x": "y"}
            check.assert_called_once_with({"a": "b"}, kwarg={"c": "d"})

    def test_task_with_pydantic_with_list_args(self):
        """Test pydantic task receiving and returning a generic dict argument."""
        with self.Celery() as app:
            check = Mock()

            @app.task(pydantic=True)
            def foo(arg: list[str], kwarg: list[str] = True) -> list[str]:
                check(arg, kwarg=kwarg)
                return ["x"]

            assert foo(["a"], kwarg=["b"]) == ["x"]
            check.assert_called_once_with(["a"], kwarg=["b"])

    def test_task_with_pydantic_with_pydantic_arg_and_default_kwarg(self):
        """Test a pydantic task with pydantic arg/kwarg and return value."""

        class ArgModel(BaseModel):
            arg_value: int

        class KwargModel(BaseModel):
            kwarg_value: int

        kwarg_default = KwargModel(kwarg_value=1)

        class ReturnModel(BaseModel):
            ret_value: int

        with self.Celery() as app:
            check = Mock()

            @app.task(pydantic=True)
            def foo(arg: ArgModel, kwarg: KwargModel = kwarg_default) -> ReturnModel:
                check(arg, kwarg=kwarg)
                return ReturnModel(ret_value=2)

            assert foo({"arg_value": 0}) == {"ret_value": 2}
            check.assert_called_once_with(ArgModel(arg_value=0), kwarg=kwarg_default)
            check.reset_mock()

            # Explicitly pass kwarg (but as argument)
            assert foo({"arg_value": 3}, {"kwarg_value": 4}) == {"ret_value": 2}
            check.assert_called_once_with(ArgModel(arg_value=3), kwarg=KwargModel(kwarg_value=4))
            check.reset_mock()

            # Explicitly pass all arguments as kwarg
            assert foo(arg={"arg_value": 5}, kwarg={"kwarg_value": 6}) == {"ret_value": 2}
            check.assert_called_once_with(ArgModel(arg_value=5), kwarg=KwargModel(kwarg_value=6))

    def test_task_with_pydantic_with_non_strict_validation(self):
        """Test a pydantic task with where Pydantic has to apply non-strict validation."""

        class Model(BaseModel):
            value: timedelta

        with self.Celery() as app:
            check = Mock()

            @app.task(pydantic=True)
            def foo(arg: Model) -> Model:
                check(arg)
                return Model(value=timedelta(days=arg.value.days * 2))

            assert foo({"value": timedelta(days=1)}) == {"value": "P2D"}
            check.assert_called_once_with(Model(value=timedelta(days=1)))
            check.reset_mock()

            # Pass a serialized value to the task
            assert foo({"value": "P3D"}) == {"value": "P6D"}
            check.assert_called_once_with(Model(value=timedelta(days=3)))

    def test_task_with_pydantic_with_optional_pydantic_args(self):
        """Test pydantic task receiving and returning an optional argument."""

        class ArgModel(BaseModel):
            arg_value: int

        class KwargModel(BaseModel):
            kwarg_value: int

        class ReturnModel(BaseModel):
            ret_value: int

        with self.Celery() as app:
            check = Mock()

            @app.task(pydantic=True)
            def foo(arg: ArgModel | None, kwarg: KwargModel | None = None) -> ReturnModel | None:
                check(arg, kwarg=kwarg)
                if isinstance(arg, ArgModel):
                    return ReturnModel(ret_value=1)
                return None

            assert foo(None) is None
            check.assert_called_once_with(None, kwarg=None)

            assert foo({"arg_value": 1}, kwarg={"kwarg_value": 2}) == {"ret_value": 1}
            check.assert_called_with(ArgModel(arg_value=1), kwarg=KwargModel(kwarg_value=2))

    def test_task_with_pydantic_with_generic_return_value(self):
        """Test pydantic task receiving and returning an optional argument."""

        class ReturnModel(BaseModel):
            ret_value: int

        with self.Celery() as app:
            check = Mock()

            @app.task(pydantic=True)
            def foo() -> dict[str, str]:
                check()
                return ReturnModel(ret_value=1)  # type: ignore  # whole point here is that this doesn't match

            assert foo() == ReturnModel(ret_value=1)
            check.assert_called_once_with()

    def test_task_with_pydantic_with_task_name_in_context(self):
        """Test that the task name is passed to as additional context."""

        class ArgModel(BaseModel):
            value: int

            @model_validator(mode="after")
            def validate_context(self, info: ValidationInfo):
                context = info.context
                assert context
                assert context.get("celery_task_name") == "tests.unit.app.test_app.task"
                return self

        with self.Celery() as app:
            check = Mock()

            @app.task(pydantic=True)
            def task(arg: ArgModel):
                check(arg)
                return 1

            assert task({"value": 1}) == 1

    def test_task_with_pydantic_with_strict_validation(self):
        """Test a pydantic task with/without strict model validation."""

        class ArgModel(BaseModel):
            value: int

        with self.Celery() as app:
            check = Mock()

            @app.task(pydantic=True, pydantic_strict=True)
            def strict(arg: ArgModel):
                check(arg)

            @app.task(pydantic=True, pydantic_strict=False)
            def loose(arg: ArgModel):
                check(arg)

            # In Pydantic, passing an "exact int" as float works without strict validation
            assert loose({"value": 1.0}) is None
            check.assert_called_once_with(ArgModel(value=1))
            check.reset_mock()

            # ... but a non-strict value will raise an exception
            with pytest.raises(ValueError):
                loose({"value": 1.1})
            check.assert_not_called()

            # ... with strict validation, even an "exact int" will not work:
            with pytest.raises(ValueError):
                strict({"value": 1.0})
            check.assert_not_called()

    def test_task_with_pydantic_with_extra_context(self):
        """Test passing additional validation context to the model."""

        class ArgModel(BaseModel):
            value: int

            @model_validator(mode="after")
            def validate_context(self, info: ValidationInfo):
                context = info.context
                assert context, context
                assert context.get("foo") == "bar"
                return self

        with self.Celery() as app:
            check = Mock()

            @app.task(pydantic=True, pydantic_context={"foo": "bar"})
            def task(arg: ArgModel):
                check(arg.value)
                return 1

            assert task({"value": 1}) == 1
            check.assert_called_once_with(1)

    def test_task_with_pydantic_with_dump_kwargs(self):
        """Test passing keyword arguments to model_dump()."""

        class ArgModel(BaseModel):
            value: int

        class RetModel(BaseModel):
            value: datetime
            unset_value: int | None = 99  # this would be in the output, if exclude_unset weren't True

        with self.Celery() as app:
            check = Mock()

            @app.task(pydantic=True, pydantic_dump_kwargs={"mode": "python", "exclude_unset": True})
            def task(arg: ArgModel) -> RetModel:
                check(arg)
                return RetModel(value=datetime(2024, 5, 14, tzinfo=timezone.utc))

            assert task({"value": 1}) == {"value": datetime(2024, 5, 14, tzinfo=timezone.utc)}
            check.assert_called_once_with(ArgModel(value=1))

    async def test_task_with_pydantic_with_async_task(self):
        """An async pydantic task validates and dumps around the awaited body."""

        class ArgModel(BaseModel):
            value: int

        class RetModel(BaseModel):
            value: int

        with self.Celery() as app:
            check = Mock()

            @app.task(pydantic=True)
            async def task(arg: ArgModel) -> RetModel:
                check(arg)
                return RetModel(value=arg.value + 1)

            assert await task.arun({"value": 1}) == {"value": 2}
            assert await task({"value": 2}) == {"value": 3}
            assert check.call_args_list == [call(ArgModel(value=1)), call(ArgModel(value=2))]

    def test_task_with_pydantic_with_pydantic_not_installed(self):
        """Test configuring a task with Pydantic when pydantic is not installed."""

        with self.Celery() as app:

            @app.task(pydantic=True)
            def task():
                return

            # mock function will raise ModuleNotFoundError only if pydantic is imported
            def import_module(name, *args, **kwargs):
                if name == "pydantic":
                    raise ModuleNotFoundError("Module not found.")
                return DEFAULT

            msg = r"^You need to install pydantic to use pydantic model serialization\.$"
            with (
                patch(
                    "celery.app.base.importlib.import_module", side_effect=import_module, wraps=importlib.import_module
                ),
                pytest.raises(ImproperlyConfigured, match=msg),
            ):
                task()

    def test_can_get_type_hints_for_tasks(self):

        with self.Celery() as app:

            @app.task
            def foo(parameter: int) -> None:
                pass

            assert typing.get_type_hints(foo) == {"parameter": int, "return": type(None)}

    def test_task_with_type_checking_annotation(self):
        # Registering a task read `fun.__annotations__`, which under PEP 649
        # resolves them -- so a parameter annotated with a type imported only
        # under TYPE_CHECKING raised NameError at decoration time. Fall back to
        # the strings when they cannot be resolved (upstream e49270e35).
        namespace = {}
        exec(compile("def foo(args: Sequence[str], x: int = 0): return args", "<deferred>", "exec"), namespace)

        with self.Celery() as app:
            task = app.task(namespace["foo"])

            assert task.apply(args=(["hello"],)).result == ["hello"]
            assert task.__annotations__["args"] == "Sequence[str]"

    def test_annotate_decorator(self):
        from celery.app.task import Task

        class adX(Task):
            def run(self, y, z, x):
                return y, z, x

        check = Mock()

        def deco(fun):

            def _inner(*args, **kwargs):
                check(*args, **kwargs)
                return fun(*args, **kwargs)

            return _inner

        self.app.conf.task_annotations = {adX.name: {"@__call__": deco}}
        adX.bind(self.app)
        assert adX.app is self.app

        i = adX()
        i(2, 4, x=3)
        check.assert_called_with(i, 2, 4, x=3)

        i.annotate()
        i.annotate()

    def test_apply_async_adds_children(self):
        from celery._state import _task_stack

        @self.app.task(bind=True, shared=False)
        def a3cX1(self):
            pass

        @self.app.task(bind=True, shared=False)
        def a3cX2(self):
            pass

        _task_stack.push(a3cX1)
        try:
            a3cX1.push_request(called_directly=False)
            try:
                res = a3cX2.apply_async(add_to_parent=True)
                assert res in a3cX1.request.children
            finally:
                a3cX1.pop_request()
        finally:
            _task_stack.pop()

    def test_pickle_app(self):
        changes = {"THE_FOO_BAR": "bars", "THE_MII_MAR": "jars"}
        self.app.conf.update(changes)
        saved = pickle.dumps(self.app)
        assert len(saved) < 2048
        restored = pickle.loads(saved)
        for key, value in changes.items():
            assert restored.conf[key] == value

    @patch("celery.bin.celery.celery")
    def test_worker_main(self, mocked_celery):
        self.app.worker_main(argv=["worker", "--help"])

        mocked_celery.main.assert_called_with(args=["worker", "--help"], standalone_mode=False)

    def test_config_from_envvar(self, monkeypatch):
        monkeypatch.setenv("CELERYTEST_CONFIG_OBJECT", "tests.unit.app.test_app")
        self.app.config_from_envvar("CELERYTEST_CONFIG_OBJECT")
        assert self.app.conf.THIS_IS_A_KEY == "this is a value"

    def assert_config2(self):
        assert self.app.conf.LEAVE_FOR_WORK
        assert self.app.conf.MOMENT_TO_STOP
        assert self.app.conf.CALL_ME_BACK == 123456789
        assert not self.app.conf.WANT_ME_TO
        assert self.app.conf.UNDERSTAND_ME

    def test_config_from_object__lazy(self):
        conf = ObjectConfig2()
        self.app.config_from_object(conf)
        assert self.app.loader._conf is unconfigured
        assert self.app._config_source is conf

        self.assert_config2()

    def test_config_from_object__force(self):
        self.app.config_from_object(ObjectConfig2(), force=True)
        assert self.app.loader._conf

        self.assert_config2()

    def test_config_from_object__compat(self):

        class Config:
            CELERY_ALWAYS_EAGER = 44
            CELERY_DEFAULT_DELIVERY_MODE = 30
            CELERY_TASK_PUBLISH_RETRY = False

        self.app.config_from_object(Config)
        assert self.app.conf.task_always_eager == 44
        assert self.app.conf.CELERY_ALWAYS_EAGER == 44
        assert not self.app.conf.task_publish_retry
        assert self.app.conf.task_default_routing_key == "testcelery"

    def test_config_from_object__supports_old_names(self):

        class Config:
            task_always_eager = 45
            task_default_delivery_mode = 301

        self.app.config_from_object(Config())
        assert self.app.conf.CELERY_ALWAYS_EAGER == 45
        assert self.app.conf.task_always_eager == 45
        assert self.app.conf.CELERY_DEFAULT_DELIVERY_MODE == 301
        assert self.app.conf.task_default_delivery_mode == 301
        assert self.app.conf.task_default_routing_key == "testcelery"

    def test_config_from_object__namespace_uppercase(self):

        class Config:
            CELERY_TASK_ALWAYS_EAGER = 44
            CELERY_TASK_DEFAULT_DELIVERY_MODE = 301

        self.app.config_from_object(Config(), namespace="CELERY")
        assert self.app.conf.task_always_eager == 44

    def test_config_from_object__namespace_lowercase(self):

        class Config:
            celery_task_always_eager = 44
            celery_task_default_delivery_mode = 301

        self.app.config_from_object(Config(), namespace="celery")
        assert self.app.conf.task_always_eager == 44

    def test_config_from_object__mixing_new_and_old(self):

        class Config:
            task_always_eager = 44
            worker_state_db = "foo.state"
            worker_consumer = "foo:Consumer"
            beat_schedule = "/foo/schedule"
            CELERY_DEFAULT_DELIVERY_MODE = 301

        with pytest.raises(ImproperlyConfigured) as exc:
            self.app.config_from_object(Config(), force=True)
            assert exc.args[0].startswith("CELERY_DEFAULT_DELIVERY_MODE")
            assert "task_default_delivery_mode" in exc.args[0]

    def test_config_from_object__mixing_old_and_new(self):

        class Config:
            CELERY_ALWAYS_EAGER = 46
            CELERYD_STATE_DB = "foo.state"
            CELERYD_CONSUMER = "foo:Consumer"
            CELERYBEAT_SCHEDULE = "/foo/schedule"
            task_default_delivery_mode = 301

        with pytest.raises(ImproperlyConfigured) as exc:
            self.app.config_from_object(Config(), force=True)
            assert exc.args[0].startswith("task_default_delivery_mode")
            assert "CELERY_DEFAULT_DELIVERY_MODE" in exc.args[0]

    def test_config_form_object__module_attr_does_not_exist(self):
        module_name = __name__
        attr_name = "bar"
        # the module must exist, but it should not have the config attr
        self.app.config_from_object(f"{module_name}.{attr_name}")

        with pytest.raises(ModuleNotFoundError) as exc:
            assert self.app.conf.broker_url is None

        assert module_name in exc.value.args[0]
        assert attr_name in exc.value.args[0]

    def test_config_from_cmdline(self):
        cmdline = [
            "task_always_eager=no",
            "result_backend=/dev/null",
            "worker_prefetch_multiplier=368",
            ".foobarstring=(string)300",
            ".foobarint=(int)300",
            'result_backend_transport_options=(dict){"foo": "bar"}',
        ]
        self.app.config_from_cmdline(cmdline, namespace="worker")
        assert not self.app.conf.task_always_eager
        assert self.app.conf.result_backend == "/dev/null"
        assert self.app.conf.worker_prefetch_multiplier == 368
        assert self.app.conf.worker_foobarstring == "300"
        assert self.app.conf.worker_foobarint == 300
        assert self.app.conf.result_backend_transport_options == {"foo": "bar"}

    def test_setting__broker_transport_options(self):

        _args = {"foo": "bar", "spam": "baz"}

        self.app.config_from_object(Bunch())
        assert self.app.conf.broker_transport_options == {"polling_interval": 0.1}

        self.app.config_from_object(Bunch(broker_transport_options=_args))
        assert self.app.conf.broker_transport_options == _args

    def test_Windows_log_color_disabled(self):
        self.app.IS_WINDOWS = True
        assert not self.app.log.supports_color(True)

    def test_WorkController(self):
        x = self.app.WorkController
        assert x.app is self.app

    def test_Worker(self):
        x = self.app.Worker
        assert x.app is self.app

    @pytest.mark.usefixtures("depends_on_current_app")
    def test_AsyncResult(self):
        x = self.app.AsyncResult("1")
        assert x.app is self.app
        r = loads(dumps(x))
        # not set as current, so ends up as default app after reduce
        assert r.app is current_app._get_current_object()

    def test_get_active_apps(self):
        assert list(_state._get_active_apps())

        app1 = self.Celery()
        appid = id(app1)
        assert app1 in _state._get_active_apps()
        app1.close()
        del app1

        gc.collect()

        # weakref removed from list when app goes out of scope.
        with pytest.raises(StopIteration):
            next(app for app in _state._get_active_apps() if id(app) == appid)

    def test_config_from_envvar_more(self, key="CELERY_HARNESS_CFG1"):
        assert not self.app.config_from_envvar("HDSAJIHWIQHEWQU", force=True, silent=True)
        with pytest.raises(ImproperlyConfigured):
            self.app.config_from_envvar(
                "HDSAJIHWIQHEWQU",
                force=True,
                silent=False,
            )
        os.environ[key] = __name__ + ".object_config"
        assert self.app.config_from_envvar(key, force=True)
        assert self.app.conf["FOO"] == 1
        assert self.app.conf["BAR"] == 2

        os.environ[key] = "unknown_asdwqe.asdwqewqe"
        with pytest.raises(ImportError):
            self.app.config_from_envvar(key, silent=False)
        assert not self.app.config_from_envvar(key, force=True, silent=True)

        os.environ[key] = __name__ + ".dict_config"
        assert self.app.config_from_envvar(key, force=True)
        assert self.app.conf["FOO"] == 10
        assert self.app.conf["BAR"] == 20

    @patch("celery.bin.celery.celery")
    def test_start(self, mocked_celery):
        self.app.start()
        mocked_celery.main.assert_called()

    def test_get_broker_info(self):
        info = self.app.connection("redis://localhost").info()
        assert info["hostname"] == "localhost"

    def test_canvas(self):
        assert self.app._canvas.Signature

    def test_signature(self):
        sig = self.app.signature("foo", (1, 2))
        assert sig.app is self.app

    def test_timezone_none_set(self):
        self.app.conf.timezone = None
        self.app.conf.enable_utc = True
        assert self.app.timezone == timezone.utc
        del self.app.timezone
        self.app.conf.enable_utc = False
        assert self.app.timezone == timezone.local

    def test_use_local_timezone(self):
        self.app.conf.timezone = None
        self.app.conf.enable_utc = False

        self._clear_timezone_cache()
        try:
            assert isinstance(self.app.timezone, ZoneInfo)
        finally:
            self._clear_timezone_cache()

    @patch("celery.utils.time.get_localzone")
    def test_use_local_timezone_failure(self, mock_get_localzone):
        mock_get_localzone.side_effect = Exception("Failed to get local timezone")
        self.app.conf.timezone = None
        self.app.conf.enable_utc = False

        self._clear_timezone_cache()
        try:
            assert isinstance(self.app.timezone, LocalTimezone)
        finally:
            self._clear_timezone_cache()

    def _clear_timezone_cache(self):
        del self.app.timezone
        del timezone.local

    def test_uses_utc_timezone(self):
        self.app.conf.timezone = None
        self.app.conf.enable_utc = True
        assert self.app.uses_utc_timezone() is True

        self.app.conf.enable_utc = False
        del self.app.timezone
        assert self.app.uses_utc_timezone() is False

        self.app.conf.timezone = "US/Eastern"
        del self.app.timezone
        assert self.app.uses_utc_timezone() is False

        self.app.conf.timezone = "UTC"
        del self.app.timezone
        assert self.app.uses_utc_timezone() is True

    def test_compat_on_configure(self):
        _on_configure = Mock(name="on_configure")

        class CompatApp(Celery):
            def on_configure(self, *args, **kwargs):
                # on pypy3 if named on_configure the class function
                # will be called, instead of the mock defined above,
                # so we add the underscore.
                _on_configure(*args, **kwargs)

        with CompatApp(set_as_current=False) as app:
            app.loader = Mock()
            app.loader.conf = {}
            app._load_config()
            _on_configure.assert_called_with()

    def test_add_periodic_task(self):

        @self.app.task
        def add(x, y):
            pass

        assert not self.app.configured
        self.app.add_periodic_task(
            10,
            self.app.signature("add", (2, 2)),
            name="add1",
            expires=3,
        )
        assert self.app._pending_periodic_tasks
        assert not self.app.configured

        sig2 = add.s(4, 4)
        assert self.app.configured
        self.app.add_periodic_task(20, sig2, name="add2", expires=4)
        assert "add1" in self.app.conf.beat_schedule
        assert "add2" in self.app.conf.beat_schedule

    def test_add_periodic_task_expected_override(self):

        @self.app.task
        def add(x, y):
            pass

        sig = add.s(2, 2)
        self.app.add_periodic_task(10, sig, name="add1", expires=3)
        self.app.add_periodic_task(20, sig, name="add1", expires=3)
        assert "add1" in self.app.conf.beat_schedule
        assert len(self.app.conf.beat_schedule) == 1

    def test_add_periodic_task_unexpected_override(self, caplog):

        @self.app.task
        def add(x, y):
            pass

        sig = add.s(2, 2)
        self.app.add_periodic_task(10, sig, expires=3)
        self.app.add_periodic_task(20, sig, expires=3)

        assert len(self.app.conf.beat_schedule) == 1
        assert caplog.records[0].message == (
            "Periodic task key='tests.unit.app.test_app.add(2, 2)' shadowed a"
            " previous unnamed periodic task. Pass a name kwarg to"
            " add_periodic_task to silence this warning."
        )

    def test_bugreport(self):
        assert self.app.bugreport()

    def test_select_queues(self):
        self.app.amqp = Mock(name="amqp")
        self.app.select_queues({"foo", "bar"})
        self.app.amqp.queues.select.assert_called_with({"foo", "bar"})

    def test_Beat(self):
        from celery.apps.beat import Beat

        beat = self.app.Beat()
        assert isinstance(beat, Beat)

    def test_registry_cls(self):

        class TaskRegistry(self.app.registry_cls):
            pass

        class CustomCelery(type(self.app)):
            registry_cls = TaskRegistry

        app = CustomCelery(set_as_current=False)
        assert isinstance(app.tasks, TaskRegistry)

    def test_oid(self):
        # Test that oid is global value.
        oid1 = self.app.oid
        oid2 = self.app.oid
        uuid.UUID(oid1)
        uuid.UUID(oid2)
        assert oid1 == oid2

    def test_global_oid(self):
        # Test that oid is global value also within threads
        main_oid = self.app.oid
        uuid.UUID(main_oid)
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(lambda: self.app.oid)
        thread_oid = future.result()
        uuid.UUID(thread_oid)
        assert main_oid == thread_oid

    def test_thread_oid(self):
        # Test that thread_oid is global value in single thread.
        oid1 = self.app.thread_oid
        oid2 = self.app.thread_oid
        uuid.UUID(oid1)
        uuid.UUID(oid2)
        assert oid1 == oid2

    def test_backend(self):
        # Test that app.backend returns the same backend in single thread
        backend1 = self.app.backend
        backend2 = self.app.backend
        assert isinstance(backend1, Backend)
        assert isinstance(backend2, Backend)
        assert backend1 is backend2

    def test_thread_backend(self):
        # Test that app.backend returns the new backend for each thread
        main_backend = self.app.backend
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(lambda: self.app.backend)
        thread_backend = future.result()
        assert isinstance(main_backend, Backend)
        assert isinstance(thread_backend, Backend)
        assert main_backend is not thread_backend

    def test_thread_oid_is_local(self):
        # Test that thread_oid is local to thread.
        main_oid = self.app.thread_oid
        uuid.UUID(main_oid)
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(lambda: self.app.thread_oid)
        thread_oid = future.result()
        uuid.UUID(thread_oid)
        assert main_oid != thread_oid

    def test_thread_backend_thread_safe(self):
        # Should share the backend object across threads
        from concurrent.futures import ThreadPoolExecutor

        with self.Celery() as app:
            app.conf.update(result_backend_thread_safe=True)
            main_backend = app.backend
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(lambda: app.backend)

            thread_backend = future.result()
            assert isinstance(main_backend, Backend)
            assert isinstance(thread_backend, Backend)
            assert main_backend is thread_backend

    def test_send_task_expire_as_string(self):
        try:
            self.app.send_task("foo", (1, 2), expires="2023-03-16T17:21:20.663973")
        except TypeError as e:
            pytest.fail(f"raise unexcepted error {e}")


class test_countdown_on_a_quorum_queue:
    """countdown and eta with a quorum queue reached a stub that raised.

    Native delayed delivery routes the message through a chain of RabbitMQ
    delay exchanges, which this fork does not build. What is left is the eta
    header, which the worker holds the message on until.
    """

    def _app(self):
        app = self.Celery(set_as_current=False, broker="amqp://guest@localhost//")
        app.conf.task_queues = [
            Queue(
                "q",
                Exchange("topic-ex", type="topic"),
                routing_key="q",
                queue_arguments={"x-queue-type": "quorum"},
            ),
        ]

        @app.task(name="t.quorum", shared=False)
        def t():
            pass

        return app

    @pytest.mark.parametrize(
        "options",
        [{"countdown": 30}, {"eta": datetime(2030, 1, 1, tzinfo=UTC)}],
        ids=["countdown", "eta"],
    )
    def test_it_publishes_to_the_queue_with_an_eta(self, options):
        app = self._app()

        with patch.object(app, "_send_task_message") as send:
            app.send_task("t.quorum", queue="q", **options)

        message = send.call_args.args[2]
        assert message.headers["eta"]
        assert send.call_args.kwargs["queue"].name == "q"


class test_send_task_exec_options:
    """`send_task("name")` picks up the options the task declares for itself.

    Until upstream fbd01579c the task's own serializer, queue, compression and
    so on only applied through `apply_async`; calling the same task by name
    silently fell back to the app defaults (upstream #8542).
    """

    def _send(self, name, **kwargs):
        """Send by name with the broker stubbed out, and report what was built."""
        self.app.finalize()
        router = Mock(name="router")
        router.route.side_effect = lambda options, *args, **kw: options
        self.app.amqp = Mock(name="amqp")
        with patch.object(self.app, "_send_task_message") as send_message:
            self.app.send_task(name, (1,), router=router, **kwargs)
        message = self.app.amqp.create_task_message
        message.assert_called_once()
        # Named parameters rather than positional indexes, so this does not
        # break the next time a field is inserted into as_task_v2.
        bound = inspect.signature(AMQP.as_task_v2).bind(None, *message.call_args.args, **message.call_args.kwargs)
        return bound.arguments, message.call_args.kwargs, send_message.call_args.kwargs

    def test_serializer_comes_from_the_task(self):
        @self.app.task(name="t.serializer", serializer="json", shared=False)
        def t():
            pass

        self.app.conf.task_serializer = "msgpack"
        _, _, published = self._send("t.serializer")
        assert published["serializer"] == "json"

    def test_explicit_serializer_beats_the_task(self):
        @self.app.task(name="t.serializer2", serializer="json", shared=False)
        def t():
            pass

        _, _, published = self._send("t.serializer2", serializer="pickle")
        assert published["serializer"] == "pickle"

    def test_a_name_this_process_does_not_know_still_sends(self):
        # The whole point of send_task is naming a task that lives elsewhere.
        _, _, published = self._send("not.registered.anywhere")
        assert "serializer" not in published

    def test_it_does_not_finalize_the_app(self):
        # Looking the name up must not force finalization, which would raise
        # under autofinalize=False.
        app = self.Celery(set_as_current=False, autofinalize=False)
        app.conf.broker_url = "memory://"
        app.amqp = Mock(name="amqp")
        router = Mock(name="router")
        router.route.side_effect = lambda options, *args, **kw: options
        with patch.object(app, "_send_task_message"):
            app.send_task("not.registered.anywhere", (1,), router=router)
        assert not app.finalized

    @pytest.mark.parametrize("option", ["time_limit", "soft_time_limit", "expires"])
    def test_task_level_value_is_used_and_not_duplicated(self, option):
        # These three are named parameters of as_task_v2 *and* keys in
        # _get_exec_options(), so before the fix this was "got multiple values
        # for argument".
        @self.app.task(name=f"t.{option}", shared=False, **{option: 300})
        def t():
            pass

        arguments, as_kwargs, _ = self._send(f"t.{option}")
        assert arguments[option] == 300
        assert option not in as_kwargs

    @pytest.mark.parametrize("option", ["time_limit", "soft_time_limit", "expires"])
    def test_explicit_value_beats_the_task(self, option):
        @self.app.task(name=f"t.{option}.override", shared=False, **{option: 300})
        def t():
            pass

        arguments, _, _ = self._send(f"t.{option}.override", **{option: 60})
        assert arguments[option] == 60

    @pytest.mark.parametrize("option", ["time_limit", "soft_time_limit", "expires"])
    def test_explicit_none_clears_the_task_value(self, option):
        # This is why the defaults are a sentinel and not None: otherwise
        # "clear the task's 300" is indistinguishable from "did not say".
        @self.app.task(name=f"t.{option}.clear", shared=False, **{option: 300})
        def t():
            pass

        arguments, _, _ = self._send(f"t.{option}.clear", **{option: None})
        assert arguments[option] is None

    def test_apply_async_is_left_alone(self):
        # apply_async merges exec options itself and then passes task_type, so
        # merging again here would clobber whatever it just decided.
        @self.app.task(name="t.viaapply", shared=False, time_limit=300)
        def t(x):
            pass

        self.app.finalize()
        with patch.object(self.app, "send_task") as send_task:
            t.apply_async((1,), time_limit=60)
        assert send_task.call_args.kwargs["time_limit"] == 60
        # Non-None task_type is what tells _prepare_task_message to skip.
        assert send_task.call_args.kwargs["task_type"].name == "t.viaapply"

    def test_a_class_left_in_the_registry_is_skipped(self):
        # `_get_exec_options` is an unbound function on a class, so calling it
        # would raise. Registered names normally hold instances.
        self.app.finalize()
        with patch.dict(self.app._tasks, {"t.raw.class": Task}):
            _, _, published = self._send("t.raw.class")
        assert "serializer" not in published


class test_broker_connection_reuse:
    """One broker connection per app and event loop, not one per message.

    `_asend_task_message` used to call `connection_for_write()` itself, and the
    sync path ran it through `async_to_sync`, which builds a throwaway loop per
    call. A transport belongs to the loop that opened it, so every published
    message opened a connection and abandoned it unclosed.
    """

    def _counting_connect(self, connected):
        real_connect = Connection.connect

        async def connect(conn, *args, **kwargs):
            connected.append(conn)
            return await real_connect(conn, *args, **kwargs)

        return patch.object(Connection, "connect", connect)

    def _app(self, name):
        app = self.Celery(set_as_current=False, broker="memory://")

        @app.task(name=name, shared=False)
        def t():
            pass

        return app

    def test_repeated_sync_sends_share_one_connection(self):
        app = self._app("t.reuse.sync")
        connected = []

        with self._counting_connect(connected):
            for _ in range(3):
                app.send_task("t.reuse.sync")

        assert len(connected) == 1
        assert len(app._async_connections) == 1

    async def test_repeated_async_sends_share_one_connection(self):
        app = self._app("t.reuse.async")
        connected = []

        with self._counting_connect(connected):
            for _ in range(3):
                await app.asend_task("t.reuse.async")

        assert len(connected) == 1
        assert app._async_connections[asyncio.get_running_loop()] is connected[0]

    async def test_a_caller_with_its_own_loop_gets_its_own_connection(self):
        # Not an inefficiency to fix later: a transport opened on the
        # background loop cannot be driven from this one.
        app = self._app("t.reuse.both")
        await app.asend_task("t.reuse.both")
        await asyncio.to_thread(app.send_task, "t.reuse.both")

        assert len(app._async_connections) == 2

    def test_a_closing_loop_hands_its_connection_back(self):
        # A transport belongs to the loop that opened it, so a loop that
        # closes with its connection still cached leaves a socket that
        # nothing can close: one per asyncio.run().
        app = self._app("t.reuse.loop")
        connected = []

        with self._counting_connect(connected):
            asyncio.run(app.asend_task("t.reuse.loop"))

        assert len(connected) == 1
        assert not connected[0].is_connected
        assert not app._async_connections

    def test_send_task_publishes_on_the_connection_it_is_given(self):
        app = self._app("t.reuse.given")
        given = app.connection_for_write()
        connected = []

        with self._counting_connect(connected):
            app.send_task("t.reuse.given", connection=given)

        assert connected == [given]
        assert not app._async_connections

    async def test_asend_task_publishes_on_the_connection_it_is_given(self):
        app = self._app("t.reuse.agiven")
        given = app.connection_for_write()
        connected = []

        with self._counting_connect(connected):
            await app.asend_task("t.reuse.agiven", connection=given)

        assert connected == [given]
        assert not app._async_connections

    def test_send_task_publishes_with_the_producer_it_is_given(self):
        app = self._app("t.reuse.producer")
        given = app.amqp.Producer(app.connection_for_write())
        published = []

        with patch.object(app.amqp, "asend_task_message") as send:
            send.side_effect = lambda producer, *a, **kw: published.append(producer)
            app.send_task("t.reuse.producer", producer=given)

        assert published == [given]

    def test_a_group_publishes_with_the_producer_it_is_given(self):
        app = self._app("t.reuse.group")
        given = app.amqp.Producer(app.connection_for_write())
        published = []

        with patch.object(app.amqp, "asend_task_message") as send:
            send.side_effect = lambda producer, *a, **kw: published.append(producer)
            group([app.signature("t.reuse.group")] * 2).apply_async(producer=given)

        assert published == [given, given]

    async def test_an_async_group_publishes_with_the_producer_it_is_given(self):
        app = self._app("t.reuse.agroup")
        given = app.amqp.Producer(app.connection_for_write())
        published = []

        with patch.object(app.amqp, "asend_task_message") as send:
            send.side_effect = lambda producer, *a, **kw: published.append(producer)
            await group([app.signature("t.reuse.agroup")] * 2).aapply_async(producer=given)

        assert published == [given, given]

    def test_close_hands_the_connection_back(self):
        app = self._app("t.reuse.close")
        app.send_task("t.reuse.close")
        connection = next(iter(app._async_connections.values()))

        # Closing is a coroutine and has to run on the loop that owns the
        # transport, so `close()` hands it over and waits for it there.
        app.close()

        assert not connection.is_connected
        assert not app._async_connections


class test_connection_options:
    """What reaches the transport when a connection is built."""

    def _app(self, broker, **conf):
        app = self.Celery(set_as_current=False, broker=broker)
        app.conf.update(conf)
        return app

    def test_the_heartbeat_setting_reaches_an_amqp_connection(self):
        app = self._app("amqp://guest:guest@h:5672//", broker_heartbeat=17)
        assert app.connection_for_write()._transport_options["heartbeat"] == 17

    def test_an_explicit_heartbeat_wins_over_the_setting(self):
        app = self._app("amqp://guest:guest@h:5672//", broker_heartbeat=17)
        assert app.connection(heartbeat=3)._transport_options["heartbeat"] == 3

    def test_transport_options_win_over_the_setting(self):
        app = self._app(
            "amqp://guest:guest@h:5672//",
            broker_heartbeat=17,
            broker_transport_options={"heartbeat": 5},
        )
        assert app.connection_for_write()._transport_options["heartbeat"] == 5

    def test_a_redis_connection_is_left_without_a_heartbeat(self):
        # Redis has no protocol-level heartbeat, and its transport hands
        # every option it does not know to redis-py, which rejects this one.
        app = self._app("redis://h:6379/13", broker_heartbeat=17)
        assert "heartbeat" not in app.connection_for_write()._transport_options

    def test_the_url_carries_the_credentials(self):
        app = self._app("amqp://me:s3cret@h:5672/vhost")
        assert app.connection_for_write().as_uri(include_password=True) == "amqp://me:s3cret@h:5672/vhost"

    def test_a_read_connection_uses_the_read_url(self):
        app = self._app("amqp://h//", broker_read_url="amqp://reader//")
        assert app.connection_for_read().as_uri() == "amqp://reader//"


class test_defaults:
    def test_strtobool(self):
        for s in ("false", "no", "0"):
            assert not defaults.strtobool(s)
        for s in ("true", "yes", "1"):
            assert defaults.strtobool(s)
        with pytest.raises(TypeError):
            defaults.strtobool("unsure")


class test_debugging_utils:
    def test_enable_disable_trace(self):
        try:
            _app.enable_trace()
            assert _state.app_or_default == _state._app_or_default_trace
            _app.disable_trace()
            assert _state.app_or_default == _state._app_or_default
        finally:
            _app.disable_trace()


class test_pyimplementation:
    def test_platform_python_implementation(self):
        with conftest.platform_pyimp(lambda: "Xython"):
            assert pyimplementation() == "Xython"


class test_shared_task:
    def test_registers_to_all_apps(self):
        with self.Celery("xproj", set_as_current=True) as xproj:
            xproj.finalize()

            @shared_task
            def foo():
                return 42

            @shared_task()
            def bar():
                return 84

            assert foo.app is xproj
            assert bar.app is xproj
            assert foo._get_current_object()

            with self.Celery("yproj", set_as_current=True) as yproj:
                assert foo.app is yproj
                assert bar.app is yproj

                @shared_task()
                def baz():
                    return 168

                assert baz.app is yproj
