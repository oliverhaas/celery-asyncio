# Originally from Kombu by Ask Solem & contributors (BSD-3-Clause)
# https://github.com/celery/kombu
"""Custom maps, sequences, etc."""


class HashedSeq(list):
    """Hashed Sequence.

    Type used for hash() to make sure the hash is not generated
    multiple times.
    """

    __slots__ = ("hashvalue",)

    def __init__(self, *seq):
        self[:] = seq
        self.hashvalue = hash(seq)

    def __hash__(self):
        return self.hashvalue


def eqhash(o):
    """Call ``obj.__eqhash__``."""
    try:
        return o.__eqhash__()
    except AttributeError:
        return hash(o)


class EqualityDict(dict):
    """Dict using the eq operator for keying."""

    def __getitem__(self, key):
        h = eqhash(key)
        if h not in self:
            # dict has no __missing__ of its own; mirror its behaviour so a
            # plain EqualityDict raises KeyError instead of AttributeError.
            missing = getattr(self, "__missing__", None)
            if missing is None:
                raise KeyError(key)
            return missing(key)
        return super().__getitem__(h)

    def __setitem__(self, key, value):
        return super().__setitem__(eqhash(key), value)

    def __delitem__(self, key):
        return super().__delitem__(eqhash(key))
