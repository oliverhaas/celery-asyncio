"""django-q2 adapter: multiprocessing cluster, Redis broker, sqlite bookkeeping."""

import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "fw_djangoq_settings")

import django

django.setup()

from django_q.tasks import async_task

TASK_PATH = "fw_common.work_sync"


def worker_argv(bin_dir: str) -> list[str]:
    return [f"{bin_dir}/django-admin", "qcluster", "--pythonpath", ".", "--settings", "fw_djangoq_settings"]


def publish_loop(specs, backlog, done_fn, stop, state) -> None:
    i = 0
    n = len(specs)
    while not stop.is_set():
        if i - done_fn() < backlog:
            for _ in range(200):
                async_task(TASK_PATH, **specs[i % n])
                i += 1
            state["published"] = i
        else:
            stop.wait(0.02)
