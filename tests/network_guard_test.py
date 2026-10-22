"""The guard in conftest.py that keeps the tests on this machine."""

import asyncio
import socket
from collections import Counter

import pytest

from tests.conftest import _on_this_machine


@pytest.mark.parametrize(
    ("host", "local"),
    [
        (None, True),
        ("", True),
        ("localhost", True),
        ("127.0.0.1", True),
        ("::1", True),
        ("::ffff:127.0.0.1", True),
        ("0.0.0.0", True),
        (b"127.0.0.1", True),
        # A resolver that doesn't special-case *.localhost asks DNS for it.
        ("box.localhost", False),
        ("aristotle.harmonic.fun", False),
        ("192.0.2.1", False),
        ("2001:db8::1", False),
        ("::ffff:192.0.2.1", False),
    ],
)
def test_only_loopback_and_unspecified_hosts_are_on_this_machine(host: object, local: bool):
    assert _on_this_machine(host) is local


def test_a_name_beyond_this_machine_is_not_resolved(no_outside_network: Counter[str]):
    with pytest.raises(socket.gaierror):
        socket.getaddrinfo("aristotle.harmonic.fun", 443)

    assert no_outside_network == {"resolve aristotle.harmonic.fun": 1}
    no_outside_network.clear()


def test_a_connection_beyond_this_machine_is_refused(no_outside_network: Counter[str]):
    with socket.socket() as sock, pytest.raises(ConnectionRefusedError):
        sock.connect(("192.0.2.1", 443))

    assert no_outside_network == {"connect to 192.0.2.1:443": 1}
    no_outside_network.clear()


async def test_an_event_loop_connection_beyond_this_machine_is_refused(no_outside_network: Counter[str]):
    with pytest.raises(OSError):
        await asyncio.open_connection("192.0.2.1", 443)

    assert no_outside_network == {"connect to 192.0.2.1:443": 1}
    no_outside_network.clear()


async def test_loopback_and_socket_pairs_stay_open(no_outside_network: Counter[str]):
    async def echo(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(await reader.read(5))
        await writer.drain()
        writer.close()

    assert socket.getaddrinfo("localhost", 80)
    server = await asyncio.start_server(echo, "127.0.0.1", 0)
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", server.sockets[0].getsockname()[1])
        writer.write(b"hello")
        assert await reader.read(5) == b"hello"
        writer.close()
    finally:
        server.close()
        await server.wait_closed()
    left, right = socket.socketpair()
    with left, right:
        left.sendall(b"x")
        assert right.recv(1) == b"x"

    assert not no_outside_network
