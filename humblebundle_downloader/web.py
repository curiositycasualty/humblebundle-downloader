"""Talking to Humble over http: identity, cookies, retries and resumable requests"""

import datetime
import email
import re

from .files import _coerce_size


# Retried at the transport layer. 429 is deliberately absent: it is
# handled explicitly so the whole pool backs off together
RETRY_STATUSES = (500, 502, 503, 504)


THROTTLE_STATUSES = (429,)


DEFAULT_COOLDOWN = 30


SITE_HOST = "www.humblebundle.com"


def _validator_for(response):
    """What If-Range should carry, so the server can tell us the file
    changed rather than handing back a slice of a different one
    """
    try:
        headers = response.headers
    except Exception:
        return None
    for name in ("ETag", "Last-Modified"):
        value = headers.get(name)
        if value:
            return value
    return None


def _jar_from_header(jar, header):
    """Put the cookies from a Cookie header into the session's jar.

    Not into a header: requests drops a hand set Cookie header when it
    follows a redirect and rebuilds it from the jar alone, and the site
    now redirects every page fetch once (Next.js adds ?_rsc=). With the
    header the redirected request went out signed out, and came back as
    the signed out page with nothing in it.

    Scoped to the site's own host, so none of them go to the download
    cdn either
    """
    for part in header.split(";"):
        name, sep, value = part.strip().partition("=")
        if sep and name:
            jar.set(name.strip(), value.strip(), domain=SITE_HOST, path="/")


def _looks_like_cookie_header(value):
    """name=value, as opposed to a bare session value.

    A bare value can be base64 and end in = padding, but padding only
    ever comes at the end, so an = with something after it means a name
    """
    return re.match(r"^\s*[^=;\s\"]+=[^=]", value or "") is not None


def _package_version():
    try:
        from importlib.metadata import version

        return version("humblebundle-downloader")
    except Exception:
        return "dev"


USER_AGENT = (
    "humblebundle-downloader/{version} "
    "(+https://github.com/xtream1101/humblebundle-downloader)"
).format(version=_package_version())


def _content_range_total(response):
    """Total size out of a `Content-Range: bytes 100-999/1000` header"""
    try:
        raw = response.headers.get("Content-Range")
    except Exception:
        return None
    if not raw or "/" not in raw:
        return None
    return _coerce_size(raw.rsplit("/", 1)[-1].strip())


def _retry_after_seconds(response, default=DEFAULT_COOLDOWN):
    """Retry-After is either a number of seconds or an http date"""
    raw = None
    try:
        raw = response.headers.get("Retry-After")
    except Exception:
        return default

    if not raw:
        return default

    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        pass

    try:
        when = email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return default
    if when is None:
        return default

    if when.tzinfo is None:
        delta = when - datetime.datetime.now()
    else:
        delta = when - datetime.datetime.now(when.tzinfo)
    return max(0, int(delta.total_seconds()))
