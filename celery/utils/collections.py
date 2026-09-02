# Originally from Celery by Ask Solem & contributors (BSD-3-Clause)
# https://github.com/celery/celery
"""Custom maps, sets, sequences, and other data structures."""

import time
from collections import OrderedDict as _OrderedDict
from collections.abc import Callable, Mapping, MutableMapping, MutableSet
from heapq import heapify, heappop, heappush
from itertools import chain
from typing import Any

from .functional import first, uniq
from .text import match_case

try:
    from django.utils.functional import LazyObject, LazySettings
except ImportError:

    class LazyObject:  # type: ignore[no-redef]
        pass

    LazySettings = LazyObject

__all__ = (
    "AttributeDictMixin",
    "AttributeDict",
    "ChainMap",
    "ConfigurationView",
    "DictAttribute",
    "LimitedSet",
    "OrderedDict",
    "force_mapping",
    "lpmerge",
)

REPR_LIMITED_SET = """\
<{name}({size}): maxlen={0.maxlen}, expires={0.expires}, minlen={0.minlen}>\
"""


def force_mapping(m):
    """Wrap object into supporting the mapping interface if necessary."""
    if isinstance(m, (LazyObject, LazySettings)):
        m = m._wrapped
    return DictAttribute(m) if not isinstance(m, Mapping) else m


def lpmerge(L, R):
    """In place left precedent dictionary merge.

    Keeps values from `L`, if the value in `R` is :const:`None`.
    """
    setitem = L.__setitem__
    [setitem(k, v) for k, v in R.items() if v is not None]
    return L


class OrderedDict(_OrderedDict):
    """Dict where insertion order matters."""

    def _LRUkey(self):
        # return value of od.keys does not support __next__,
        # but this version will also not create a copy of the list.
        return next(iter(self.keys()))


class AttributeDictMixin:
    """Mixin for Mapping interface that adds attribute access.

    I.e., `d.key -> d[key]`).
    """

    def __getattr__(self, k):
        """`d.key -> d[key]`."""
        try:
            return self[k]
        except KeyError:
            raise AttributeError(f"{type(self).__name__!r} object has no attribute {k!r}") from None

    def __setattr__(self, key: str, value) -> None:
        """`d[key] = value -> d.key = value`."""
        self[key] = value


class AttributeDict(dict, AttributeDictMixin):
    """Dict subclass with attribute access."""


class DictAttribute:
    """Dict interface to attributes.

    `obj[k] -> obj.k`
    `obj[k] = val -> obj.k = val`
    """

    obj = None

    def __init__(self, obj):
        object.__setattr__(self, "obj", obj)

    def __getattr__(self, key):
        return getattr(self.obj, key)

    def __setattr__(self, key, value):
        return setattr(self.obj, key, value)

    def get(self, key, default=None):
        try:
            return self[key]
        except KeyError:
            return default

    def setdefault(self, key, default=None):
        if key not in self:
            self[key] = default

    def __getitem__(self, key):
        try:
            return getattr(self.obj, key)
        except AttributeError:
            raise KeyError(key) from None

    def __setitem__(self, key, value):
        setattr(self.obj, key, value)

    def __contains__(self, key):
        return hasattr(self.obj, key)

    def _iterate_keys(self):
        return iter(dir(self.obj))

    iterkeys = _iterate_keys

    def __iter__(self):
        return self._iterate_keys()

    def _iterate_items(self):
        for key in self._iterate_keys():
            yield key, getattr(self.obj, key)

    iteritems = _iterate_items

    def _iterate_values(self):
        for key in self._iterate_keys():
            yield getattr(self.obj, key)

    itervalues = _iterate_values

    items = _iterate_items
    keys = _iterate_keys
    values = _iterate_values


MutableMapping.register(DictAttribute)


class ChainMap(MutableMapping):
    """Key lookup on a sequence of maps."""

    key_t = None
    changes = None
    defaults = None
    maps = None
    _observers = ()

    def __init__(self, *maps, **kwargs):
        maps = list(maps or [{}])
        self.__dict__.update(
            key_t=kwargs.get("key_t"),
            maps=maps,
            changes=maps[0],
            defaults=maps[1:],
            _observers=[],
        )

    def add_defaults(self, d):
        d = force_mapping(d)
        self.defaults.insert(0, d)
        self.maps.insert(1, d)

    def pop(self, key, *default):
        try:
            return self.maps[0].pop(key, *default)
        except KeyError:
            raise KeyError(f"Key not found in the first mapping: {key!r}") from None

    def __missing__(self, key):
        raise KeyError(key)

    def _key(self, key):
        return self.key_t(key) if self.key_t is not None else key

    def __getitem__(self, key):
        _key = self._key(key)
        for mapping in self.maps:
            try:
                return mapping[_key]
            except KeyError:
                pass
        return self.__missing__(key)

    def __setitem__(self, key, value):
        self.changes[self._key(key)] = value

    def __delitem__(self, key):
        try:
            del self.changes[self._key(key)]
        except KeyError:
            raise KeyError(f"Key not found in first mapping: {key!r}") from None

    def clear(self):
        self.changes.clear()

    def get(self, key, default=None):
        try:
            return self[key]
        except KeyError:
            return default

    def __len__(self):
        return len(set().union(*self.maps))

    def __iter__(self):
        return self._iterate_keys()

    def __contains__(self, key):
        key = self._key(key)
        return any(key in m for m in self.maps)

    def __bool__(self):
        return any(self.maps)

    def setdefault(self, key, default=None):
        key = self._key(key)
        if key not in self:
            self[key] = default

    def update(self, *args, **kwargs):
        result = self.changes.update(*args, **kwargs)
        for callback in self._observers:
            callback(*args, **kwargs)
        return result

    def __repr__(self):
        return "{.__class__.__name__}({})".format(self, ", ".join(map(repr, self.maps)))

    @classmethod
    def fromkeys(cls, iterable, *args):
        """Create a ChainMap with a single dict created from the iterable."""
        return cls(dict.fromkeys(iterable, *args))

    def copy(self):
        return self.__class__(self.maps[0].copy(), *self.maps[1:])

    __copy__ = copy  # Py2

    def _iter(self, op):
        # defaults must be first in the stream, so values in
        # changes take precedence.
        return chain(*(op(d) for d in reversed(self.maps)))

    def _iterate_keys(self):
        return uniq(self._iter(lambda d: d.keys()))

    iterkeys = _iterate_keys

    def _iterate_items(self):
        return ((key, self[key]) for key in self)

    iteritems = _iterate_items

    def _iterate_values(self):
        return (self[key] for key in self)

    itervalues = _iterate_values

    def bind_to(self, callback):
        self._observers.append(callback)

    keys = _iterate_keys
    items = _iterate_items
    values = _iterate_values


class ConfigurationView(ChainMap, AttributeDictMixin):
    """A view over an applications configuration dictionaries.

    Custom (but older) version of :class:`collections.ChainMap`.

    If the key does not exist in ``changes``, the ``defaults``
    dictionaries are consulted.

    Arguments:
        changes (Mapping): Map of configuration changes.
        defaults (List[Mapping]): List of dictionaries containing
            the default configuration.
    """

    def __init__(self, changes, defaults=None, keys=None, prefix=None):
        defaults = [] if defaults is None else defaults
        super().__init__(changes, *defaults)
        self.__dict__.update(
            prefix=prefix.rstrip("_") + "_" if prefix else prefix,
            _keys=keys,
        )

    def _to_keys(self, key):
        prefix = self.prefix
        if prefix:
            pkey = prefix + key if not key.startswith(prefix) else key
            return match_case(pkey, prefix), key
        return (key,)

    def __getitem__(self, key):
        keys = self._to_keys(key)
        getitem = super().__getitem__
        for k in keys + (tuple(f(key) for f in self._keys) if self._keys else ()):
            try:
                return getitem(k)
            except KeyError:
                pass
        try:
            # support subclasses implementing __missing__
            return self.__missing__(key)
        except KeyError:
            if len(keys) > 1:
                raise KeyError("Key not found: {0!r} (with prefix: {0!r})".format(*keys)) from None
            raise

    def __setitem__(self, key, value):
        self.changes[self._key(key)] = value

    def first(self, *keys):
        return first(None, (self.get(key) for key in keys))

    def get(self, key, default=None):
        try:
            return self[key]
        except KeyError:
            return default

    def clear(self):
        """Remove all changes, but keep defaults."""
        self.changes.clear()

    def __contains__(self, key):
        keys = self._to_keys(key)
        return any(any(k in m for k in keys) for m in self.maps)

    def swap_with(self, other):
        changes = other.__dict__["changes"]
        defaults = other.__dict__["defaults"]
        self.__dict__.update(
            changes=changes,
            defaults=defaults,
            key_t=other.__dict__["key_t"],
            prefix=other.__dict__["prefix"],
            maps=[changes] + defaults,
        )


class LimitedSet:
    """Kind-of Set (or priority queue) with limitations.

    Good for when you need to test for membership (`a in set`),
    but the set should not grow unbounded.

    ``maxlen`` is enforced at all times, so if the limit is reached
    we'll also remove non-expired items.

    You can also configure ``minlen``: this is the minimal residual size
    of the set.

    All arguments are optional, and no limits are enabled by default.

    Arguments:
        maxlen (int): Optional max number of items.
            Adding more items than ``maxlen`` will result in immediate
            removal of items sorted by oldest insertion time.

        expires (float): TTL for all items.
            Expired items are purged as keys are inserted.

        minlen (int): Minimal residual size of this set.
            .. versionadded:: 4.0

            Value must be less than ``maxlen`` if both are configured.

            Older expired items will be deleted, only after the set
            exceeds ``minlen`` number of items.

        data (Sequence): Initial data to initialize set with.
            Can be an iterable of ``(key, value)`` pairs,
            a dict (``{key: insertion_time}``), or another instance
            of :class:`LimitedSet`.

    Example:
        >>> s = LimitedSet(maxlen=50000, expires=3600, minlen=4000)
        >>> for i in range(60000):
        ...     s.add(i)
        ...     s.add(str(i))
        ...
        >>> 57000 in s  # last 50k inserted values are kept
        True
        >>> '10' in s  # '10' did expire and was purged from set.
        False
        >>> len(s)  # maxlen is reached
        50000
        >>> s.purge(now=time.monotonic() + 7200)  # clock + 2 hours
        >>> len(s)  # now only minlen items are cached
        4000
        >>>> 57000 in s  # even this item is gone now
        False
    """

    max_heap_percent_overload = 15

    def __init__(self, maxlen=0, expires=0, data=None, minlen=0):
        self.maxlen = 0 if maxlen is None else maxlen
        self.minlen = 0 if minlen is None else minlen
        self.expires = 0 if expires is None else expires
        self._data = {}
        self._heap = []

        if data:
            # import items from data
            self.update(data)

        if not self.maxlen >= self.minlen >= 0:
            raise ValueError("minlen must be a positive number, less or equal to maxlen.")
        if self.expires < 0:
            raise ValueError("expires cannot be negative!")

    def _refresh_heap(self):
        """Time consuming recreating of heap.  Don't run this too often."""
        self._heap[:] = list(self._data.values())
        heapify(self._heap)

    def _maybe_refresh_heap(self):
        if self._heap_overload >= self.max_heap_percent_overload:
            self._refresh_heap()

    def clear(self):
        """Clear all data, start from scratch again."""
        self._data.clear()
        self._heap[:] = []

    def add(self, item, now=None):
        """Add a new item, or reset the expiry time of an existing item."""
        now = now or time.monotonic()
        if item in self._data:
            self.discard(item)
        entry = (now, item)
        self._data[item] = entry
        heappush(self._heap, entry)
        if self.maxlen and len(self._data) >= self.maxlen:
            self.purge()

    def update(self, other):
        """Update this set from other LimitedSet, dict or iterable."""
        if not other:
            return
        if isinstance(other, LimitedSet):
            self._data.update(other._data)
            self._refresh_heap()
            self.purge()
        elif isinstance(other, dict):
            # revokes are sent as a dict
            for key, inserted in other.items():
                if isinstance(inserted, (tuple, list)):
                    # in case someone uses ._data directly for sending update
                    inserted = inserted[0]
                if not isinstance(inserted, float):
                    raise ValueError(f"Expecting float timestamp, got type {type(inserted)!r} with value: {inserted}")
                self.add(key, inserted)
        else:
            # XXX AVOID THIS, it could keep old data if more parties
            # exchange them all over and over again
            for obj in other:
                self.add(obj)

    def discard(self, item):
        # mark an existing item as removed.  If KeyError is not found, pass.
        self._data.pop(item, None)
        self._maybe_refresh_heap()

    pop_value = discard

    def purge(self, now=None):
        """Check oldest items and remove them if needed.

        Arguments:
            now (float): Time of purging -- by default right now.
                This can be useful for unit testing.
        """
        now = now or time.monotonic()
        now = now() if isinstance(now, Callable) else now
        if self.maxlen:
            while len(self._data) > self.maxlen:
                self.pop()
        # time based expiring:
        if self.expires:
            while len(self._data) > self.minlen >= 0:
                self._drop_stale_heap_head()
                if not self._heap:
                    break
                inserted_time, _ = self._heap[0]
                if inserted_time + self.expires > now:
                    break  # oldest item hasn't expired yet
                self.pop()

    def _drop_stale_heap_head(self):
        """Discard heap entries left behind by a discarded or refreshed item.

        Both ``discard`` and re-adding an item leave the previous
        ``(timestamp, item)`` entry in the heap.  The head has to be a live
        entry before its timestamp can be read as the oldest one.
        """
        heap, data = self._heap, self._data
        while heap and data.get(heap[0][1]) != heap[0]:
            heappop(heap)

    def pop(self, default: Any = None) -> Any:
        """Remove and return the oldest item, or :const:`None` when empty."""
        while self._heap:
            entry = heappop(self._heap)
            item = entry[1]
            if self._data.get(item) != entry:
                continue  # stale entry, the item was discarded or re-added
            del self._data[item]
            return item
        return default

    def as_dict(self):
        """Whole set as serializable dictionary.

        Example:
            >>> s = LimitedSet(maxlen=200)
            >>> r = LimitedSet(maxlen=200)
            >>> for i in range(500):
            ...     s.add(i)
            ...
            >>> r.update(s.as_dict())
            >>> r == s
            True
        """
        return {key: inserted for inserted, key in self._data.values()}

    def __eq__(self, other):
        if isinstance(other, LimitedSet):
            return self._data == other._data
        return NotImplemented

    def __repr__(self):
        return REPR_LIMITED_SET.format(
            self,
            name=type(self).__name__,
            size=len(self),
        )

    def __iter__(self):
        return (i for _, i in sorted(self._data.values()))

    def __len__(self):
        return len(self._data)

    def __contains__(self, key):
        return key in self._data

    def __reduce__(self):
        return self.__class__, (self.maxlen, self.expires, self.as_dict(), self.minlen)

    def __bool__(self):
        return bool(self._data)

    @property
    def _heap_overload(self):
        """Compute how much is heap bigger than data [percents]."""
        return len(self._heap) * 100 / max(len(self._data), 1) - 100


MutableSet.register(LimitedSet)
