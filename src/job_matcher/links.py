"""Shared validation for job links, including links recovered from old caches."""

import unicodedata
from urllib.parse import urlsplit


def is_http_url(value):
    if not isinstance(value, str) or not value or len(value) > 2000:
        return False
    if any(c.isspace() or unicodedata.category(c).startswith("C") for c in value):
        return False
    if "\\" in value:
        return False
    try:
        parts = urlsplit(value)
        host = parts.hostname
        # Accessing port also validates malformed/out-of-range port values.
        parts.port
        return bool(
            parts.scheme in {"http", "https"}
            and host
            and not any(c in host for c in "/@<>\"'[]()")
            and parts.username is None
            and parts.password is None
        )
    except ValueError:
        return False
