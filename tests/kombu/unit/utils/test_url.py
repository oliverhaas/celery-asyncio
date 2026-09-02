import pytest

from kombu.utils.url import as_url, maybe_sanitize_url, sanitize_url, url_to_parts


@pytest.mark.parametrize(
    "urltuple,expected",
    [
        (("https",), "https:///"),
        (("https", "e.com"), "https://e.com/"),
        (("https", "e.com", 80), "https://e.com:80/"),
        (("https", "e.com", 80, "u"), "https://u@e.com:80/"),
        (("https", "e.com", 80, "u", "p"), "https://u:p@e.com:80/"),
        (("https", "e.com", 80, None, "p"), "https://:p@e.com:80/"),
        (("https", "e.com", 80, None, "p", "/foo"), "https://:p@e.com:80//foo"),
        (("https", "e.com", 80, "u", "p", "foo", {"a": "1"}), "https://u:p@e.com:80/foo?a=1"),
        (("https", "e.com", None, None, None, None, "a=1&b=2"), "https://e.com/?a=1&b=2"),
    ],
)
def test_as_url(urltuple, expected):
    assert as_url(*urltuple) == expected


@pytest.mark.parametrize(
    "url",
    [
        "redis://user:pass@host:6379/0?ssl_cert_reqs=CERT_REQUIRED&ssl_ca_certs=/var/ssl/myca.pem",
        "amqp://user:pass@host:5672/vhost",
        "redis://host:6379/0?ssl_cert_reqs=none",
    ],
)
def test_as_url_round_trips_url_to_parts(url):
    assert as_url(*url_to_parts(url)) == url


def test_sanitize_url_keeps_the_query_string():
    # The query used to be accepted by as_url and dropped on the floor, so
    # the startup banner showed a broker URL without its options.
    assert (
        sanitize_url("redis://user:pass@host:6379/0?ssl_cert_reqs=CERT_REQUIRED")
        == "redis://user:**@host:6379/0?ssl_cert_reqs=CERT_REQUIRED"
    )
    assert sanitize_url("redis://host:6379/0?ssl_cert_reqs=none") == "redis://host:6379/0?ssl_cert_reqs=none"


@pytest.mark.parametrize(
    "url,expected",
    [
        ("foo", "foo"),
        ("http://u:p@e.com//foo", "http://u:**@e.com//foo"),
    ],
)
def test_maybe_sanitize_url(url, expected):
    assert maybe_sanitize_url(url) == expected
    assert maybe_sanitize_url("http://u:p@e.com//foo") == "http://u:**@e.com//foo"
