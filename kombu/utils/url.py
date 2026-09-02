# Originally from Kombu by Ask Solem & contributors (BSD-3-Clause)
# https://github.com/celery/kombu
"""URL Utilities."""
# ruff: noqa: TID252, SIM118

import ssl
from functools import partial
from typing import Any, NamedTuple
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlparse

from ..log import get_logger

ssl_available = True

safequote = partial(quote, safe="")
logger = get_logger(__name__)


class urlparts(NamedTuple):
    """Named tuple representing parts of the URL."""

    scheme: str
    hostname: str | None
    port: int | None
    username: str | None
    password: str | None
    path: str | None
    query: dict[str, Any]


def parse_url(url: str) -> dict[str, Any]:
    """Parse URL into mapping of components."""
    scheme, host, port, user, password, path, query = _parse_url(url)
    if query:
        keys = [key for key in query.keys() if key.startswith("ssl_")]
        for key in keys:
            if key == "ssl_check_hostname":
                query[key] = query[key].lower() != "false"
            elif key == "ssl_cert_reqs":
                query[key] = parse_ssl_cert_reqs(query[key])
                if query[key] is None:
                    logger.warning("Defaulting to insecure SSL behaviour.")

            if "ssl" not in query:
                query["ssl"] = {}

            query["ssl"][key] = query[key]
            del query[key]

    return dict(transport=scheme, hostname=host, port=port, userid=user, password=password, virtual_host=path, **query)


def url_to_parts(url: str) -> urlparts:
    """Parse URL into :class:`urlparts` tuple of components."""
    scheme = urlparse(url).scheme
    schemeless = url[len(scheme) + 3 :]
    # parse with HTTP URL semantics
    parts = urlparse("http://" + schemeless)
    path = parts.path or ""
    path = path[1:] if path and path[0] == "/" else path
    return urlparts(
        scheme,
        unquote(parts.hostname or "") or None,
        parts.port,
        unquote(parts.username or "") or None,
        unquote(parts.password or "") or None,
        unquote(path or "") or None,
        dict(parse_qsl(parts.query)),
    )


_parse_url = url_to_parts


def as_url(
    scheme: str,
    host: str | None = None,
    port: int | None = None,
    user: str | None = None,
    password: str | None = None,
    path: str | None = None,
    query: Any = None,
    sanitize: bool = False,
    mask: str = "**",
) -> str:
    """Generate URL from component parts."""
    parts: list[Any] = [f"{scheme}://"]
    if user or password:
        if user:
            parts.append(safequote(user))
        if password:
            if sanitize:
                parts.extend([":", mask] if mask else [":"])
            else:
                parts.extend([":", safequote(password)])
        parts.append("@")
    parts.append(safequote(host) if host else "")
    if port:
        parts.extend([":", port])
    parts.extend(["/", path])
    if query:
        # safe="/" keeps file paths in values such as ssl_ca_certs readable.
        parts.extend(["?", query if isinstance(query, str) else urlencode(query, safe="/", quote_via=quote)])
    return "".join(str(part) for part in parts if part)


def sanitize_url(url: str, mask: str = "**") -> str:
    """Return copy of URL with password removed."""
    return as_url(*_parse_url(url), sanitize=True, mask=mask)


def maybe_sanitize_url(url: Any, mask: str = "**") -> Any:
    """Sanitize url, or do nothing if url undefined."""
    if isinstance(url, str) and "://" in url:
        return sanitize_url(url, mask)
    return url


def parse_ssl_cert_reqs(query_value: str) -> Any:
    """Given the query parameter for ssl_cert_reqs, return the SSL constant or None."""
    if ssl_available:
        query_value_to_constant = {
            "CERT_REQUIRED": ssl.CERT_REQUIRED,
            "CERT_OPTIONAL": ssl.CERT_OPTIONAL,
            "CERT_NONE": ssl.CERT_NONE,
            "required": ssl.CERT_REQUIRED,
            "optional": ssl.CERT_OPTIONAL,
            "none": ssl.CERT_NONE,
        }
        return query_value_to_constant[query_value]
    return None
