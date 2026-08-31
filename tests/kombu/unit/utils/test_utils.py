import pytest

from kombu import version_info_t
from kombu.utils.text import version_string_as_tuple


def test_dir():
    import kombu

    assert dir(kombu)


def test_every_name_in_all_is_importable():
    import kombu.utils

    for name in kombu.utils.__all__:
        assert getattr(kombu.utils, name, None) is not None, name


@pytest.mark.parametrize(
    "version,expected",
    [
        ("3", version_info_t(3, 0, 0, "", "")),
        ("3.3", version_info_t(3, 3, 0, "", "")),
        ("3.3.1", version_info_t(3, 3, 1, "", "")),
        ("3.3.1a3", version_info_t(3, 3, 1, "a3", "")),
        ("3.3.1.a3.40c32", version_info_t(3, 3, 1, "a3", "40c32")),
    ],
)
def test_version_string_as_tuple(version, expected):
    assert version_string_as_tuple(version) == expected
