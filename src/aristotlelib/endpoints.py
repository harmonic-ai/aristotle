"""The web origin used by both the web app and its API."""

import os
from ipaddress import ip_address
from urllib.parse import urlsplit

WEB_URL = "https://aristotle.harmonic.fun"


def get_web_url() -> str:
    """Return a canonical web origin for links, requests, and stored credentials."""
    value = os.environ.get("ARISTOTLE_WEB_URL", "").strip()
    if not value:
        return WEB_URL

    invalid_origin = "ARISTOTLE_WEB_URL must be an HTTP(S) origin without credentials, a path, query, or fragment."
    if any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError(invalid_origin)
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        port = parsed.port
    except ValueError as e:
        raise ValueError(invalid_origin) from e
    if (
        parsed.scheme not in ("http", "https")
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or "?" in value
        or "#" in value
        or parsed.netloc.endswith(":")
        or "%" in host
        or "\\" in host
    ):
        raise ValueError(invalid_origin)

    try:
        address = ip_address(host)
    except ValueError:
        address = None
    if parsed.scheme == "http" and host != "localhost" and not (address and address.is_loopback):
        raise ValueError("ARISTOTLE_WEB_URL must use HTTPS unless its host is loopback.")

    host = str(address) if address else host
    authority = f"[{host}]" if ":" in host else host
    if port is not None and port != (443 if parsed.scheme == "https" else 80):
        authority = f"{authority}:{port}"
    return f"{parsed.scheme}://{authority}"
