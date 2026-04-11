# Originally from Kombu by Ask Solem & contributors (BSD-3-Clause)
# https://github.com/celery/kombu
"""UUID utilities."""

from collections.abc import Callable
from uuid import UUID, uuid4


def uuid(_uuid: Callable[[], UUID] = uuid4) -> str:
    """Generate unique id in UUID4 format.

    See Also
    --------
        For now this is provided by :func:`uuid.uuid4`.
    """
    return str(_uuid())
