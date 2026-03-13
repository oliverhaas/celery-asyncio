try:
    from celery.contrib.django.backend import CeleryBackend as CeleryBackend
except Exception:
    pass  # Django not installed or not configured.
