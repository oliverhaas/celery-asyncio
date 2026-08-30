"""Minimal Django settings for the django-q2 row.

django-q2 needs an ORM for its own bookkeeping even when task results are not
saved, so a throwaway sqlite file backs it. `save_limit: 0` keeps successful
tasks out of that database, which is django-q2's fastest configuration.
"""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
SECRET_KEY = "bench-only-not-a-credential"
DEBUG = False
USE_TZ = True
INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "django_q",
]
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": str(BASE_DIR / "results" / "djangoq.sqlite3"),
    },
}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

Q_CLUSTER = {
    "name": "bench",
    "workers": int(os.environ.get("FW_PROCS", "4")),
    "recycle": 100_000,
    "timeout": 600,
    "retry": 1200,
    "save_limit": 0,
    "max_attempts": 1,
    "ack_failures": True,
    "catch_up": False,
    "bulk": int(os.environ.get("FW_BULK", "10")),
    # Same reason as arq: the 0.2 s default poll is the ceiling, not the pool.
    "poll": float(os.environ.get("FW_POLL", "0.01")),
    "redis": {"host": "localhost", "port": 6379, "db": 0},
}

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"null": {"class": "logging.NullHandler"}},
    "loggers": {"django-q": {"handlers": ["null"], "level": "WARNING"}},
}
