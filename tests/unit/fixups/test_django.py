import socket
from contextlib import contextmanager
from unittest.mock import MagicMock, Mock, patch

import pytest

from celery import signals
from celery.fixups.django import DjangoFixup, DjangoWorkerFixup, FixupWarning, fixup
from tests.unit import conftest


class FixupCase:
    Fixup = None

    @contextmanager
    def fixup_context(self, app):
        with patch("celery.fixups.django.DjangoWorkerFixup.validate_models"):
            with patch("celery.fixups.django.symbol_by_name") as symbyname:
                with patch("celery.fixups.django.import_module") as impmod:
                    f = self.Fixup(app)
                    yield f, impmod, symbyname


class test_DjangoFixup(FixupCase):
    Fixup = DjangoFixup

    def test_setting_default_app(self):
        from celery import _state

        prev, _state.default_app = _state.default_app, None
        try:
            app = Mock(name="app")
            DjangoFixup(app)
            app.set_default.assert_called_with()
        finally:
            _state.default_app = prev

    @patch("celery.fixups.django.DjangoWorkerFixup")
    def test_worker_fixup_property(self, DjangoWorkerFixup):
        f = DjangoFixup(self.app)
        f._worker_fixup = None
        assert f.worker_fixup is DjangoWorkerFixup()
        assert f.worker_fixup is DjangoWorkerFixup()

    def test_on_import_modules(self):
        f = DjangoFixup(self.app)
        f.worker_fixup = Mock(name="worker_fixup")
        f.on_import_modules()
        f.worker_fixup.validate_models.assert_called_with()

    def test_autodiscover_tasks(self, patching):
        patching.modules("django.apps")
        from django.apps import apps

        f = DjangoFixup(self.app)
        configs = [Mock(name="c1"), Mock(name="c2")]
        apps.get_app_configs.return_value = configs
        assert f.autodiscover_tasks() == [c.name for c in configs]

    @pytest.mark.masked_modules("django")
    def test_fixup_no_django(self, patching, mask_modules):
        with patch("celery.fixups.django.DjangoFixup") as Fixup:
            patching.setenv("DJANGO_SETTINGS_MODULE", "")
            fixup(self.app)
            Fixup.assert_not_called()

            patching.setenv("DJANGO_SETTINGS_MODULE", "settings")
            with pytest.warns(FixupWarning):
                fixup(self.app)
            Fixup.assert_not_called()

    def test_fixup(self, patching):
        with patch("celery.fixups.django.DjangoFixup") as Fixup:
            patching.setenv("DJANGO_SETTINGS_MODULE", "")
            fixup(self.app)
            Fixup.assert_not_called()

            patching.setenv("DJANGO_SETTINGS_MODULE", "settings")
            with conftest.module_exists("django"):
                import django

                django.VERSION = (1, 11, 1)
                fixup(self.app)
                Fixup.assert_called()

    def test_init(self):
        with self.fixup_context(self.app) as (f, importmod, sym):
            assert f

    @pytest.mark.patched_module(
        "django",
        "django.db",
        "django.db.transaction",
    )
    def test_install(self, patching, module):
        self.app.loader = Mock()
        self.cw = patching("os.getcwd")
        self.p = patching("sys.path")
        self.sigs = patching("celery.fixups.django.signals")
        with self.fixup_context(self.app) as (f, _, _):
            self.cw.return_value = "/opt/vandelay"
            f.install()
            self.sigs.worker_init.connect.assert_called_with(f.on_worker_init)
            assert self.app.loader.now == f.now

            # Specialized DjangoTask class is used
            assert self.app.task_cls == "celery.contrib.django.task:DjangoTask"
            from celery.contrib.django.task import DjangoTask

            assert issubclass(f.app.Task, DjangoTask)
            assert hasattr(f.app.Task, "delay_on_commit")
            assert hasattr(f.app.Task, "apply_async_on_commit")

            self.p.insert.assert_called_with(0, "/opt/vandelay")

    def test_install_custom_user_task(self, patching):
        patching("celery.fixups.django.signals")

        self.app.task_cls = "myapp.celery.tasks:Task"
        self.app._custom_task_cls_used = True

        with self.fixup_context(self.app) as (f, _, _):
            f.install()
            # Specialized DjangoTask class is NOT used,
            # The one from the user's class is
            assert self.app.task_cls == "myapp.celery.tasks:Task"

    def test_install_custom_user_task_as_class_attribute(self, patching):
        patching("celery.fixups.django.signals")

        from celery.app import Celery

        class MyCeleryApp(Celery):
            task_cls = "myapp.celery.tasks:Task"

        app = MyCeleryApp("mytestapp")

        with self.fixup_context(app) as (f, _, _):
            f.install()
            # Specialized DjangoTask class is NOT used,
            # The one from the user's class is
            assert app.task_cls == "myapp.celery.tasks:Task"

    def test_now(self):
        with self.fixup_context(self.app) as (f, _, _):
            assert f.now(utc=True)
            f._now.assert_not_called()
            assert f.now(utc=False)
            f._now.assert_called()

    def test_on_worker_init(self):
        with self.fixup_context(self.app) as (f, _, _), patch("celery.fixups.django.DjangoWorkerFixup") as DWF:
            f.on_worker_init()
            DWF.assert_called_with(f.app)
            DWF.return_value.install.assert_called_with()
            assert f._worker_fixup is DWF.return_value


class test_DjangoWorkerFixup(FixupCase):
    Fixup = DjangoWorkerFixup

    def test_init(self):
        with self.fixup_context(self.app) as (f, importmod, sym):
            assert f

    def test_install(self):
        self.app.conf = {"CELERY_DB_REUSE_MAX": None}
        self.app.loader = Mock()
        with self.fixup_context(self.app) as (f, _, _), patch("celery.fixups.django.signals") as sigs:
            f.install()
            sigs.beat_embedded_init.connect.assert_called_with(
                f.close_database,
            )
            sigs.task_prerun.connect.assert_called_with(f.on_task_prerun)
            sigs.task_postrun.connect.assert_called_with(f.on_task_postrun)
            # Nothing forks, so the pool starting is not a reason to touch the
            # connections of the process that started it.
            sigs.worker_process_init.connect.assert_not_called()

    def test_the_pool_starting_leaves_open_connections_alone(self):
        # The fixup used to close the raw file descriptor of every open
        # connection when the pool started, which is what a forked child has to
        # do with the descriptors it inherited. Nothing forks here, so the
        # connection belongs to this process, and Django went on using a socket
        # whose number the next open call is free to hand out again.
        with self.fixup_context(self.app) as (f, _, _):
            left, right = socket.socketpair()
            with left, right:
                # A MagicMock for `wrap_database_errors`, which is a context
                # manager Django enters around the close.
                connection = MagicMock()
                connection.connection = left
                f._db.connections.all = Mock(return_value=[connection])
                f.install()

                signals.worker_process_init.send(sender=None)

                left.send(b"still open")
                assert right.recv(16) == b"still open"

    def test_on_task_prerun(self):
        task = Mock()
        with self.fixup_context(self.app) as (f, _, _):
            task.request.is_eager = False
            with patch.object(f, "close_database"):
                f.on_task_prerun(task)
                f.close_database.assert_called_with()

            task.request.is_eager = True
            with patch.object(f, "close_database"):
                f.on_task_prerun(task)
                f.close_database.assert_not_called()

    def test_on_task_postrun(self):
        task = Mock()
        with self.fixup_context(self.app) as (f, _, _):
            with patch.object(f, "close_cache"):
                task.request.is_eager = False
                with patch.object(f, "close_database"):
                    f.on_task_postrun(task)
                    f.close_database.assert_called()
                    f.close_cache.assert_called()

            # when a task is eager, don't close connections
            with patch.object(f, "close_cache"):
                task.request.is_eager = True
                with patch.object(f, "close_database"):
                    f.on_task_postrun(task)
                    f.close_database.assert_not_called()
                    f.close_cache.assert_not_called()

    def test_close_database(self):
        with self.fixup_context(self.app) as (f, _, _), patch.object(f, "_close_database") as _close:
            f.db_reuse_max = None
            f.close_database()
            _close.assert_called_with()
            _close.reset_mock()

            f.db_reuse_max = 10
            f._db_recycles = 3
            f.close_database()
            _close.assert_not_called()
            assert f._db_recycles == 4
            _close.reset_mock()

            f._db_recycles = 20
            f.close_database()
            _close.assert_called_with()
            assert f._db_recycles == 1

    def test__close_database(self):
        with self.fixup_context(self.app) as (f, _, _):
            conns = [Mock(), Mock(), Mock()]
            conns[1].close.side_effect = KeyError("already closed")
            f.DatabaseError = KeyError
            f.interface_errors = ()

            f._db.connections = Mock()  # ConnectionHandler
            f._db.connections.all.side_effect = lambda **kwargs: conns

            f._close_database()
            conns[0].close.assert_called_with()
            conns[1].close.assert_called_with()
            conns[2].close.assert_called_with()

            conns[1].close.side_effect = KeyError("omg")
            with pytest.raises(KeyError):
                f._close_database()

    def test_close_database_always_closes_connections(self):
        with self.fixup_context(self.app) as (f, _, _):
            conn = Mock()
            f._db.connections.all = Mock(return_value=[conn])
            f.close_database()
            conn.close.assert_called_once_with()
            # close_if_unusable_or_obsolete is not safe to call in all conditions, so avoid using
            # it to optimize connection handling.
            conn.close_if_unusable_or_obsolete.assert_not_called()

    def test_close_database_only_looks_at_connections_that_exist(self):
        # Without `initialized_only`, closing after a task that never touched
        # the database opens a connection just to close it (upstream cc3350ef9).
        with self.fixup_context(self.app) as (f, _, _):
            f._db.connections.all = Mock(return_value=[])
            f.close_database()
            f._db.connections.all.assert_called_once_with(initialized_only=True)

    def test_close_database_leaves_the_pool_alone(self):
        # One process, one shared pool, and this runs on every task prerun and
        # postrun. Closing the pool here would leave pooling doing nothing.
        # Upstream closes it under prefork only (upstream a4f9beb41).
        with self.fixup_context(self.app) as (f, _, _):
            conn = Mock()
            conn.alias = "default"
            f._db.connections.all = Mock(return_value=[conn])
            f._settings.DATABASES = {"default": {"OPTIONS": {"pool": True}}}
            f.close_database()
            conn.close.assert_called_once_with()
            conn.close_pool.assert_not_called()

    def test_close_cache_raises_error(self):
        with self.fixup_context(self.app) as (f, _, _):
            f._cache.close_caches.side_effect = AttributeError
            f.close_cache()

    def test_close_cache(self):
        with self.fixup_context(self.app) as (f, _, _):
            f.close_cache()
            f._cache.close_caches.assert_called_with()

    @pytest.mark.patched_module(
        "django", "django.db", "django.core", "django.core.cache", "django.conf", "django.db.utils"
    )
    def test_validate_models(self, patching, module):
        f = self.Fixup(self.app)
        f.django_setup = Mock(name="django.setup")
        patching.modules("django.core.checks")
        from django.core.checks import run_checks

        f.validate_models()
        f.django_setup.assert_called_with()
        run_checks.assert_called_with()

        # test --skip-checks flag
        f.django_setup.reset_mock()
        run_checks.reset_mock()

        patching.setenv("CELERY_SKIP_CHECKS", "true")
        f.validate_models()
        f.django_setup.assert_called_with()
        run_checks.assert_not_called()

    def test_django_setup(self, patching):
        patching("celery.fixups.django.symbol_by_name")
        patching("celery.fixups.django.import_module")
        (django,) = patching.modules("django")
        f = self.Fixup(self.app)
        f.django_setup()
        django.setup.assert_called_with()
