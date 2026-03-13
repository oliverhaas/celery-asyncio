"""Minimal Django settings for the django_tasks example."""

import os

SECRET_KEY = "example-secret-key-not-for-production"
DEBUG = True
ALLOWED_HOSTS = ["*"]
ROOT_URLCONF = "proj.urls"

INSTALLED_APPS = [
    "django.contrib.contenttypes",
]

# -- Celery ------------------------------------------------------------------

CELERY_BROKER_URL = os.environ.get("CELERY_BROKER_URL", "redis://localhost:6379/0")
CELERY_RESULT_BACKEND = os.environ.get("CELERY_RESULT_BACKEND", "redis://localhost:6379/0")
CELERY_RESULT_EXTENDED = True  # Required for get_result() task resolution.

# -- Django Tasks ------------------------------------------------------------

TASKS = {
    "default": {
        "BACKEND": "celery.contrib.django.CeleryBackend",
        "QUEUES": ["default"],
        "OPTIONS": {
            # Explicit Celery app path. If omitted, uses the current default app.
            "celery_app": "proj.celery.app",
        },
    },
}

# Minimal middleware (no sessions, no auth).
MIDDLEWARE = []
USE_TZ = True
