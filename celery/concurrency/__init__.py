"""Pool implementation abstract factory, and alias definitions.

celery-asyncio uses asyncio with threads for concurrency.
Legacy pool types (prefork, eventlet, gevent) are not available.
"""
import os

from kombu.utils.imports import symbol_by_name

__all__ = ('get_implementation', 'get_available_pool_names',)

ALIASES = {
    'asyncio': 'celery.concurrency.aio:TaskPool',
    'solo': 'celery.concurrency.solo:TaskPool',
    'threads': 'celery.concurrency.thread:TaskPool',
}

# Allow for an out-of-tree worker pool implementation
try:
    custom = os.environ.get('CELERY_CUSTOM_WORKER_POOL')
except KeyError:
    pass
else:
    if custom:
        ALIASES['custom'] = custom


def get_implementation(cls):
    """Return pool implementation by name."""
    return symbol_by_name(cls, ALIASES)


def get_available_pool_names():
    """Return all available pool type names."""
    return tuple(ALIASES.keys())
