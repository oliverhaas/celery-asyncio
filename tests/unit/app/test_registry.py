import pytest

from celery.app.registry import _unpickle_task, _unpickle_task_v2
from celery.exceptions import InvalidTaskError


def returns():
    return 1


@pytest.mark.usefixtures("depends_on_current_app")
class test_unpickle_task:
    def test_unpickle_v1(self, app):
        app.tasks["txfoo"] = "bar"
        assert _unpickle_task("txfoo") == "bar"

    def test_unpickle_v2(self, app):
        app.tasks["txfoo1"] = "bar1"
        assert _unpickle_task_v2("txfoo1") == "bar1"
        assert _unpickle_task_v2("txfoo1", module="celery") == "bar1"


class test_TaskRegistry:
    def setup_method(self):
        self.mytask = self.app.task(name="A", shared=False)(returns)
        self.missing_name_task = self.app.task(name=None, shared=False)(returns)
        self.missing_name_task.name = None  # name is overridden with path
        self.othertask = self.app.task(name="B", shared=False)(returns)

    def test_NotRegistered_str(self):
        assert repr(self.app.tasks.NotRegistered("tasks.add"))

    def assert_register_unregister_cls(self, r, task):
        r.unregister(task)
        with pytest.raises(r.NotRegistered):
            r.unregister(task)
        r.register(task)
        assert task.name in r

    def test_task_registry(self):
        r = self.app._tasks
        assert isinstance(r, dict)

        self.assert_register_unregister_cls(r, self.mytask)
        self.assert_register_unregister_cls(r, self.othertask)

        with pytest.raises(InvalidTaskError):
            r.register(self.missing_name_task)

        r.register(self.othertask)
        r.unregister(self.othertask.name)
        assert self.othertask not in r
        r.register(self.othertask)

        tasks = dict(r)
        assert tasks.get(self.mytask.name) is self.mytask
        assert tasks.get(self.othertask.name) is self.othertask

        assert r[self.mytask.name] is self.mytask
        assert r[self.othertask.name] is self.othertask

        r.unregister(self.mytask)
        assert self.mytask.name not in r
        r.unregister(self.othertask)
        assert self.othertask.name not in r

        assert self.mytask.run()
        assert self.othertask.run()
