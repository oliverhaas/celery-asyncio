import contextlib
import typing
from typing import get_args

import pytest

from celery.app import Celery
from celery.app.task import Context, Task
from celery.canvas import Signature
from celery.local import class_property
from celery.result import AsyncResult
from celery.utils.objects import FallbackContext
from celery.utils.threads import _LocalStack


class test_Generics:
    """Subscripting these does nothing at runtime beyond building a GenericAlias.

    It is what lets a stubs package annotate `app: Celery[CustomTask]` without
    the annotation blowing up when it is evaluated (upstream 7ca0e0f18).
    """

    def test_Celery__class_getitem__(self):
        app = Celery[Task]()
        assert isinstance(app, Celery)
        assert get_args(Celery[Task]) == (Task,)

    def test_Task__class_getitem__(self):
        task = Task[[int], str]()
        assert isinstance(task, Task)
        assert get_args(Task[[int], str]) == ([int], str)

    @pytest.mark.usefixtures("depends_on_current_app")
    def test_AsyncResult__class_getitem__(self):
        result = AsyncResult[str]("some-id")
        assert isinstance(result, AsyncResult)
        assert get_args(AsyncResult[str]) == (str,)

    def test_Signature__class_getitem__(self):
        # Signature subclasses dict, so it already had this. Pinned so that
        # changing the base class does not quietly take it away again.
        s = Signature[str]()
        assert isinstance(s, Signature)
        assert get_args(Signature[str]) == (str,)

    def test__LocalStack__class_getitem__(self):
        stack = _LocalStack[Context]()
        assert isinstance(stack, _LocalStack)
        assert get_args(_LocalStack[Context]) == (Context,)

    def test_FallbackContext__class_getitem__(self):
        @contextlib.contextmanager
        def make_thing(int_count):
            yield f"dynamic_thing_{int_count}"

        thing_manager = FallbackContext[str, [int]]("static_thing", make_thing)
        assert isinstance(thing_manager, FallbackContext)
        assert get_args(FallbackContext[str, [int]]) == (str, [int])

    def test_class_property__class_getitem__(self):
        class Thing:
            def _get_my_prop(self):
                return "hello"

            def _set_my_prop(self, str_value):
                pass

            my_prop = class_property[typing.Self, str](_get_my_prop, _set_my_prop)

        assert isinstance(Thing.__dict__["my_prop"], class_property)
        assert Thing.my_prop == "hello"
        assert get_args(class_property[Thing, str]) == (Thing, str)
