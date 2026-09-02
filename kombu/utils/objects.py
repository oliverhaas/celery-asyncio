# Originally from Kombu by Ask Solem & contributors (BSD-3-Clause)
# https://github.com/celery/kombu
"""Object Utilities."""

from threading import RLock

__all__ = ("cached_property",)

from functools import cached_property as _cached_property

_NOT_FOUND = object()


class cached_property(_cached_property):
    """Implementation of Cached property."""

    def __init__(self, fget, fset=None, fdel=None):
        super().__init__(fget)
        self.__set = fset
        self.__del = fdel
        # Python 3.12+ removed the lock from functools.cached_property.
        # We need it for thread-safe __set__/__delete__ (especially for 3.14t free-threading).
        self.lock = RLock()

    def __set__(self, instance, value):
        with self.lock:
            if self.__set is not None:
                value = self.__set(instance, value)

            cache = instance.__dict__
            cache[self.attrname] = value

    def __delete__(self, instance):
        with self.lock:
            value = instance.__dict__.pop(self.attrname, _NOT_FOUND)

            if self.__del and value is not _NOT_FOUND:
                self.__del(instance, value)

    def setter(self, fset):
        return self.__class__(self.func, fset, self.__del)

    def deleter(self, fdel):
        return self.__class__(self.func, self.__set, fdel)
