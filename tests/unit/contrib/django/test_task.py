import subprocess
import sys
from unittest.mock import patch

import pytest


@pytest.mark.patched_module(
    "django",
    "django.db",
    "django.db.transaction",
)
@pytest.mark.usefixtures("module")
class test_DjangoTask:
    @pytest.fixture
    def task_instance(self):
        from celery.contrib.django.task import DjangoTask

        return DjangoTask()

    @pytest.fixture(name="on_commit")
    def on_commit(self):
        with patch(
            "django.db.transaction.on_commit",
            side_effect=lambda f: f(),
        ) as patched_on_commit:
            yield patched_on_commit

    def test_delay_on_commit(self, task_instance, on_commit):
        result = task_instance.delay_on_commit()
        assert result is None

    def test_apply_async_on_commit(self, task_instance, on_commit):
        result = task_instance.apply_async_on_commit()
        assert result is None


class test_DjangoTask_without_django:
    def test_the_module_imports(self):
        # The module used to import django.db at the top, so anything that
        # touched it without Django installed died on the import. A
        # subprocess, because by the time this runs the module is already in
        # sys.modules, imported while the class above had Django mocked out.
        result = subprocess.run(
            [sys.executable, "-c", "from celery.contrib.django.task import DjangoTask"],
            capture_output=True,
            text=True,
            check=False,
        )

        assert result.returncode == 0, result.stderr

    @pytest.mark.parametrize("method", ["delay_on_commit", "apply_async_on_commit"])
    def test_calling_it_says_what_is_missing(self, method):
        from celery.contrib.django.task import DjangoTask

        with pytest.raises(ModuleNotFoundError, match="django"):
            getattr(DjangoTask(), method)()
