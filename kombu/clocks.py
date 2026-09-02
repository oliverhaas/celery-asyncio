# Originally from Kombu by Ask Solem & contributors (BSD-3-Clause)
# https://github.com/celery/kombu
"""Logical Clocks and Synchronization."""

from itertools import islice
from operator import itemgetter
from threading import Lock
from typing import Any

__all__ = ("LamportClock", "timetuple")

R_CLOCK = "_lamport(clock={0}, timestamp={1}, id={2} {3!r})"


def _before(a: tuple, b: tuple) -> bool:
    """Return whether event ``a`` happened before event ``b``."""
    # 0: clock 1: timestamp 2: process id
    A, B = a[0], b[0]
    if A and B:  # use the logical clock when both events carry one
        if A == B:  # equal clocks are ordered by process id
            return a[2] < b[2]
        return A < B
    return a[1] < b[1]  # ... or fall back to the timestamp


class timetuple(tuple):
    """Tuple of event clock information.

    Can be used as part of a heap to keep events ordered.

    Arguments:
    ---------
        clock (Optional[int]):  Event clock value.
        timestamp (float): Event UNIX timestamp value.
        id (str): Event host id (e.g. ``hostname:pid``).
        obj (Any): Optional obj to associate with this event.
    """

    __slots__ = ()

    def __new__(cls, clock: int | None, timestamp: float, id: str, obj: Any = None) -> timetuple:
        return tuple.__new__(cls, (clock, timestamp, id, obj))

    def __repr__(self) -> str:
        return R_CLOCK.format(*self)

    def __getnewargs__(self) -> tuple:
        return tuple(self)

    # The four below cannot delegate to `other < self` or `not other < self`:
    # `other` is usually a plain tuple, and Python offers the subclass its
    # reflected operation first, which lands back here and recurses forever.
    def __lt__(self, other: tuple) -> bool:
        try:
            return _before(self, other)
        except IndexError:
            return NotImplemented

    def __gt__(self, other: tuple) -> bool:
        try:
            return _before(other, self)
        except IndexError:
            return NotImplemented

    def __le__(self, other: tuple) -> bool:
        try:
            return not _before(other, self)
        except IndexError:
            return NotImplemented

    def __ge__(self, other: tuple) -> bool:
        try:
            return not _before(self, other)
        except IndexError:
            return NotImplemented

    clock = property(itemgetter(0))
    timestamp = property(itemgetter(1))
    id = property(itemgetter(2))
    obj = property(itemgetter(3))


class LamportClock:
    """Lamport's logical clock.

    From Wikipedia:

    A Lamport logical clock is a monotonically incrementing software counter
    maintained in each process.  It follows some simple rules:

        * A process increments its counter before each event in that process;
        * When a process sends a message, it includes its counter value with
          the message;
        * On receiving a message, the receiver process sets its counter to be
          greater than the maximum of its own value and the received value
          before it considers the message received.

    Conceptually, this logical clock can be thought of as a clock that only
    has meaning in relation to messages moving between processes.  When a
    process receives a message, it resynchronizes its logical clock with
    the sender.

    See Also
    --------
        * `Lamport timestamps`_

        * `Lamports distributed mutex`_

    .. _`Lamport Timestamps`: https://en.wikipedia.org/wiki/Lamport_timestamps
    .. _`Lamports distributed mutex`: https://bit.ly/p99ybE

    *Usage*

    When sending a message use :meth:`forward` to increment the clock,
    when receiving a message use :meth:`adjust` to sync with
    the time stamp of the incoming message.

    """

    #: The clocks current value.
    value = 0

    def __init__(self, initial_value: int = 0, Lock: type[Lock] = Lock) -> None:
        self.value = initial_value
        self.mutex = Lock()

    def adjust(self, other: int) -> int:
        with self.mutex:
            value = self.value = max(self.value, other) + 1
            return value

    def forward(self) -> int:
        with self.mutex:
            self.value += 1
            return self.value

    def sort_heap(self, h: list[tuple[int, str]]) -> tuple[int, str]:
        """Sort heap of events.

        List of tuples containing at least two elements, representing
        an event, where the first element is the event's scalar clock value,
        and the second element is the id of the process (usually
        ``"hostname:pid"``): ``sh([(clock, processid, ...?), (...)])``

        The list must already be sorted, which is why we refer to it as a
        heap.

        The tuple will not be unpacked, so more than two elements can be
        present.

        Will return the latest event.
        """
        if len(h) > 1 and h[0][0] == h[1][0]:
            same = []
            for PN in zip(h, islice(h, 1, None), strict=False):
                if PN[0][0] != PN[1][0]:
                    break  # Prev and Next's clocks differ
                same.append(PN[0])
            # return first item sorted by process id
            return min(same, key=lambda event: event[1])
        # clock values unique, return first item
        return h[0]

    def __str__(self) -> str:
        return str(self.value)

    def __repr__(self) -> str:
        return f"<LamportClock: {self.value}>"
