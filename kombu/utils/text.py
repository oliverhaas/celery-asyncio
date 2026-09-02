# Originally from Kombu by Ask Solem & contributors (BSD-3-Clause)
# https://github.com/celery/kombu
"""Text Utilities."""

from kombu import version_info_t


def version_string_as_tuple(s: str) -> version_info_t:
    """Convert version string to version info tuple."""
    v = _unpack_version(*s.split("."))
    # X.Y.3a1 -> (X, Y, 3, 'a1')
    if isinstance(v.micro, str):
        v = version_info_t(v.major, v.minor, *_splitmicro(*v[2:]))
    # X.Y.3a1-40 -> (X, Y, 3, 'a1', '40')
    if not v.serial and v.releaselevel and "-" in v.releaselevel:
        v = version_info_t(*list(v[0:3]) + v.releaselevel.split("-"))
    return v


def _unpack_version(
    major: str,
    minor: str | int = 0,
    micro: str | int = 0,
    releaselevel: str = "",
    serial: str = "",
) -> version_info_t:
    return version_info_t(int(major), int(minor), micro, releaselevel, serial)


def _splitmicro(micro: str, releaselevel: str = "", serial: str = "") -> tuple[int, str, str]:
    for index, char in enumerate(micro):  # noqa: B007
        if not char.isdigit():
            break
    else:
        return int(micro or 0), releaselevel, serial
    return int(micro[:index]), micro[index:], serial
