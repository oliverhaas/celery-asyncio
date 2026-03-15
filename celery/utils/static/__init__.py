"""Static files."""

import os


def get_file(*args):
    """Get filename for static file."""
    return os.path.join(os.path.abspath(os.path.dirname(__file__)), *args)


def logo():
    """Celery logo image."""
    return get_file("celery_128.png")
