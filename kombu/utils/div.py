# Originally from Kombu by Ask Solem & contributors (BSD-3-Clause)
# https://github.com/celery/kombu
"""Div. Utilities."""

import logging
import os

logger = logging.getLogger(__name__)


def emergency_dump_state(state, open_file=open, dump=None, stderr=None):
    """Dump message state to a file, reporting where it went.

    Without an explicit `stderr` the report is logged rather than printed, so
    the path to the dump reaches wherever the application sends its logs
    instead of a stream nobody is watching.
    """
    from pprint import pformat
    from tempfile import mkstemp

    if dump is None:
        import pickle

        dump = pickle.dump
    fd, persist = mkstemp()
    os.close(fd)
    if stderr:
        print(f"EMERGENCY DUMP STATE TO FILE -> {persist} <-", file=stderr)
    else:
        logger.error(
            "EMERGENCY DUMP STATE TO FILE -> %s <-",
            persist,
            extra={"emergency_state_file": persist},
        )
    fh = open_file(persist, "wb")
    try:
        try:
            dump(state, fh, protocol=0)
        except Exception as exc:
            if stderr:
                print(
                    f"Cannot pickle state: {exc!r}. Fallback to pformat.",
                    file=stderr,
                )
            else:
                logger.exception("Cannot pickle state. Falling back to pformat.")
            # The file is binary because of pickle, so the fallback has to
            # encode. It used to hand pformat's str straight to it and raise
            # TypeError, losing the very state the dump exists to preserve.
            fh.write(pformat(state).encode("utf-8", "replace"))
    finally:
        fh.flush()
        fh.close()
    return persist
