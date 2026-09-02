# Originally from Kombu by Ask Solem & contributors (BSD-3-Clause)
# https://github.com/celery/kombu
"""Text encoding utilities.

Utilities to encode text, and to safely emit text from running
applications without crashing from the infamous
:exc:`UnicodeDecodeError` exception.
"""

import traceback


def str_to_bytes(s):
    """Convert str to bytes."""
    if isinstance(s, str):
        return s.encode()
    return s


def bytes_to_str(s):
    """Convert bytes to str."""
    if isinstance(s, bytes):
        return s.decode(errors="replace")
    return s


def ensure_bytes(s):
    """Ensure s is bytes, not str."""
    if not isinstance(s, bytes):
        return str_to_bytes(s)
    return s


def safe_str(s):
    """Safe form of str(), void of unicode errors."""
    s = bytes_to_str(s)
    if not isinstance(s, str):
        return safe_repr(s)
    return _safe_str(s)


def _safe_str(s):
    if isinstance(s, str):
        return s
    try:
        return str(s)
    except Exception as exc:
        return "<Unrepresentable {!r}: {!r} {!r}>".format(type(s), exc, "\n".join(traceback.format_stack()))


def safe_repr(o):
    """Safe form of repr, void of Unicode errors."""
    try:
        return repr(o)
    except Exception:
        return _safe_str(o)
