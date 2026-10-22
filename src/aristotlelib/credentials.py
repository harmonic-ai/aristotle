"""Storage of the CLI's API key on disk: location, permissions, and format."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path

from aristotlelib.endpoints import WEB_URL, get_web_url

CREDENTIALS_FILENAME = "credentials.json"


def _home() -> Path:
    """``Path.home()``, raising OSError instead of RuntimeError when no home directory is known."""
    try:
        return Path.home()
    except (RuntimeError, KeyError) as e:
        raise OSError("Could not determine the home directory for the credentials file.") from e


def config_dir() -> Path:
    """The directory holding the CLI's own state."""
    if os.name == "nt":
        appdata = os.environ.get("APPDATA")
        return Path(appdata) / "aristotle" if appdata else _home() / ".aristotle"
    return Path(os.environ.get("XDG_CONFIG_HOME") or _home() / ".config") / "aristotle"


def credentials_path() -> Path:
    origin = get_web_url()
    if origin == WEB_URL:
        return config_dir() / CREDENTIALS_FILENAME
    return config_dir() / f"credentials-{sha256(origin.encode()).hexdigest()}.json"


def _stored() -> dict[str, object] | None:
    """The credentials file's JSON object, or None if there is no readable one."""
    try:
        data = json.loads(credentials_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def read_api_key() -> str | None:
    """The stored key, or None if there is no readable one.

    A missing or damaged file is not worth an exception: either way the caller's next move is
    the same, which is to tell the user to log in.
    """
    data = _stored()
    api_key = None if data is None else data.get("api_key")
    return api_key if isinstance(api_key, str) and api_key else None


def read_device_api_key() -> str | None:
    """The stored key if a device login made it, as write_api_key records with ``device_login``; else None."""
    data = _stored()
    api_key = data.get("api_key") if data is not None and data.get("device_login") is True else None
    return api_key if isinstance(api_key, str) and api_key else None


def write_api_key(api_key: str, *, name: str | None = None, device_login: bool = False) -> Path:
    """Store the key in a file only this user can read, recording whether a device login made it, and return its
    path."""
    path = credentials_path()
    # A new directory is 0700, an existing one keeps its mode; on Windows 0o700 would replace %APPDATA%'s per-user ACL.
    path.parent.mkdir(mode=0o777 if os.name == "nt" else 0o700, parents=True, exist_ok=True)
    payload = {
        "api_key": api_key,
        "name": name,
        "device_login": device_login,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    # Written to a private (0600) temporary file that is renamed over the old one, so the key
    # is never in a file with wider permissions and a failed write leaves the old file intact.
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{CREDENTIALS_FILENAME}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
            f.write("\n")
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return path


def delete_credentials() -> bool:
    """Remove the stored key. Returns False if there was nothing to remove.

    Errors other than a missing file or an unknown home directory propagate.
    """
    try:
        path = credentials_path()
    except OSError:
        return False
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True
