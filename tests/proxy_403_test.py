"""HTTP 403 from a TLS-inspecting loopback proxy must retain its diagnosis through every command."""

import asyncio
import io
import logging
import os
import re
import socket
import ssl
import subprocess
import sys
import threading
from base64 import b64encode
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.request import getproxies_environment

import httpx
import pytest

from aristotlelib import api_request, credentials
from aristotlelib.cli import controller, login_flow
from aristotlelib.project import Project

PROJECT_ID = "3f1002f7-0000-4000-8000-000000000000"
KEY = "arstl_test-secret-never-print"
ACCOUNT_MESSAGE = (
    "Error: this account doesn't have access (403)\n\n"
    "Hint: it may be awaiting approval or no longer in the key's organization. Contact aristotle@harmonic.fun."
)
UNKNOWN_MESSAGE = (
    "Error: request refused (403 Forbidden)\n\n"
    "Hint: this may come from a proxy or firewall on your network. Run with --verbose to see the response headers."
)
PROXY_MESSAGE = (
    "Error: request blocked by a proxy (403 Forbidden, via 127.0.0.1)\n\n"
    "Hint: ask your network admin to allow localhost."
)


@pytest.fixture(autouse=True)
def no_machine_proxy_settings(monkeypatch):
    for name in (*api_request.PROXY_VARIABLES, "HTTP_PROXY", "http_proxy", "NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    # Windows and macOS can also supply a system proxy; the loopback fixture sets an environment proxy explicitly.
    monkeypatch.setattr(api_request, "getproxies", getproxies_environment)


@pytest.fixture
def inspecting_proxy(monkeypatch, tmp_path):
    """Accept a real CONNECT tunnel and TLS connection, then answer as a corporate proxy without reaching Core."""
    tls = Path(__file__).parent / "tls"
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(tls / "server-of-ca-with-keyusage.pem", tls / "server-of-ca-with-keyusage.key")
    response = [b"<html><head><title>Corporate policy: Aristotle blocked</title></head><body>Denied</body></html>"]
    content_type = ["text/html; charset=utf-8"]
    requests: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_CONNECT(self) -> None:
            if response[0] is None:
                requests.append(f"CONNECT {self.path}")
                self.send_response(403)
                self.send_header("Content-Length", "0")
                self.end_headers()
                self.close_connection = True
                return
            self.send_response(200)
            self.end_headers()
            self.close_connection = True
            with context.wrap_socket(self.connection, server_side=True) as connection:
                head = b""
                while not head.endswith(b"\r\n\r\n"):
                    chunk = connection.recv(1)
                    if not chunk:
                        return
                    head += chunk
                # The body too: closing with any of the request unread sends a reset, which can discard the reply.
                length = re.search(rb"(?im)^content-length: *(\d+)", head)
                unread = int(length[1]) if length else 0
                while unread:
                    chunk = connection.recv(unread)
                    if not chunk:
                        return
                    unread -= len(chunk)
                requests.append(head.split(b"\r\n", 1)[0].decode())
                connection.sendall(
                    b"HTTP/1.1 403 Forbidden\r\n"
                    + f"Content-Type: {content_type[0]}\r\nContent-Length: {len(response[0])}\r\n".encode()
                    + b"Connection: close\r\n\r\n"
                    + response[0]
                )

        def log_message(self, format, *args) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    for name in (
        *api_request.PROXY_VARIABLES,
        *api_request.CA_BUNDLE_VARIABLES,
        "HTTP_PROXY",
        "http_proxy",
        "NO_PROXY",
        "no_proxy",
        "SSL_CERT_DIR",
    ):
        monkeypatch.delenv(name, raising=False)
    address = f"http://127.0.0.1:{server.server_port}"
    monkeypatch.setenv("HTTPS_PROXY", address)
    monkeypatch.setenv("ARISTOTLE_CA_BUNDLE", str(tls / "ca-with-keyusage.pem"))
    monkeypatch.setenv("ARISTOTLE_WEB_URL", "https://localhost")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.delenv("ARISTOTLE_API_KEY", raising=False)
    monkeypatch.setattr(api_request, "API_KEY", KEY)
    try:
        yield response, content_type, requests, address
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


async def test_login_uses_the_requested_proxy_message_without_blaming_the_account(inspecting_proxy):
    _, _, requests, _ = inspecting_proxy
    with pytest.raises(login_flow.LoginError) as raised:
        await asyncio.wait_for(login_flow.login(api_key=KEY), timeout=10)

    message = str(raised.value)
    assert raised.value.exit_code == 5
    assert message == PROXY_MESSAGE
    assert "recognized" not in message
    assert "awaiting approval" not in message
    assert KEY not in message
    assert credentials.read_api_key() is None
    assert requests == ["GET /api/v3/project?limit=1 HTTP/1.1"]


@pytest.mark.parametrize("detail", [b'"Account is not approved"', b'{"code":"access_denied","message":"Denied"}'])
async def test_aristotle_json_403_uses_the_requested_account_message(inspecting_proxy, detail):
    response, content_type, requests, _ = inspecting_proxy
    response[0] = b'{"detail":' + detail + b"}"
    content_type[0] = "Application/JSON; charset=utf-8"
    with pytest.raises(login_flow.LoginError) as raised:
        await asyncio.wait_for(login_flow.login(api_key=KEY), timeout=10)

    assert raised.value.exit_code == 5
    assert str(raised.value) == ACCOUNT_MESSAGE
    assert credentials.read_api_key() is None
    assert len(requests) == 1


@pytest.mark.parametrize("detail", [b'"Account is not approved"', b'{"code":"access_denied","message":"Denied"}'])
def test_a_listing_uses_the_requested_account_message(detail, inspecting_proxy, monkeypatch, caplog):
    response, content_type, _, _ = inspecting_proxy
    response[0] = b'{"detail":' + detail + b"}"
    content_type[0] = "application/json"
    monkeypatch.setattr("sys.argv", ["aristotle", "list"])

    assert controller.main() == 4
    assert ACCOUNT_MESSAGE in caplog.text
    assert "proxy" not in caplog.text.lower()


@pytest.mark.parametrize(
    "argv",
    [
        ["list"],
        ["show", PROJECT_ID],
        ["show", PROJECT_ID, "--task", PROJECT_ID],
        ["tasks", PROJECT_ID],
        ["download", PROJECT_ID],
        ["continue", PROJECT_ID, "Prove it"],
        ["cancel", PROJECT_ID],
        ["cancel", "--project-id", PROJECT_ID],
        ["cancel", "--task-id", PROJECT_ID],
        ["questions", PROJECT_ID],
        ["questions", PROJECT_ID, "--task", PROJECT_ID],
        ["properties", PROJECT_ID],
        ["properties", PROJECT_ID, "--status", "PROVED"],
        ["property", PROJECT_ID, "Spec.Anything"],
        ["rename", PROJECT_ID, "New name"],
    ],
    ids=lambda argv: " ".join(value for value in argv if value != PROJECT_ID),
)
@pytest.mark.parametrize("known_proxy", [False, True])
def test_commands_preserve_the_refusal_and_existing_exit_code(
    argv, known_proxy, inspecting_proxy, api_server, monkeypatch, caplog
):
    if not known_proxy:
        monkeypatch.delenv("HTTPS_PROXY")
        api_server(httpx.Response(403, text="<title>Blocked</title>"))
    monkeypatch.setattr("sys.argv", ["aristotle", *argv])
    caplog.set_level(logging.ERROR)
    with patch("sys.stdin", io.StringIO("")):
        try:
            code = controller.main()
        except SystemExit as raised:
            code = raised.code

    assert code == 4
    assert (PROXY_MESSAGE if known_proxy else UNKNOWN_MESSAGE) in caplog.text
    assert "Response headers" not in caplog.text
    assert "Corporate policy: Aristotle blocked" not in caplog.text
    assert "not found" not in caplog.text
    assert "account" not in caplog.text
    assert KEY not in caplog.text


def test_the_inspecting_proxy_reads_a_request_body_before_answering_so_its_reply_arrives_whole(inspecting_proxy):
    _, _, requests, address = inspecting_proxy
    context = ssl.create_default_context(cafile=Path(__file__).parent / "tls" / "ca-with-keyusage.pem")
    with socket.create_connection(("127.0.0.1", int(address.rsplit(":", 1)[1])), timeout=10) as raw:
        raw.sendall(b"CONNECT localhost:443 HTTP/1.1\r\n\r\n")
        established = b""
        while not established.endswith(b"\r\n\r\n"):
            chunk = raw.recv(1)
            assert chunk, established
            established += chunk
        with context.wrap_socket(raw, server_hostname="localhost") as tunnel:
            # Head and body in records of their own, as httpx sends them.
            tunnel.sendall(b"POST /api/v3/project HTTP/1.1\r\nHost: localhost\r\nContent-Length: 2\r\n\r\n")
            tunnel.sendall(b"{}")
            reply = b""
            while chunk := tunnel.recv(65536):
                reply += chunk

    assert reply.startswith(b"HTTP/1.1 403 Forbidden\r\n")
    assert reply.endswith(b"<body>Denied</body></html>")
    assert requests == ["POST /api/v3/project HTTP/1.1"]


async def test_file_download_preserves_the_proxy_diagnosis(inspecting_proxy, tmp_path):
    project = Project(
        project_id=PROJECT_ID,
        created_at="2026-10-15T00:00:00",
        last_updated="2026-10-15T00:00:00",
        description=None,
        status=2,
        has_input=True,
        has_files=True,
    )
    with pytest.raises(api_request.AristotleAPIError, match="request blocked by a proxy") as raised:
        async for _ in project.save_files(["trace.vcd"], tmp_path):
            pytest.fail("A refused file request must not yield a file")

    assert not isinstance(raised.value, api_request.AristotleNotFoundError)
    assert not (tmp_path / "trace.vcd").exists()


@pytest.mark.parametrize(
    ("content_type", "body", "excerpt"),
    [
        ("text/html", "<html><title>Blocked &amp; refused</title><script>hidden</script></html>", "Blocked & refused"),
        ("text/html", "<style>hidden</style><body>Company <b>policy</b> refused</body>", "Company policy refused"),
        ("text/plain", "Blocked by firewall\nContact IT", "Blocked by firewall Contact IT"),
        ("application/json", '{"message":"Blocked by proxy"}', "Blocked by proxy"),
        ("application/json", '{"detail":"Blocked"', "Blocked"),
        ("application/json", '["Blocked"]', "Blocked"),
        ("text/html", '{"detail":"Forbidden"}', "Forbidden"),
        ("application/json", '{"detail":null}', "null"),
        ("application/json", '{"detail":{"other":"Blocked"}}', "Blocked"),
        ("application/json", '{"detail":""}', "detail"),
        ("", "", ""),
    ],
)
async def test_unknown_403_uses_the_requested_message_and_logs_an_excerpt_only_at_debug(
    content_type, body, excerpt, api_server, caplog
):
    caplog.set_level(logging.DEBUG, logger="aristotle.http")
    api_server(httpx.Response(403, content=body.encode(), headers={"Content-Type": content_type}))
    with pytest.raises(api_request.AristotleAPIError) as raised:
        async with api_request.AristotleRequestClient() as client:
            await client.get("/project")

    message = str(raised.value)
    assert message == UNKNOWN_MESSAGE
    assert excerpt in caplog.text
    assert "hidden" not in caplog.text
    assert "<html>" not in caplog.text


async def test_verbose_headers_and_excerpt_are_bounded_printable_and_redact_credentials(
    api_server, monkeypatch, caplog
):
    caplog.set_level(logging.DEBUG, logger="aristotle.http")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy-user:proxy-password@proxy.corp:3128/private?token=secret")
    body = f"<title>Blocked {KEY} proxy-user proxy-password \x1b[31m {'x' * 500}</title>"
    api_server(
        httpx.Response(
            403,
            text=body,
            headers={"X-Block-Reason": body, "Set-Cookie": "session=secret-cookie", "X-API-Key": KEY},
        )
    )
    with pytest.raises(api_request.AristotleAPIError) as raised:
        async with api_request.AristotleRequestClient() as client:
            await client.get("/project")

    message = str(raised.value)
    assert message == (
        "Error: request blocked by a proxy (403 Forbidden, via proxy.corp)\n\n"
        "Hint: ask your network admin to allow aristotle.harmonic.fun."
    )
    diagnostics = caplog.text
    for secret in (KEY, "proxy-user", "proxy-password", "token=secret", "secret-cookie", "test-key"):
        assert secret not in diagnostics + message
    assert "\x1b" not in diagnostics
    assert "\\x1b[31m" in diagnostics
    assert "[redacted]" in diagnostics
    assert "…" in diagnostics
    assert all(len(record.message) < 350 for record in caplog.records if record.name == "aristotle.http")


async def test_verbose_redacts_cookie_values_echoed_in_the_body_and_other_headers(api_server, caplog):
    caplog.set_level(logging.DEBUG, logger="aristotle.http")
    session = "echoed-session-credential"
    csrf = "quoted csrf credential"
    expired = "new-response-cookie-credential"

    def echo_cookies(request):
        cookie = request.headers["Cookie"]
        assert session in cookie
        assert csrf in cookie
        echoed = f"{cookie}; expired={expired}"
        return httpx.Response(
            403,
            text=f"<title>{echoed}</title>",
            headers=[("X-Echo", echoed), ("Set-Cookie", f"expired={expired}; Max-Age=0; Path=/")],
        )

    requests = api_server(
        httpx.Response(
            200,
            json={},
            headers=[("Set-Cookie", f"session={session}; Path=/"), ("Set-Cookie", f'csrf="{csrf}"; Path=/')],
        ),
        echo_cookies,
    )
    async with api_request.AristotleRequestClient() as client:
        await client.get("/project")
        with pytest.raises(api_request.AristotleAPIError):
            await client.get("/project")

    assert len(requests) == 2
    for secret in (session, csrf, expired):
        assert secret not in caplog.text
    assert "x-echo:" in caplog.text
    assert "Response excerpt:" in caplog.text
    assert "[redacted]" in caplog.text


@pytest.mark.parametrize("value", ["partitioned-session-credential", '"quoted partitioned credential"'])
async def test_verbose_redacts_partitioned_cookie_values_echoed_in_the_response(value, api_server, caplog):
    caplog.set_level(logging.DEBUG, logger="aristotle.http")
    decoded = value.strip('"')

    def check_cookie_was_accepted(request):
        assert value in request.headers["Cookie"]
        return httpx.Response(200, json={})

    requests = api_server(
        httpx.Response(
            403,
            text=f"<title>Blocked {decoded}</title>",
            headers={
                "Set-Cookie": f"session={value}; Secure; HttpOnly; Path=/; SameSite=None; Partitioned",
                "X-Echo": decoded,
            },
        ),
        check_cookie_was_accepted,
    )
    async with api_request.AristotleRequestClient() as client:
        with pytest.raises(api_request.AristotleAPIError) as raised:
            await client.get("/project")
        await client.get("/project")

    assert str(raised.value) == UNKNOWN_MESSAGE
    assert len(requests) == 2
    assert decoded not in caplog.text
    assert "x-echo: [redacted]" in caplog.text
    assert "Response excerpt: Blocked [redacted]" in caplog.text


@pytest.mark.parametrize(
    "credentials",
    ["proxy-user:proxy-password", "proxy%40user:pass%3Aword", "proxy-%C3%A9:secret-%C3%A9"],
)
async def test_verbose_redacts_encoded_proxy_credentials_echoed_in_a_response(
    credentials, api_server, monkeypatch, caplog
):
    caplog.set_level(logging.DEBUG, logger="aristotle.http")
    proxy = httpx.URL(f"http://{credentials}@proxy.corp:3128")
    monkeypatch.setenv("HTTPS_PROXY", str(proxy))
    encoded = b64encode(f"{proxy.username}:{proxy.password}".encode()).decode()
    echoed = f"Basic {encoded}"
    api_server(httpx.Response(403, text=f"<title>{echoed}</title>", headers={"X-Echo": echoed}))

    with pytest.raises(api_request.AristotleAPIError):
        async with api_request.AristotleRequestClient() as client:
            await client.get("/project")

    assert encoded not in caplog.text
    assert proxy.username not in caplog.text
    assert proxy.password not in caplog.text
    assert "x-echo: Basic [redacted]" in caplog.text
    assert "Response excerpt: Basic [redacted]" in caplog.text


async def test_an_event_stream_preserves_the_same_403_diagnosis(api_server):
    api_server(httpx.Response(403, text="<title>Event stream blocked by firewall</title>"))
    with pytest.raises(api_request.AristotleAPIError) as raised:
        async with api_request.AristotleRequestClient() as client:
            async for _ in client.stream_sse("/project/events"):
                pytest.fail("A refused event stream must not yield events")
    assert str(raised.value) == UNKNOWN_MESSAGE


@pytest.mark.parametrize("argv", [["--verbose", "list"], ["list", "--verbose"], ["login", "--verbose"]])
def test_verbose_works_before_or_after_the_command_and_shows_response_headers(
    argv, inspecting_proxy, monkeypatch, caplog, capsys
):
    monkeypatch.setattr("sys.argv", ["aristotle", *argv])
    with patch("sys.stdin", io.StringIO(f"{KEY}\n")):
        try:
            code = controller.main()
        except SystemExit as raised:
            code = raised.code

    assert code == (5 if "login" in argv else 4)
    assert "Response headers (403 Forbidden):" in caplog.text
    assert "content-type: text/html; charset=utf-8" in caplog.text
    assert "Response excerpt: Corporate policy: Aristotle blocked" in caplog.text
    assert PROXY_MESSAGE in caplog.text + capsys.readouterr().err
    assert KEY not in caplog.text


@pytest.mark.parametrize("known_proxy", [False, True])
def test_nested_setup_verbose_reports_the_refusal_and_headers(
    known_proxy, inspecting_proxy, api_server, monkeypatch, tmp_path, caplog
):
    if not known_proxy:
        monkeypatch.delenv("HTTPS_PROXY")
        api_server(httpx.Response(403, text="<title>Blocked</title>"))
    monkeypatch.setattr(
        "sys.argv",
        ["aristotle", "ci", "setup", "--from", PROJECT_ID, "--directory", str(tmp_path), "--verbose"],
    )

    assert controller.main() == 4
    assert (PROXY_MESSAGE if known_proxy else UNKNOWN_MESSAGE) in caplog.text
    assert "Response headers (403 Forbidden):" in caplog.text
    assert not (tmp_path / "aristotle.toml").exists()


@pytest.mark.parametrize(
    "no_proxy",
    [
        "aristotle.harmonic.fun",
        ".harmonic.fun",
        "*",
        "localhost,*",
        "fd00::/8,*",
        "https://aristotle.harmonic.fun",
        "harmonic.fun:8443",
    ],
)
async def test_a_bypassed_proxy_does_not_receive_blame(no_proxy, api_server, monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.corp:3128")
    monkeypatch.setenv("NO_PROXY", no_proxy)
    if no_proxy.endswith(":8443"):
        monkeypatch.setenv("ARISTOTLE_WEB_URL", "https://aristotle.harmonic.fun:8443")
    api_server(httpx.Response(403, text="<title>Blocked</title>"))
    with pytest.raises(api_request.AristotleAPIError) as raised:
        async with api_request.AristotleRequestClient() as client:
            await client.get("/project")

    assert str(raised.value) == UNKNOWN_MESSAGE


def test_a_global_no_proxy_wildcard_preserves_the_cli_refusal_message_and_exit_code(api_server, monkeypatch, caplog):
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.corp:3128")
    monkeypatch.setenv("NO_PROXY", "fd00::/8,*")
    monkeypatch.setattr("sys.argv", ["aristotle", "list", "--verbose"])
    api_server(httpx.Response(403, text="<title>Blocked</title>"))

    assert controller.main() == 4
    assert UNKNOWN_MESSAGE in caplog.text
    assert "Response headers (403 Forbidden):" in caplog.text


def test_error_formatter_preserves_the_requested_error_prefix():
    record = logging.LogRecord("aristotle", logging.ERROR, __file__, 1, PROXY_MESSAGE, (), None)
    assert controller._LogFormatter().format(record) == PROXY_MESSAGE


@pytest.mark.parametrize("method", ["get", "post"])
async def test_a_proxy_refusing_connect_has_the_same_message_and_is_not_retried(method, api_server, monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.corp:3128")
    requests = api_server(httpx.ProxyError("403 Forbidden"))
    with pytest.raises(api_request.AristotleAPIError) as raised:
        async with api_request.AristotleRequestClient() as client:
            await getattr(client, method)("/project")

    assert str(raised.value) == (
        "Error: request blocked by a proxy (403 Forbidden, via proxy.corp)\n\n"
        "Hint: ask your network admin to allow aristotle.harmonic.fun."
    )
    assert raised.value.status_code is None
    assert isinstance(raised.value, api_request.AristotleNotProcessedError) == (method == "post")
    assert len(requests) == 1


async def test_a_real_proxy_refusing_connect_does_not_attempt_tls_or_store_the_key(inspecting_proxy):
    response, _, requests, _ = inspecting_proxy
    response[0] = None
    with pytest.raises(login_flow.LoginError) as raised:
        await asyncio.wait_for(login_flow.login(api_key=KEY), timeout=10)

    assert str(raised.value) == PROXY_MESSAGE
    assert raised.value.exit_code == 3
    assert requests == ["CONNECT localhost:443"]
    assert credentials.read_api_key() is None


async def test_a_device_login_behind_a_proxy_refusing_connect_reports_the_proxy(inspecting_proxy):
    response, _, requests, _ = inspecting_proxy
    response[0] = None
    with patch("sys.stdin", io.StringIO("")), pytest.raises(login_flow.LoginError) as raised:
        await asyncio.wait_for(login_flow.login(), timeout=10)

    assert str(raised.value) == PROXY_MESSAGE
    assert raised.value.exit_code == 3
    assert requests == ["CONNECT localhost:443"]
    assert credentials.read_api_key() is None


@pytest.mark.parametrize("command", ["login", "list"])
@pytest.mark.parametrize("verbose", [False, True])
def test_real_cli_stderr_has_the_exact_error_output(command, verbose, inspecting_proxy):
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from aristotlelib.cli.controller import main; raise SystemExit(main())",
            command,
            *(["--verbose"] if verbose else []),
        ],
        input=f"{KEY}\n",
        env={**os.environ, "ARISTOTLE_API_KEY": KEY},
        capture_output=True,
        text=True,
        timeout=15,
    )

    assert result.returncode == (5 if command == "login" else 4)
    assert result.stdout == ""
    assert result.stderr.endswith(PROXY_MESSAGE + "\n")
    assert ("Response headers (403 Forbidden):" in result.stderr) == verbose
    assert ("Corporate policy: Aristotle blocked" in result.stderr) == verbose
    assert "ERROR - Error:" not in result.stderr
    assert KEY not in result.stderr


async def test_an_event_stream_connect_refusal_keeps_the_proxy_message_and_retry_count(inspecting_proxy, monkeypatch):
    response, _, requests, _ = inspecting_proxy
    response[0] = None
    monkeypatch.setattr(api_request, "STREAM_RECONNECT_SECONDS", 0)
    with pytest.raises(api_request.AristotleAPIError) as raised:
        async with api_request.AristotleRequestClient() as client:
            async for _ in client.stream_sse("/project/events", max_retries=2):
                pytest.fail("A refused event stream must not yield events")

    assert str(raised.value) == PROXY_MESSAGE
    assert raised.value.status_code is None
    assert requests == ["CONNECT localhost:443", "CONNECT localhost:443"]
