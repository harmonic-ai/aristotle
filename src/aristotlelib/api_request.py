import asyncio
import contextlib
import contextvars
import copy
import json
import logging
import math
import os
import random
import re
import ssl
import sys
import threading
import time
import weakref
from base64 import b64encode
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Generator
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from http.cookies import CookieError, SimpleCookie
from ipaddress import ip_address
from typing import Any, Literal, TypeVar
from urllib.request import getproxies

import certifi
import httpx
import socksio

from aristotlelib import credentials
from aristotlelib.cli.version import get_version
from aristotlelib.endpoints import WEB_URL, get_web_url
from aristotlelib.printable import printable

logger = logging.getLogger("aristotle")
http_logger = logging.getLogger("aristotle.http")

API_VERSION = "3"
DEFAULT_TIMEOUT_SECONDS = 30
# Longest the TCP and TLS handshakes may take before httpx raises ConnectTimeout.
CONNECT_TIMEOUT_SECONDS = 10
# Least rate assumed for an upload's bytes to reach the server: each is written within the write timeout, but the last
# can wait in this machine's and a proxy's buffers, megabytes of them, while the reply's read timeout runs.
UPLOAD_FLOOR_BYTES_PER_SECOND = 16 * 1024
# Longest the reply to an upload may take, however large the upload.
UPLOAD_READ_TIMEOUT_MAX_SECONDS = 600
# Longest an attempt may take to open its connection, a SOCKS proxy's handshake included, which httpcore doesn't time;
# under DEFAULT_TIMEOUT_SECONDS, so it ends a proxy's unanswered CONNECT before the read timeout does.
CONNECTION_DEADLINE_SECONDS = 25
# Most attempts at one request; AristotleRequestClient._make_request says which failures are retried.
RETRY_ATTEMPTS = 5
# Delay before the first retry; it doubles for each retry after that, and jitter adds up to as much again.
RETRY_BACKOFF_SECONDS = 1
# No retry starts more than this many seconds after the request's first attempt.
RETRY_DEADLINE_SECONDS = 90
# Delay before reopening an event stream; doubles for each connection in a row that received nothing.
STREAM_RECONNECT_SECONDS = 1
# Longest delay before reopening an event stream; a refusal whose Retry-After asks for more ends the stream.
STREAM_RECONNECT_MAX_SECONDS = 30
# How long the event stream of a run still live keeps retrying connections, from the first that failed: a network outage
# of several minutes doesn't end a wait on a run that goes on for hours.
STREAM_OUTAGE_SECONDS = 600
# Longest an event stream may go without a line before it counts as dropped; the server sends a keepalive every 30 s.
STREAM_READ_TIMEOUT_SECONDS = 90
# Least time between the starts of two requests from this process: at most five a second, half the per-second limit.
REQUEST_INTERVAL_SECONDS = 0.2
# Header that makes a create or an ask safe to repeat on servers that take it; they answer 409 while its first copy runs.
IDEMPOTENCY_KEY_HEADER = "Idempotency-Key"
# How the detail of that 409 begins; a 409 with any other detail is final.
KEY_BUSY_DETAIL = "A request with this Idempotency-Key is still in progress"
# The detail code of the 503 for a run whose organization's credits couldn't be read, before Aristotle acts on it.
BILLING_UNAVAILABLE = "BILLING_UNAVAILABLE"
# The detail code of the 409 for a run of an organization still setting up, before Aristotle acts on it.
SIGNUP_GRANT_PENDING = "SIGNUP_GRANT_PENDING"
# Prefix of the project-limit 429 detail from servers that refuse runs over the limit; PROJECT_LIMIT_FALLBACK replaces it.
LEGACY_PROJECT_LIMIT_DETAIL = "Too many inflight requests"
# How the 429 details for the limit on runs in progress or queued begin; any other 429 is a limit on requests.
PROJECT_LIMIT_DETAILS = ("Too many runs in progress", LEGACY_PROJECT_LIMIT_DETAIL)
PROJECT_LIMIT_FALLBACK = (
    "You have too many projects in progress. Cancel a running task or wait for one to complete before starting another."
)
_ACCOUNT_ACCESS_MESSAGE = (
    "Error: this account doesn't have access (403)\n\n"
    "Hint: it may be awaiting approval or no longer in the key's organization. Contact aristotle@harmonic.fun."
)

API_KEY: str | None = None


def get_base_url() -> str:
    return f"{get_web_url()}/api/v{API_VERSION}"


def billing_url() -> str:
    return f"{get_web_url()}/settings/billing"


def _later_event_id(current: str | None, candidate: str) -> str:
    """The later of two event ids, which the events stream sets to ISO creation times; an unparseable id wins."""
    if current is None:
        return candidate
    try:
        return candidate if datetime.fromisoformat(candidate) > datetime.fromisoformat(current) else current
    except (TypeError, ValueError):
        return candidate


class _MalformedKeyError(ValueError):
    """A key get_api_key() found and can't send; the message names where it came from, and never shows it."""


def _given_api_key() -> tuple[str, str] | None:
    """The key get_api_key() takes, as given, and where from, as api_key_source() names it; None without one."""
    # An explicit key beats the environment, and the environment beats the stored one, so a
    # CI variable or a --api-key flag still wins on a machine somebody has logged in on.
    if API_KEY:
        return API_KEY, "argument"
    if environment := os.environ.get("ARISTOTLE_API_KEY"):
        return environment, "environment"
    if stored := credentials.read_api_key():
        return stored, "stored"
    return None


def api_key_problem(api_key: str) -> str | None:
    """What keeps ``api_key``, already stripped of the whitespace around it, out of a request header, such as "contains
    a space"; None for printable ASCII without spaces."""
    if not api_key:
        return "is blank"
    if " " in api_key:
        return "contains a space"
    if not api_key.isascii():
        return "contains a non-ASCII character"
    if not api_key.isprintable():
        return "contains a control character"
    return None


def _malformed_key_message(source: str, problem: str) -> str:
    """The message for a key from ``source``, as api_key_source() names it, with ``problem``; outside the library, the
    argument is --api-key."""
    if source == "argument":
        where = "passed to aristotlelib.set_api_key()" if _surface == "library" else "given with --api-key"
        return f"The API key {where} {problem}."
    if source == "environment":
        return f"The API key in the ARISTOTLE_API_KEY environment variable {problem}."
    return (
        f"The API key in the credentials file {credentials.credentials_path()} {problem}. Log in again with:\n"
        "  aristotle login"
    )


def get_api_key() -> str:
    """The key from set_api_key(), else ARISTOTLE_API_KEY, else the credentials file, without the whitespace around it.

    Raises:
        ValueError: no source has a key; _MalformedKeyError, a subclass, when api_key_problem() refuses the key found.
    """
    given = _given_api_key()
    if given is None:
        raise ValueError(
            "API key has not been set. Run `aristotle login`, call aristotlelib.set_api_key(), "
            "or set the ARISTOTLE_API_KEY environment variable."
        )
    api_key, source = given
    api_key = api_key.strip()
    if (problem := api_key_problem(api_key)) is not None:
        raise _MalformedKeyError(_malformed_key_message(source, problem))
    return api_key


def api_key_source() -> str | None:
    """Where get_api_key() would take the key from: "argument", "environment", "stored", or None."""
    given = _given_api_key()
    return None if given is None else given[1]


def set_api_key(api_key: str) -> str:
    global API_KEY
    API_KEY = api_key
    return API_KEY


CA_BUNDLE_VARIABLES = (
    "ARISTOTLE_CA_BUNDLE",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
    "SSL_CERT_FILE",
)
"""Checked in order for a custom certificate authority bundle."""

PROXY_VARIABLES = ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy")

_logged_environment = False
_ignored_ca_bundles: set[tuple[str, str]] = set()


def _ca_bundle_setting() -> tuple[str, str] | None:
    """The variable naming the first configured CA bundle that actually exists on disk, and its path."""
    for variable in CA_BUNDLE_VARIABLES:
        path = os.environ.get(variable)
        if not path:
            continue
        if os.path.isfile(path):
            return variable, path
        # Every request builds a client; warn once per setting, not once per poll.
        if (variable, path) not in _ignored_ca_bundles:
            _ignored_ca_bundles.add((variable, path))
            logger.warning(f"{variable} points at {path}, which is not a file. Ignoring it.")
    return None


def _ca_bundle() -> str | None:
    """The first configured CA bundle that actually exists on disk."""
    setting = _ca_bundle_setting()
    return setting[1] if setting else None


def _ssl_context(ca_bundle: str | None) -> ssl.SSLContext:
    """Built here, not by httpx: with trust_env it would open a missing SSL_CERT_FILE that _ca_bundle skipped."""
    if ca_bundle is not None:
        context = ssl.create_default_context(cafile=ca_bundle)
    elif cert_dir := os.environ.get("SSL_CERT_DIR"):
        context = ssl.create_default_context(capath=cert_dir)
    else:
        return ssl.create_default_context(cafile=certifi.where())
    # From Python 3.13 the default context also demands extensions, such as keyUsage, that many company certificate
    # authorities lack; one the user names is trusted as curl and browsers trust it.
    context.verify_flags &= ~ssl.VERIFY_X509_STRICT
    return context


def _log_environment_once(ca_bundle: str | None) -> None:
    """Report proxy and CA configuration once per process."""
    global _logged_environment
    if _logged_environment:
        return
    _logged_environment = True

    if ca_bundle:
        logger.info(f"Using the certificate authority bundle at {ca_bundle}")
    proxy = next((os.environ[v] for v in PROXY_VARIABLES if os.environ.get(v)), None)
    if proxy:
        # The value can carry credentials, so report only that a proxy is configured.
        logger.info("Using the HTTPS proxy from the environment.")


def upload_timeout(size: int) -> httpx.Timeout:
    """Timeouts for a request that uploads ``size`` bytes: the reply's read timeout also allows for all of them to reach
    the server at UPLOAD_FLOOR_BYTES_PER_SECOND, up to UPLOAD_READ_TIMEOUT_MAX_SECONDS."""
    read = min(DEFAULT_TIMEOUT_SECONDS + size / UPLOAD_FLOOR_BYTES_PER_SECOND, UPLOAD_READ_TIMEOUT_MAX_SECONDS)
    return httpx.Timeout(DEFAULT_TIMEOUT_SECONDS, connect=CONNECT_TIMEOUT_SECONDS, read=read)


Surface = Literal["library", "cli", "ci", "action"]

_surface: Surface = "library"
"""What sends this process's requests, as their User-Agent names it."""


def set_surface(surface: Surface) -> None:
    """Name ``surface`` in the User-Agent of every client built from now on."""
    global _surface
    _surface = surface


def user_agent() -> str:
    """The User-Agent of every request: ``aristotlelib/<version> (<surface>; python/<x.y>; <sys.platform>)``."""
    python = f"{sys.version_info.major}.{sys.version_info.minor}"
    return f"aristotlelib/{get_version()} ({_surface}; python/{python}; {sys.platform})"


def new_http_client(
    timeout: float | httpx.Timeout | None = httpx.Timeout(DEFAULT_TIMEOUT_SECONDS, connect=CONNECT_TIMEOUT_SECONDS),
) -> httpx.AsyncClient:
    """Build a client that honors the proxy and CA settings of its environment, and sends user_agent()."""
    ca_bundle = _ca_bundle()
    _log_environment_once(ca_bundle)
    headers = {"User-Agent": user_agent()}
    try:
        return httpx.AsyncClient(timeout=timeout, trust_env=True, verify=_ssl_context(ca_bundle), headers=headers)
    except ssl.SSLError as e:
        # httpx loads the bundle at construction; a malformed file surfaces here as an opaque X509 error.
        raise AristotleAPIError(
            f"The certificate authority bundle at {ca_bundle} could not be loaded: {e}. "
            "It must be a PEM file containing one or more certificates."
        ) from e
    except (ValueError, httpx.InvalidURL) as e:
        # httpx refuses proxy settings it can't use as it builds the client; its message quotes them, credentials too.
        scheme = re.search(r"URL\('([a-z][a-z0-9+.-]*)://", str(e))
        if scheme:
            message = (
                f"A {scheme.group(1)}:// proxy from the environment (HTTPS_PROXY, HTTP_PROXY or ALL_PROXY) or the "
                "system's proxy settings can't be used. Use an http://, https://, socks5:// or socks5h:// proxy."
            )
        else:
            message = (
                "The proxy settings from the environment or the system can't be read: a proxy URL in HTTPS_PROXY, "
                "HTTP_PROXY or ALL_PROXY is malformed, or NO_PROXY has an entry the client can't read, such as an "
                "IPv6 range (fd00::/8) or a bracketed address ([::1])."
            )
        raise AristotleAPIError(message) from None


class AristotleAPIError(Exception):
    """Exception raised for API-related errors."""

    def __init__(self, message: str, status_code: int | None = None, error_code: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code
        """The ``error`` string of the server's JSON error response, such as ``authorization_pending``, or the ``code``
        of its ``detail`` object, such as ``OUT_OF_CREDITS``; None without one."""


class _IntermediaryError(AristotleAPIError):
    """A refusal without Aristotle's JSON error format; keep its network diagnosis through command error handling."""


class AristotleNotFoundError(AristotleAPIError):
    """No object the caller can see has this id; the server answers 403 for unknown and hidden ids alike."""

    def __init__(self, kind: str, object_id: str, status_code: int | None = None, message: str | None = None):
        if message is None:
            message = f"{kind.capitalize()} {object_id or repr(object_id)} not found or not accessible."
        super().__init__(message, status_code)
        self.kind = kind
        self.object_id = object_id

    def __reduce__(self) -> tuple[type["AristotleNotFoundError"], tuple[str, str, int | None, str]]:
        """Rebuilds from the constructor's arguments; args holds only the message."""
        return type(self), (self.kind, self.object_id, self.status_code, str(self))


class AristotleProjectLimitError(AristotleAPIError):
    """A 429 for too many runs in progress or queued, as opposed to too many requests per second."""

    def __init__(self, message: str):
        super().__init__(message, status_code=429)


class AristotleOutcomeUnknownError(AristotleAPIError):
    """A request that is not safe to repeat failed after Aristotle may have acted on it, so it was not sent again, or
    while an earlier copy sent under its idempotency key was still in progress."""


class AristotleNotProcessedError(AristotleAPIError):
    """A request that is not safe to repeat failed on every attempt before Aristotle acted on it: the connection never
    opened, Aristotle turned it away before any handler ran, or billing refused the run it would start. Or it was never
    sent, as a request before it failed. It did nothing, so it is safe to send again."""


@contextlib.contextmanager
def before_sending(advice: str) -> Generator[None, None, None]:
    """Re-raise an error from this context's requests, made before one that is not safe to repeat, as
    AristotleNotProcessedError followed by ``advice`` when no response came, or a server error or a 429 did; any other
    error as it is."""
    try:
        yield
    except AristotleAPIError as e:
        status = e.status_code
        # A lookup that matched nothing has no status either.
        if isinstance(e, AristotleNotFoundError) or (status is not None and status != 429 and status < 500):
            raise
        stop = "" if str(e).endswith((".", "!", "?")) else "."
        raise AristotleNotProcessedError(f"{e}{stop} {advice}", status) from e


# Transport failures that may pass on a later attempt.
_TRANSIENT_ERRORS = (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError)
# Transport failures raised before any of the request was sent: a LocalProtocolError is h11 refusing its request line or
# headers. Not WriteTimeout: the bytes it waited on may all arrive.
# TODO: resend a request after a WriteTimeout under its idempotency key, once the server says it takes keys.
_UNSENT_ERRORS = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.PoolTimeout,
    httpx.ProxyError,
    httpx.UnsupportedProtocol,
    httpx.LocalProtocolError,
)


def _scrubbed(error: httpx.HTTPError) -> httpx.HTTPError:
    """``error``, or for a LocalProtocolError, whose text quotes the header value h11 refused, a copy of its class with
    no text and no request."""
    return type(error)("") if isinstance(error, httpx.LocalProtocolError) else error


# A SOCKS proxy's refusals that a later try may pass, as httpcore words them after "Proxy Server could not connect: ".
_SOCKS_TRANSIENT_REPLIES = (
    "General SOCKS server failure",
    "Network unreachable",
    "Host unreachable",
    "Connection refused",
    "TTL expired",
)
# How _socks_reply_error words a reply socksio couldn't read; a later try may pass.
_SOCKS_MALFORMED = "The SOCKS proxy sent a reply it couldn't read, or closed the connection"


def _proxy_unavailable(error: httpx.HTTPError) -> bool:
    """Whether a proxy refused the tunnel in a way a later try may pass: a 5xx status, such as ``502 Bad Gateway``, a
    SOCKS refusal such as ``Host unreachable``, or a SOCKS reply that couldn't be read."""
    if not isinstance(error, httpx.ProxyError):
        return False
    message = str(error)
    status = message.split(" ", 1)[0]
    if status.isdigit():
        return int(status) >= 500
    refusal = message.removeprefix("Proxy Server could not connect: ").removesuffix(".")
    return refusal in _SOCKS_TRANSIENT_REPLIES or message.startswith(_SOCKS_MALFORMED)


def _socks_reply_error(error: socksio.ProtocolError) -> httpx.ProxyError:
    """The httpx.ProxyError for a SOCKS handshake that socksio couldn't read, which httpx doesn't map."""
    return httpx.ProxyError(f"{_SOCKS_MALFORMED}: {error}")


_CERTIFICATE_HINT = (
    ". This machine doesn't trust the server's certificate. Behind a proxy that inspects TLS, set ARISTOTLE_CA_BUNDLE "
    "to the PEM file of the proxy's certificate authority."
)


_UNTRUSTED_ISSUER = frozenset({2, 7, 18, 19, 20, 21, 30, 31})
"""OpenSSL's verify codes for a certificate no trusted authority vouches for, which a CA bundle can fix: its issuer's
certificate not found (2, or 20 among the trusted ones), self-signed (18) or with a self-signed one in its chain (19),
or its signature unverifiable (21). When only an authority with its issuer's name is trusted, as a renewed CA's
predecessor is, OpenSSL gives 7 for certificates without key identifiers, and 3.0.2 gives 30, or 31 for a renewal with
the same key."""

# httpcore checks an https:// proxy's own certificate with its default context, not the one the CA bundle sets.
_HTTPS_PROXY_CERTIFICATE_HINT = (
    ". The https:// proxy's own certificate didn't verify. It is checked against this machine's trusted authorities, "
    "which ARISTOTLE_CA_BUNDLE doesn't change."
)


def _certificate_hint(error: ssl.SSLCertVerificationError, proxy: bool) -> str:
    """What to do about a certificate that failed verification, the https:// proxy's own if ``proxy``.

    For a server certificate no trusted authority vouches for: name a CA bundle, or check the one named. OpenSSL's
    reason for any other failure, such as the certificate's dates or name, stands alone.
    """
    if proxy:
        return _HTTPS_PROXY_CERTIFICATE_HINT
    if error.verify_code not in _UNTRUSTED_ISSUER:
        return ""
    if setting := _ca_bundle_setting():
        variable, path = setting
        where, held = f"{path}, which {variable} names", "that file must hold the proxy's certificate authority"
    elif cert_dir := os.environ.get("SSL_CERT_DIR"):
        where = f"{cert_dir}, which SSL_CERT_DIR names"
        held = (
            "that folder must hold the proxy's certificate authority under its hashed name, as `openssl rehash` "
            "names it"
        )
    else:
        return _CERTIFICATE_HINT
    return (
        f". The server's certificate didn't verify against the certificate authorities in {where}. Behind a proxy that "
        f"inspects TLS, {held}."
    )


def _certificate_error(error: BaseException) -> ssl.SSLCertVerificationError | None:
    """A TLS handshake's failure to verify a certificate, anywhere in the error's chain of causes."""
    seen: set[int] = set()
    cause: BaseException | None = error
    while cause is not None and id(cause) not in seen:
        if isinstance(cause, ssl.SSLCertVerificationError):
            return cause
        seen.add(id(cause))
        cause = cause.__cause__ or cause.__context__
    return None


_T = TypeVar("_T")


class _ConnectionWatch:
    """httpcore ``trace`` extension that ends an attempt whose connection isn't open within CONNECTION_DEADLINE_SECONDS,
    and notes the host of the last TLS handshake it began."""

    def __init__(self) -> None:
        self._deadline: asyncio.TimerHandle | None = None
        self.expired = False
        self.tls_host: str | None = None
        """The host the last TLS handshake begun was with: the server, or an https:// proxy for its own certificate."""

    async def __call__(self, event: str, info: dict[str, Any]) -> None:
        if event.endswith(".start_tls.started"):
            self.tls_host = info.get("server_hostname")
        # The request starts to be written once its connection, any proxy's tunnel and TLS included, is open; an
        # HTTP proxy's CONNECT request is written before the tunnel is.
        if (
            event.endswith(".send_request_headers.started")
            and getattr(info.get("request"), "method", None) != b"CONNECT"
            and self._deadline is not None
        ):
            self._deadline.cancel()

    async def run(self, attempt: Awaitable[_T]) -> _T:
        """Await ``attempt`` in this task; raise httpx.ConnectTimeout if its connection isn't open in time.

        A request sent while a GeneratorExit is being handled, as by the cleanup of a task the garbage collector
        closes, has no deadline: the running task is someone else's.
        """
        if isinstance(sys.exc_info()[1], GeneratorExit):
            return await attempt
        task = asyncio.current_task()
        assert task is not None

        def expire() -> None:
            self.expired = True
            task.cancel()

        self._deadline = asyncio.get_running_loop().call_later(CONNECTION_DEADLINE_SECONDS, expire)
        try:
            return await attempt
        except asyncio.CancelledError:
            # Another cancellation, such as Ctrl-C's, still propagates.
            if not self.expired or (sys.version_info >= (3, 11) and task.uncancel() > 0):
                raise
            raise httpx.ConnectTimeout(f"No connection within {CONNECTION_DEADLINE_SECONDS} s") from None
        finally:
            self._deadline.cancel()
            self._deadline = None


@contextlib.asynccontextmanager
async def _streamed_get(
    client: httpx.AsyncClient, url: str, headers: dict[str, str]
) -> AsyncGenerator[httpx.Response, None]:
    """``client.stream("GET", url)``, whose connection must open within CONNECTION_DEADLINE_SECONDS."""
    watch = _ConnectionWatch()
    request = client.build_request("GET", url, headers=headers, extensions={"trace": watch})
    response = await watch.run(client.send(request, stream=True))
    try:
        yield response
    finally:
        await response.aclose()


_next_request_at = 0.0
"""The monotonic time at which this process may start its next request."""
# Re-entrant: the garbage collector can run a task's cleanup, which may send a request, while this thread holds it.
_request_slots = threading.RLock()
_queues: weakref.WeakValueDictionary[int, asyncio.Lock] = weakref.WeakValueDictionary()
"""Each event loop's queue for request slots, by the loop's id. Only the requests waiting in a queue hold it, and they
hold their loop, so it goes when they do, and a loop's id can't be reused while its queue lasts."""


async def _wait_for_request_slot() -> None:
    """Wait until REQUEST_INTERVAL_SECONDS after this process last started a request; the next one counts from now.

    Requests on one event loop take their slots in the order they asked: one waits for the slot at a time. A request
    sent while a GeneratorExit is being handled, as by the cleanup of a task the garbage collector closes, neither
    queues nor waits: it couldn't wait anyway, and left in the queue it would hold up the requests behind it.
    """
    global _next_request_at
    if isinstance(sys.exc_info()[1], GeneratorExit):
        return
    loop = asyncio.get_running_loop()
    with _request_slots:
        if (turn := _queues.get(id(loop))) is None:
            turn = _queues[id(loop)] = asyncio.Lock()
    async with turn:
        while True:
            with _request_slots:
                now = time.monotonic()
                if now >= _next_request_at:
                    _next_request_at = now + REQUEST_INTERVAL_SECONDS
                    return
                wait = _next_request_at - now
            await asyncio.sleep(wait)


_quiet: contextvars.ContextVar[Callable[[], bool] | None] = contextvars.ContextVar("quiet", default=None)


@contextlib.contextmanager
def quietly(when: Callable[[], bool]) -> Generator[None, None, None]:
    """Log this context's retries at debug level whenever ``when()`` is true as they are logged, as a wait does for its
    refreshes while it rides out an outage it has reported."""
    token = _quiet.set(when)
    try:
        yield
    finally:
        _quiet.reset(token)


def _retry_after(response: httpx.Response) -> float | None:
    """The seconds the response's Retry-After asks a client to wait, in seconds or as an HTTP date; None without one.

    A value that is neither counts as none, so a malformed header can't turn an error response into a crash.
    """
    value = response.headers.get("Retry-After", "").strip()
    if value.isascii() and value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())
    except Exception:  # Python 3.10.0's parser raises UnboundLocalError on some malformed times.
        return None


def _wait_text(seconds: float) -> str:
    """``seconds`` rounded up, as "1 second" or "45 seconds", and from two minutes on as whole minutes."""
    whole = math.ceil(seconds)
    if whole < 120:
        return f"{whole} second{'' if whole == 1 else 's'}"
    return f"{math.ceil(whole / 60)} minutes"


def _json_detail(response: httpx.Response) -> str | None:
    """The response's JSON ``detail`` string, or the ``message`` of a ``detail`` object, or None when the body has
    neither."""
    try:
        detail = response.json().get("detail")
    except Exception:
        return None
    if isinstance(detail, dict):
        detail = detail.get("message")
    if not isinstance(detail, str) or not detail.strip():
        return None
    return detail.strip()


class _ResponseText(HTMLParser):
    """Extract an error page's title or visible text without printing HTML, scripts, or styles."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title: list[str] = []
        self.text: list[str] = []
        self._in_title = False
        self._hidden: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "title":
            self._in_title = True
        if tag in ("head", "script", "style"):
            self._hidden.add(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        self._hidden.discard(tag)

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title.append(data)
        elif not self._hidden:
            self.text.append(data)


def _proxy_bypassed(url: httpx.URL, no_proxy: str) -> bool:
    """Match NO_PROXY as httpx does, including URL entries and a wildcard anywhere in the list."""
    entries = [entry.strip() for entry in no_proxy.split(",")]
    # httpx ignores every other entry when the global wildcard is present, even entries it cannot parse.
    if "*" in entries:
        return True
    for entry in entries:
        if not entry:
            continue
        domain = False
        if "://" in entry:
            pattern = httpx.URL(entry)
        else:
            try:
                address = ip_address(entry.split("/", 1)[0])
            except ValueError:
                domain = entry.lower() != "localhost"
                pattern = httpx.URL(f"all://{entry}")
            else:
                pattern = httpx.URL(f"all://[{entry}]" if address.version == 6 else f"all://{entry}")
        if pattern.scheme not in ("all", url.scheme) or (pattern.port is not None and pattern.port != url.port):
            continue
        host = pattern.host
        if host in ("", "*"):
            return True
        if host.startswith("*"):
            domain = True
            host = host[1:]
        if url.host == host or (domain and url.host.endswith("." + host.lstrip("."))):
            return True
    return False


def _configured_proxy(url: httpx.URL) -> httpx.URL | None:
    """The proxy for this destination, excluding hosts bypassed by NO_PROXY."""
    proxies = getproxies()
    configured = proxies.get(url.scheme) or proxies.get("all")
    if not configured or _proxy_bypassed(url, proxies.get("no", "")):
        return None
    return httpx.URL(configured if "://" in configured else f"http://{configured}")


def _refusal_message(status_line: str, url: httpx.URL, *, known_proxy: bool = False) -> str:
    """The next step for a proxy refusal, or a refusal whose source we cannot identify."""
    proxy = _configured_proxy(url)
    if proxy is not None or known_proxy:
        host = printable(proxy.host) if proxy is not None else "unknown proxy"
        return (
            f"Error: request blocked by a proxy ({printable(status_line)}, via {host})\n\n"
            f"Hint: ask your network admin to allow {printable(url.host)}."
        )
    return (
        f"Error: request refused ({printable(status_line)})\n\n"
        "Hint: this may come from a proxy or firewall on your network. "
        "Run with --verbose to see the response headers."
    )


def _log_refusal_response(error: httpx.HTTPStatusError, redact: Callable[[str], str] | None = None) -> None:
    """Verbose diagnostics without credentials, terminal controls, or an entire error page; ``redact``, the request's,
    rewrites each line first."""
    if not http_logger.isEnabledFor(logging.DEBUG):
        return
    response = error.response
    secrets = [error.request.headers.get("X-API-Key", "")]
    for headers in (error.request.headers, response.headers):
        for name in ("Authorization", "Proxy-Authorization"):
            for authorization in headers.get_list(name):
                secrets.extend([authorization, authorization.partition(" ")[2]])
        for name in ("Cookie", "Set-Cookie"):
            for raw in headers.get_list(name):
                secrets.append(raw)
                cookies: SimpleCookie = SimpleCookie()
                try:
                    # New Set-Cookie attributes can make older Pythons silently discard the entire cookie.
                    # Parse its name/value independently; request Cookie headers still contain multiple cookies.
                    cookies.load(raw.partition(";")[0] if name == "Set-Cookie" else raw)
                except CookieError:
                    continue
                for cookie in cookies.values():
                    secrets.extend([cookie.value, cookie.coded_value])
    for configured in getproxies().values():
        try:
            proxy = httpx.URL(configured if "://" in configured else f"http://{configured}")
        except httpx.InvalidURL:
            continue
        secrets.extend([configured, proxy.username, proxy.password])
        if proxy.username or proxy.password:
            # httpx sends HTTP proxy credentials as UTF-8 username:password encoded with Base64.
            secrets.append(b64encode(f"{proxy.username}:{proxy.password}".encode("utf-8")).decode("ascii"))

    def safe(text: str, limit: int) -> str:
        if redact is not None:
            text = redact(text)
        for secret in sorted(set(secrets), key=len, reverse=True):
            if secret:
                text = text.replace(secret, "[redacted]")
        text = re.sub(r"\barstl_[A-Za-z0-9_-]+", "[redacted]", text)
        text = printable(" ".join(text.split()))
        return text[:limit] + ("…" if len(text) > limit else "")

    http_logger.debug("Response headers (%s %s):", response.status_code, safe(response.reason_phrase, 80))
    for name, value in response.headers.multi_items():
        if re.search(r"authorization|cookie|api[-_]?key|token|secret", name, re.IGNORECASE):
            value = "[redacted]"
        http_logger.debug("%s: %s", safe(name, 80), safe(value, 256))
    excerpt = response.text
    if "html" in response.headers.get("content-type", "").lower() or excerpt.lstrip().startswith("<"):
        page = _ResponseText()
        page.feed(excerpt)
        excerpt = " ".join(page.title or page.text)
    if excerpt:
        http_logger.debug("Response excerpt: %s", safe(excerpt, 160))


def _status_error(error: httpx.HTTPStatusError, redact: Callable[[str], str] | None = None) -> AristotleAPIError:
    """The AristotleAPIError for an error response, with the server's detail and error code; a 429 says which limit was
    hit, a 402 where to buy credits, a detail object's message stands alone, and a 403's verbose report passes through
    ``redact``."""
    if error.response.status_code == 403:
        _log_refusal_response(error, redact)
        content_type = error.response.headers.get("content-type", "").partition(";")[0].strip().lower()
        # The API's HTTPException responses have JSON with a detail string, or Auth0's code/message detail object.
        # A proxy can imitate that format; certificate trust alone cannot establish who generated the response.
        detail = None
        if content_type == "application/json":
            try:
                body = error.response.json()
                if isinstance(body, dict):
                    detail = body.get("detail")
                    if isinstance(detail, dict):
                        detail = detail.get("message")
            except ValueError:
                pass
        if not isinstance(detail, str) or not detail.strip():
            status_line = f"{error.response.status_code} {error.response.reason_phrase}".strip()
            return _IntermediaryError(_refusal_message(status_line, error.request.url), status_code=403)
        return AristotleAPIError(_ACCOUNT_ACCESS_MESSAGE, status_code=403)
    # Extract error message from response body if available
    error_message = None
    error_code = None
    # The message of a detail object, as billing's refusals carry with their code, worded for the user.
    worded = None
    try:
        error_data = error.response.json()
        detail = error_data.get("detail")
        if isinstance(detail, dict):
            message, error_code = detail.get("message"), detail.get("code")
            worded = message.strip() if isinstance(message, str) and message.strip() else None
            error_message = worded
        else:
            error_message = detail or error_data.get("message")
            error_code = error_data.get("error")
    except Exception:
        # If JSON parsing fails, try to get text
        try:
            error_message = error.response.text
        except Exception:
            pass

    # The status line's reason, not str(error), which holds the URL with its query.
    if not error_message:
        error_message = error.response.reason_phrase or "no detail"

    if error.response.status_code == 429:
        detail = _json_detail(error.response)
        # Only a request that starts a run can meet the limit on runs, and only a POST starts one.
        if error.request.method == "POST" and detail is not None and detail.startswith(PROJECT_LIMIT_DETAILS):
            legacy = detail.startswith(LEGACY_PROJECT_LIMIT_DETAIL)
            return AristotleProjectLimitError(PROJECT_LIMIT_FALLBACK if legacy else detail)
        wait = _retry_after(error.response)
        advice = (
            f"Aristotle asked to wait {_wait_text(wait)} before trying again."
            if wait and math.isfinite(wait)
            else "Wait a few seconds and try again."
        )
        return AristotleAPIError(f"Too many requests to Aristotle at once. {advice}", status_code=429)
    status = error.response.status_code
    code = error_code if isinstance(error_code, str) else None
    if status == 402:
        refusal = worded or "Your organization needs credits to start a run."
        stop = "" if refusal.endswith((".", "!", "?")) else "."
        return AristotleAPIError(
            f"{refusal}{stop} An organization admin can buy credits at {billing_url()}.",
            status_code=402,
            error_code=code,
        )
    if worded is not None:
        return AristotleAPIError(worded, status_code=status, error_code=code)
    return AristotleAPIError(
        f"API request failed with status {status}: {error_message}", status_code=status, error_code=code
    )


class _ForeignAnswer(httpx.HTTPStatusError):
    """A 2xx answer typed as text, or untyped, that isn't JSON; its message says what it holds, as _foreign_body
    does."""


def _foreign_body(response: httpx.Response) -> str | None:
    """What a 2xx answer typed as text, or untyped, holds when it isn't JSON, such as ``text/html`` or ``an empty
    body``; None for any other answer, and for a 204 or 205, which has no content: the API answers with JSON, or with a
    file of another type."""
    if response.status_code in (204, 205):
        return None
    content_type = response.headers.get("content-type", "").partition(";")[0].strip()
    if content_type and not content_type.lower().startswith("text/"):
        return None
    try:
        response.json()
    except ValueError:
        if not response.content:
            return "an empty body"
        return printable(content_type) if content_type else "an untyped body"
    return None


def _foreign_answer_message(response: httpx.Response, body: str) -> str:
    """What a 2xx answer that isn't Aristotle's was: its status and ``body``, which says what it holds, where it came
    from, and what may have sent it."""
    status_line = printable(f"{response.status_code} {response.reason_phrase}".strip())
    origin = get_web_url()
    proxy = _configured_proxy(response.request.url)
    via = "" if proxy is None else f" through the proxy {printable(proxy.host)}"
    senders = (
        "a captive portal or a proxy" if origin == WEB_URL else "a captive portal, a proxy or a wrong ARISTOTLE_WEB_URL"
    )
    return (
        f"the answer from {origin}{via} was {status_line} with {body}, not Aristotle's: {senders} may have answered "
        "instead."
    )


class _Failure:
    """One failed attempt at a request: whether it may be retried, and what to report."""

    def __init__(
        self,
        cause: httpx.HTTPError,
        request: str,
        keyed: bool = False,
        proxy_tls: bool = False,
        redact: Callable[[str], str] | None = None,
    ):
        self.cause = cause = _scrubbed(cause)
        self.request = request
        self.retry_after: float | None = None
        self.key_busy = False
        self.rate_limited = False
        # Whether billing refused the run the request would start, before Aristotle acted on it; no retry gets past it.
        self.refused = False
        self.certificate_error: ssl.SSLCertVerificationError | None = None
        # Whether the TLS handshake that failed was with an https:// proxy, for its own certificate, not the server.
        self.proxy_tls = proxy_tls
        if isinstance(cause, _ForeignAnswer):
            self.status_error = AristotleAPIError(_foreign_answer_message(cause.response, str(cause)))
            # Aristotle's answer was lost, as the request may have reached it.
            self.unsent = False
            self.transient = self.maybe_processed = True
            self.summary = f"{cause.response.status_code} on {request}: {cause}, not Aristotle's answer"
        elif isinstance(cause, httpx.HTTPStatusError):
            status = cause.response.status_code
            self.status_error = _status_error(cause, redact)
            code = self.status_error.error_code
            self.retry_after = _retry_after(cause.response)
            detail = _json_detail(cause.response)
            # The server refuses a keyed request with KEY_BUSY_DETAIL's 409 while an earlier copy under its key is in
            # progress, any request with the per-second limit's 429 while authenticating it, before any handler runs,
            # and a run with BILLING_UNAVAILABLE's 503 before creating anything.
            self.key_busy = status == 409 and keyed and (detail or "").startswith(KEY_BUSY_DETAIL)
            self.rate_limited = status == 429 and not isinstance(self.status_error, AristotleProjectLimitError)
            billing_unavailable = status == 503 and code == BILLING_UNAVAILABLE
            self.refused = status == 402 or (status == 409 and code == SIGNUP_GRANT_PENDING)
            self.unsent = self.key_busy or self.rate_limited or billing_unavailable
            self.transient = self.unsent or status >= 500
            self.maybe_processed = status >= 500 and not billing_unavailable
            self.summary = f"{status} on {request}" + (f": {detail}" if detail else "")
        else:
            self.unsent = isinstance(cause, _UNSENT_ERRORS)
            self.certificate_error = _certificate_error(cause)
            self.transient = (
                isinstance(cause, _TRANSIENT_ERRORS) or _proxy_unavailable(cause)
            ) and self.certificate_error is None
            self.maybe_processed = not self.unsent
            self.summary = f"{type(cause).__name__} on {request}" + (f": {cause}" if str(cause) else "")

    def error(self, attempts: int, seconds: float, idempotent: bool) -> AristotleAPIError:
        """The error to raise: the failure, the request, and the attempts made if more than one."""
        after = f" after {attempts} attempts over {seconds:.0f} s" if attempts > 1 else ""
        if isinstance(self.cause, httpx.HTTPStatusError):
            if self.refused and not idempotent:
                error = self.status_error
                return AristotleNotProcessedError(str(error), error.status_code, error.error_code)
            if not self.transient:
                return self.status_error
            if self.key_busy:
                return AristotleOutcomeUnknownError(
                    f"{self.request}{after}: an earlier copy sent under its Idempotency-Key was still in progress, and "
                    "may yet act.",
                    self.status_error.status_code,
                )
            message = f"{self.request}{after}: {self.status_error}"
            status_code, error_code = self.status_error.status_code, self.status_error.error_code
        else:
            if isinstance(self.cause, httpx.ProxyError) and re.fullmatch(r"403(?: [^\r\n]+)?", str(self.cause)):
                message = _refusal_message(str(self.cause), self.cause.request.url, known_proxy=True)
                # A refused CONNECT sent none of a mutating request; keep that outcome classification.
                return _IntermediaryError(message) if idempotent else AristotleNotProcessedError(message)
            message = f"{type(self.cause).__name__} on {self.request}{after}"
            if str(self.cause):
                message += f": {self.cause}"
            if self.certificate_error is not None:
                message += _certificate_hint(self.certificate_error, self.proxy_tls)
            status_code, error_code = None, None
        if not idempotent:
            message = message if message.endswith(".") else message + "."
            if self.maybe_processed:
                return AristotleOutcomeUnknownError(
                    f"{message} Aristotle may have acted on it, so it was not sent again.", status_code, error_code
                )
            # Retried only while unsent, and never after a refusal for its key: no attempt reached a handler.
            return AristotleNotProcessedError(f"{message} Aristotle didn't act on it.", status_code, error_code)
        return AristotleAPIError(message, status_code, error_code)


def _redacted(error: AristotleAPIError, redact: Callable[[str], str]) -> AristotleAPIError:
    """A copy of ``error``, of its class and with its status and error code, whose message has passed through
    ``redact``."""
    redacted = copy.copy(error)
    redacted.args = (redact(str(error)),)
    return redacted


def _redacted_cause(cause: httpx.HTTPError, redact: Callable[[str], str]) -> httpx.HTTPError | None:
    """The cause a redacted error keeps: a copy of a transport failure, of its class, with its message through
    ``redact`` and no request; None for a status error, whose response holds a body ``redact`` can't reach."""
    return None if isinstance(cause, httpx.HTTPStatusError) else type(cause)(redact(str(cause)))


class AristotleRequestClient:
    """Async HTTP client for the Aristotle API."""

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        # Replaced after an attempt's connection deadline: httpcore keeps a cancelled SOCKS handshake in the pool.
        self._retired: list[httpx.AsyncClient] = []
        self.rate_limit_refusals = 0
        """How many attempts sent through this client the per-second rate limit has refused."""

    async def __aenter__(self):
        """Async context manager entry."""
        self._client = new_http_client()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        await self.close()

    async def get(self, endpoint: str, params: dict[str, Any] | None = None) -> httpx.Response:
        """Make a GET request."""
        return await self._make_request("GET", endpoint, params=params)

    async def post(
        self,
        endpoint: str,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        files: list[tuple[str, tuple[Any, ...]]] | None = None,
        json: dict[str, Any] | None = None,
        idempotent: bool = False,
        idempotency_key: str | None = None,
        timeout: httpx.Timeout | None = None,
        authenticated: bool = True,
        redact: Callable[[str], str] | None = None,
    ) -> httpx.Response:
        """Make a POST request; ``idempotent`` says that sending it twice has the effect of sending it once,
        ``idempotency_key``, sent as IDEMPOTENCY_KEY_HEADER on every attempt, lets a server that takes it say so,
        ``timeout`` replaces the client's timeouts, as upload_timeout() does for an upload, ``authenticated=False``
        sends it without an API key, and ``redact`` rewrites what the request logs and raises, as _make_request says."""
        return await self._make_request(
            "POST",
            endpoint,
            params=params,
            data=data,
            files=files,
            json=json,
            idempotent=idempotent,
            idempotency_key=idempotency_key,
            timeout=timeout,
            authenticated=authenticated,
            redact=redact,
        )

    async def put(self, endpoint: str, data: dict[str, Any] | None = None) -> httpx.Response:
        """Make a PUT request."""
        return await self._make_request("PUT", endpoint, data=data)

    async def delete(self, endpoint: str) -> httpx.Response:
        """Make a DELETE request."""
        return await self._make_request("DELETE", endpoint)

    async def _make_request(
        self,
        method: str,
        endpoint: str,
        data: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        files: list[tuple[str, tuple[Any, ...]]] | None = None,
        json: dict[str, Any] | None = None,
        idempotent: bool = True,
        idempotency_key: str | None = None,
        timeout: httpx.Timeout | None = None,
        authenticated: bool = True,
        redact: Callable[[str], str] | None = None,
    ) -> httpx.Response:
        """Make an HTTP request to the Aristotle API, with the API key unless not ``authenticated``, retrying transient
        failures with jittered backoff. ``redact``, for a request whose body holds a secret a server may quote back,
        rewrites the text of each retry it logs, of a refusal's verbose report and of the error it raises, whose cause
        is then _redacted_cause's.

        Any request is retried after a failure to connect, a proxy's refusal that a later try may pass, a refusal by the
        per-second rate limit, billing's 503 for credits it couldn't read or, with ``idempotency_key``, a 409 while its
        first copy is in progress, which leave it unprocessed, and an idempotent one also after a 5xx, a timeout, a
        dropped connection or a 2xx answer that _foreign_body says isn't Aristotle's; a certificate this machine doesn't
        trust is not retried. A request that is not idempotent raises AristotleOutcomeUnknownError after a failure that
        may have followed its processing, and AristotleNotProcessedError when no attempt was processed, as when billing
        refused the run it would start, for credits or while the organization sets up. A retry waits at least as long as
        the response's Retry-After asks, every attempt waits for its request slot, and an attempt whose connection isn't
        open within CONNECTION_DEADLINE_SECONDS fails as a ConnectTimeout.
        """
        url = f"{get_base_url()}/{endpoint.lstrip('/')}"
        # As httpcore names it to the server's TLS handshake.
        host = httpx.URL(url).raw_host.decode("ascii")
        request = f"{method} /{endpoint.lstrip('/')}"
        headers = {"X-API-Key": get_api_key()} if authenticated else {}
        if idempotency_key is not None:
            headers[IDEMPOTENCY_KEY_HEADER] = idempotency_key
        # None params are dropped: the server rejects an empty pagination_key.
        params = None if params is None else {k: v for k, v in params.items() if v is not None}
        body: dict[str, Any] = {}
        if files is not None:
            # httpx uses multipart when both data and files are provided
            body = {"data": data, "files": files}
        elif data is not None:
            body = {"data": data}
        elif json is not None:
            body = {"json": json}

        if not self._client:
            self._client = new_http_client()

        # The deadline counts from the first attempt, not from the wait for its slot.
        await _wait_for_request_slot()
        started = time.monotonic()
        attempt = 1
        # The last refusal saying an earlier copy under the key was in progress; it may yet act.
        busy: _Failure | None = None
        while True:
            watch = _ConnectionWatch()
            try:
                response = await watch.run(
                    self._client.request(
                        method=method,
                        url=url,
                        params=params,
                        headers=headers,
                        extensions={"trace": watch},
                        timeout=httpx.USE_CLIENT_DEFAULT if timeout is None else timeout,
                        **body,
                    )
                )
                response.raise_for_status()
                if (foreign := _foreign_body(response)) is not None:
                    raise _ForeignAnswer(foreign, request=response.request, response=response)
                return response
            except httpx.HTTPError as e:
                proxy_tls = watch.tls_host is not None and watch.tls_host != host
                failure = _Failure(e, request, keyed=idempotency_key is not None, proxy_tls=proxy_tls, redact=redact)
                if watch.expired:
                    self._retired.append(self._client)
                    self._client = new_http_client()
            except socksio.ProtocolError as e:
                failure = _Failure(_socks_reply_error(e), request, keyed=idempotency_key is not None)
            self.rate_limit_refusals += failure.rate_limited
            if failure.key_busy:
                busy = failure
            delay = RETRY_BACKOFF_SECONDS * 2 ** (attempt - 1) * random.uniform(1, 2)
            if failure.retry_after is not None:
                delay = max(delay, failure.retry_after + random.uniform(0, 1))
            retry = failure.transient and (idempotent or failure.unsent)
            if not retry or attempt == RETRY_ATTEMPTS or time.monotonic() + delay - started > RETRY_DEADLINE_SECONDS:
                if busy is not None and failure.unsent:
                    failure = busy
                error = failure.error(attempt, time.monotonic() - started, idempotent)
                if redact is None:
                    raise error from failure.cause
                raise _redacted(error, redact) from _redacted_cause(failure.cause, redact)
            quiet = _quiet.get()
            retrying = f"Retrying in {delay:.1f} s after {failure.summary}"
            (logger.debug if quiet is not None and quiet() else logger.warning)(
                retrying if redact is None else redact(retrying)
            )
            await asyncio.sleep(delay)
            await _wait_for_request_slot()
            attempt += 1

    async def stream_sse(
        self,
        endpoint: str,
        max_retries: int = 5,
        reconnect_if: Callable[[], Awaitable[bool]] | None = None,
        live: Callable[[], bool] | None = None,
        on_outage: Callable[[Exception | None], None] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Stream Server-Sent Events; a dropped or silent connection reopens with Last-Event-ID, the newest id seen, and
        one whose answer isn't typed text/event-stream counts as dropped.

        A clean end also reconnects while ``reconnect_if()`` is true. Gives up after ``max_retries`` connections in a
        row that received no line, or at once after a refusal whose Retry-After asks for more than
        STREAM_RECONNECT_MAX_SECONDS: a transport error or retryable status is raised, a clean end returns.
        ``live()`` says, without a request, whether the run the stream follows was live when last seen. While it is,
        connections in a row that received no line are retried instead for STREAM_OUTAGE_SECONDS from the first of
        them that failed, however many that takes; once it isn't, giving up returns rather than raises.
        ``on_outage(error)`` is called as the stream begins to ride out such an outage, and ``on_outage(None)`` when a
        line arrives after it. A data line already yielded is not yielded again. Each connection waits for its request
        slot, and a reconnect after a refusal waits at least as long as its Retry-After asks.

        Yields:
            Parsed JSON data for each SSE event.
        """
        url = f"{get_base_url()}/{endpoint.lstrip('/')}"
        data_prefix = "data: "
        last_event_id: str | None = None
        yielded: set[str] = set()
        failures = 0
        # When the first failed connection began of those in a row that received no line; None until one fails.
        outage_since: float | None = None
        # Whether on_outage was told of the outage under way.
        reported = False

        while True:
            headers = {
                "X-API-Key": get_api_key(),
                "Accept": "text/event-stream",
            }
            if last_event_id is not None:
                headers["Last-Event-ID"] = last_event_id
            received = False
            # An event's id counts once the blank line ending the event arrives, as the SSE spec has it.
            event_id: str | None = None
            error: Exception | None = None
            retry_after: float | None = None
            await _wait_for_request_slot()
            started = time.monotonic()
            try:
                timeout = httpx.Timeout(
                    DEFAULT_TIMEOUT_SECONDS, connect=CONNECT_TIMEOUT_SECONDS, read=STREAM_READ_TIMEOUT_SECONDS
                )
                async with new_http_client(timeout=timeout) as client:
                    async with _streamed_get(client, url, headers) as response:
                        try:
                            response.raise_for_status()
                        except httpx.HTTPStatusError as e:
                            # Read the body for the server's detail.
                            await response.aread()
                            retry_after = _retry_after(response)
                            raise _status_error(e) from e
                        media_type = response.headers.get("content-type", "").partition(";")[0].strip()
                        if media_type.lower() != "text/event-stream":
                            # Any other answer fails the connection, as the SSE spec has it.
                            raise httpx.RemoteProtocolError(
                                _foreign_answer_message(response, printable(media_type) or "no content type"),
                                request=response.request,
                            )
                        async for line in response.aiter_lines():
                            if not received:
                                outage_since = None
                                if reported and on_outage is not None:
                                    reported = False
                                    on_outage(None)
                            received = True
                            if not line:
                                if event_id is not None:
                                    last_event_id = _later_event_id(last_event_id, event_id)
                                    event_id = None
                            elif line.startswith("id:"):
                                event_id = line[3:].removeprefix(" ")
                            elif line.startswith(data_prefix):
                                data = line.removeprefix(data_prefix)
                                if data in yielded:
                                    continue
                                try:
                                    event = json.loads(data)
                                except json.JSONDecodeError:
                                    logger.warning(f"Failed to parse SSE data: {line}")
                                    continue
                                yielded.add(data)
                                yield event
            except httpx.TransportError as e:
                if isinstance(e, httpx.ProxyError) and re.fullmatch(r"403(?: [^\r\n]+)?", str(e)):
                    error = _IntermediaryError(_refusal_message(str(e), e.request.url, known_proxy=True))
                else:
                    error = _scrubbed(e)
            except socksio.ProtocolError as e:
                error = _socks_reply_error(e)
            except AristotleAPIError as e:
                if e.status_code is None or (e.status_code != 429 and e.status_code < 500):
                    raise
                error = e
            if error is None and (reconnect_if is None or not await reconnect_if()):
                return

            failures = 0 if received else failures + 1
            began = not received and error is not None and outage_since is None
            if began:
                outage_since = started
            ended = live is not None and not live()
            # A wait rides out an outage while the run it follows goes on.
            riding_out = (
                live is not None
                and not ended
                and outage_since is not None
                and time.monotonic() - outage_since < STREAM_OUTAGE_SECONDS
            )
            if (failures >= max_retries and not riding_out) or (
                retry_after is not None and retry_after > STREAM_RECONNECT_MAX_SECONDS
            ):
                # A run that has ended leaves nothing to follow.
                if error is None or ended:
                    return
                raise error
            if began and riding_out and on_outage is not None:
                on_outage(error)
                reported = True
            delay = min(STREAM_RECONNECT_SECONDS * 2**failures, STREAM_RECONNECT_MAX_SECONDS)
            if retry_after is not None:
                delay = max(delay, retry_after + random.uniform(0, 1))
            await asyncio.sleep(delay)

    async def close(self):
        """Close the async client, and any it replaced."""
        clients, self._retired = self._retired, []
        if self._client:
            clients.append(self._client)
            self._client = None
        for client in clients:
            await client.aclose()
