"""Tests for aristotlelib.credentials."""

import json
import os
import stat
from pathlib import Path

import pytest

from aristotlelib import api_request, credentials


@pytest.fixture
def config_home(tmp_path, monkeypatch):
    """Point the credentials file at a temporary directory, not the developer's own."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.delenv("ARISTOTLE_WEB_URL", raising=False)
    return tmp_path


def test_read_api_key_returns_none_without_a_file(config_home):
    assert credentials.read_api_key() is None


def test_write_then_read_round_trips(config_home):
    path = credentials.write_api_key("test-key", name="laptop")

    assert credentials.read_api_key() == "test-key"
    assert json.loads(path.read_text())["name"] == "laptop"


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_written_key_is_readable_only_by_its_owner(config_home):
    path = credentials.write_api_key("test-key")

    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_write_tightens_permissions_on_an_existing_file(config_home):
    path = credentials.write_api_key("old-key")
    path.chmod(0o644)

    credentials.write_api_key("new-key")

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert credentials.read_api_key() == "new-key"


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_a_new_config_directory_is_private_under_a_group_writable_umask(config_home):
    umask = os.umask(0o002)
    try:
        path = credentials.write_api_key("test-key")
    finally:
        os.umask(umask)

    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_an_existing_config_directory_keeps_its_permissions(config_home):
    directory = credentials.config_dir()
    directory.mkdir()
    directory.chmod(0o775)

    credentials.write_api_key("test-key")

    assert stat.S_IMODE(directory.stat().st_mode) == 0o775


def test_a_key_a_device_login_made_reads_as_one(config_home):
    path = credentials.write_api_key("device-key", name="aristotle CLI on laptop", device_login=True)

    assert credentials.read_api_key() == "device-key"
    assert credentials.read_device_api_key() == "device-key"
    assert json.loads(path.read_text())["device_login"] is True


def test_a_key_given_or_pasted_doesnt_read_as_one_a_device_login_made(config_home):
    path = credentials.write_api_key("pasted-key")

    assert credentials.read_api_key() == "pasted-key"
    assert credentials.read_device_api_key() is None
    assert json.loads(path.read_text())["device_login"] is False


@pytest.mark.parametrize(
    "stored",
    [{"api_key": "old-key", "name": None}, {"api_key": "old-key", "device_login": "true"}],
    ids=["written before device logins", "not a boolean"],
)
def test_a_file_that_doesnt_record_a_device_login_reads_as_a_key_no_device_login_made(config_home, stored):
    path = credentials.credentials_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(stored))

    assert credentials.read_api_key() == "old-key"
    assert credentials.read_device_api_key() is None


def test_a_damaged_file_reads_as_no_key(config_home):
    path = credentials.credentials_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json")

    assert credentials.read_api_key() is None


def test_delete_reports_whether_there_was_anything_to_delete(config_home):
    assert credentials.delete_credentials() is False

    credentials.write_api_key("test-key")

    assert credentials.delete_credentials() is True
    assert credentials.read_api_key() is None


def test_credentials_are_isolated_by_web_origin(config_home, monkeypatch):
    monkeypatch.setattr(api_request, "API_KEY", None)
    monkeypatch.delenv("ARISTOTLE_API_KEY", raising=False)
    default_path = credentials.write_api_key("default-key")
    assert default_path.name == "credentials.json"

    monkeypatch.setenv("ARISTOTLE_WEB_URL", "https://alt.example.test/")
    configured_path = credentials.credentials_path()
    assert configured_path != default_path
    assert configured_path.parent == default_path.parent
    assert credentials.read_api_key() is None
    assert credentials.write_api_key("configured-key") == configured_path
    assert api_request.get_api_key() == "configured-key"

    monkeypatch.setenv("ARISTOTLE_WEB_URL", "https://other.example.test")
    assert credentials.read_api_key() is None

    monkeypatch.setenv("ARISTOTLE_WEB_URL", "https://alt.example.test")
    assert credentials.read_api_key() == "configured-key"
    assert credentials.delete_credentials() is True
    assert not configured_path.exists()

    monkeypatch.delenv("ARISTOTLE_WEB_URL")
    assert credentials.read_api_key() == "default-key"
    assert default_path.exists()


def test_equivalent_origin_spellings_share_one_credential_file(config_home, monkeypatch):
    default_path = credentials.write_api_key("default-key")
    monkeypatch.setenv("ARISTOTLE_WEB_URL", " HTTPS://ARISTOTLE.HARMONIC.FUN:443/ ")
    assert credentials.credentials_path() == default_path
    assert credentials.read_api_key() == "default-key"

    monkeypatch.setenv("ARISTOTLE_WEB_URL", " HTTPS://ALT.EXAMPLE.TEST:443/ ")
    configured_path = credentials.write_api_key("configured-key")
    monkeypatch.setenv("ARISTOTLE_WEB_URL", "https://alt.example.test")
    assert credentials.credentials_path() == configured_path
    assert credentials.read_api_key() == "configured-key"

    monkeypatch.setenv("ARISTOTLE_WEB_URL", "https://alt.example.test:8443")
    assert credentials.credentials_path() != configured_path
    assert credentials.read_api_key() is None


def test_get_api_key_falls_back_to_the_stored_key(config_home, monkeypatch):
    monkeypatch.setattr(api_request, "API_KEY", None)
    monkeypatch.delenv("ARISTOTLE_API_KEY", raising=False)
    credentials.write_api_key("stored-key")

    assert api_request.get_api_key() == "stored-key"


def test_the_environment_outranks_the_stored_key(config_home, monkeypatch):
    monkeypatch.setattr(api_request, "API_KEY", None)
    monkeypatch.setenv("ARISTOTLE_API_KEY", "env-key")
    credentials.write_api_key("stored-key")

    assert api_request.get_api_key() == "env-key"


def test_get_api_key_points_at_login_when_there_is_nothing(config_home, monkeypatch):
    monkeypatch.setattr(api_request, "API_KEY", None)
    monkeypatch.delenv("ARISTOTLE_API_KEY", raising=False)

    with pytest.raises(ValueError, match="aristotle login"):
        api_request.get_api_key()


def test_no_home_directory_reads_as_no_key(monkeypatch):
    """No home directory reads as no stored key rather than a RuntimeError."""
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.delenv("ARISTOTLE_WEB_URL", raising=False)

    def no_home():
        raise RuntimeError("Could not determine home directory.")

    monkeypatch.setattr(Path, "home", staticmethod(no_home))

    assert credentials.read_api_key() is None
    assert credentials.delete_credentials() is False
    api_request.API_KEY = None
    monkeypatch.delenv("ARISTOTLE_API_KEY", raising=False)
    with pytest.raises(ValueError, match="API key has not been set"):
        api_request.get_api_key()


def test_a_failed_write_leaves_the_previous_key_and_no_temporary_file(config_home):
    credentials.write_api_key("first-key")

    with pytest.raises(TypeError):
        credentials.write_api_key("second-key", name=object())  # not JSON-serialisable

    assert credentials.read_api_key() == "first-key"
    assert [p.name for p in credentials.config_dir().iterdir()] == [credentials.CREDENTIALS_FILENAME]


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_the_key_is_never_written_into_a_wider_permission_file(config_home, monkeypatch):
    """An existing 0644 file is replaced, not truncated and rewritten in place."""
    path = credentials.write_api_key("first-key")
    os.chmod(path, 0o644)
    modes_seen: list[int] = []
    real_replace = os.replace

    def observing_replace(src, dst):
        modes_seen.append(stat.S_IMODE(os.stat(src).st_mode))
        assert "second-key" not in Path(dst).read_text()
        return real_replace(src, dst)

    monkeypatch.setattr(credentials.os, "replace", observing_replace)

    credentials.write_api_key("second-key")

    assert modes_seen == [0o600]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert credentials.read_api_key() == "second-key"


def test_a_removal_error_is_not_reported_as_nothing_to_remove(config_home, monkeypatch):
    credentials.write_api_key("stored-key")

    def refuse(self):
        raise PermissionError("read-only file system")

    monkeypatch.setattr(Path, "unlink", refuse)

    with pytest.raises(PermissionError):
        credentials.delete_credentials()
