"""The local port ``dokimi prometheus`` listens on, relaying each connection on its own.

The remote end is a plain local server here: what the relay is given to open a channel
with is all it knows, so it behaves the same against a pod's port-forward.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Callable, Iterator

import pytest

from dokimi.k8s.port_forward import Channel, PortRelay


def server(handle: Callable[[socket.socket], None]) -> tuple[socket.socket, Callable[[], Channel]]:
    """A local server answering each connection with *handle*, and how to open a channel."""
    listener = socket.create_server(("127.0.0.1", 0))

    def accept() -> None:
        while True:
            try:
                connection, _ = listener.accept()
            except OSError:
                return
            threading.Thread(target=handle, args=(connection,), daemon=True).start()

    threading.Thread(target=accept, daemon=True).start()
    address = listener.getsockname()
    return listener, lambda: socket.create_connection(address)


def answers(reply: bytes, *, after: bytes = b"\n") -> Callable[[socket.socket], None]:
    """A handler that reads up to *after*, then sends *reply* and closes."""

    def handle(connection: socket.socket) -> None:
        received = b""
        while after not in received:
            chunk = connection.recv(1024)
            if not chunk:
                break
            received += chunk
        connection.sendall(reply + b" to " + received.strip())
        connection.close()

    return handle


@pytest.fixture
def running() -> Iterator[Callable[[PortRelay], PortRelay]]:
    relays: list[PortRelay] = []

    def start(relay: PortRelay) -> PortRelay:
        relays.append(relay)
        threading.Thread(target=relay.serve, daemon=True).start()
        return relay

    yield start
    for relay in relays:
        relay.close()


def ask(address: tuple[str, int], request: bytes) -> bytes:
    """Send *request*, and return everything received until the connection closed.

    A connection closed with the request still unread may arrive as a reset instead of an
    end of data, depending on the platform: either is a connection closed unanswered.
    """
    with socket.create_connection(address, timeout=5) as connection:
        connection.sendall(request)
        received = b""
        try:
            while chunk := connection.recv(1024):
                received += chunk
        except ConnectionResetError, ConnectionAbortedError:
            pass
        return received


def test_a_connection_is_relayed_both_ways(
    running: Callable[[PortRelay], PortRelay],
) -> None:
    listener, open_channel = server(answers(b"pong"))
    with listener:
        relay = running(PortRelay(open_channel))

        assert ask(relay.address, b"ping\n") == b"pong to ping"


def test_a_free_port_is_chosen_unless_one_is_given(
    running: Callable[[PortRelay], PortRelay],
) -> None:
    listener, open_channel = server(answers(b"pong"))
    with listener:
        free = running(PortRelay(open_channel))
        host, port = free.address

        assert host == "127.0.0.1"
        assert port > 0


def test_each_connection_gets_a_channel_of_its_own(
    running: Callable[[PortRelay], PortRelay],
) -> None:
    # A browser opens several connections; their bytes must never meet in one channel.
    listener, open_channel = server(answers(b"pong"))
    opened: list[int] = []

    def counting() -> Channel:
        opened.append(1)
        return open_channel()

    with listener:
        relay = running(PortRelay(counting))
        replies: list[bytes] = []
        askers = [
            threading.Thread(target=lambda n=n: replies.append(ask(relay.address, b"%d\n" % n)))
            for n in range(5)
        ]
        for asker in askers:
            asker.start()
        for asker in askers:
            asker.join(5)

    assert sorted(replies) == [b"pong to %d" % n for n in range(5)]
    assert len(opened) == 5


def test_a_response_still_arrives_after_the_client_has_stopped_sending(
    running: Callable[[PortRelay], PortRelay],
) -> None:
    # The client library's forward closes everything on the local side's end of input,
    # dropping an answer on its way, so that end is never passed on.
    shutdowns: list[int] = []

    def slow(connection: socket.socket) -> None:
        connection.recv(1024)
        time.sleep(0.3)
        connection.sendall(b"late answer")
        connection.close()

    listener, open_channel = server(slow)

    class Watched:
        def __init__(self) -> None:
            self.inner = open_channel()

        def recv(self, size: int) -> bytes:
            return self.inner.recv(size)

        def sendall(self, data: bytes) -> None:
            self.inner.sendall(data)

        def shutdown(self, how: int) -> None:
            shutdowns.append(how)
            self.inner.shutdown(how)

        def close(self) -> None:
            self.inner.close()

    with listener:
        relay = running(PortRelay(Watched))
        with socket.create_connection(relay.address, timeout=5) as connection:
            connection.sendall(b"question")
            connection.shutdown(socket.SHUT_WR)
            received = b""
            while chunk := connection.recv(1024):
                received += chunk

    assert received == b"late answer"
    assert shutdowns == []


def test_a_channel_that_cant_be_opened_closes_that_connection_only(
    running: Callable[[PortRelay], PortRelay],
) -> None:
    listener, open_channel = server(answers(b"pong"))
    errors: list[Exception] = []
    attempts = iter([ConnectionRefusedError("forwarding refused"), None])

    def sometimes() -> Channel:
        failure = next(attempts)
        if failure is not None:
            raise failure
        return open_channel()

    with listener:
        relay = running(PortRelay(sometimes, on_error=errors.append))

        assert ask(relay.address, b"first\n") == b""
        assert ask(relay.address, b"second\n") == b"pong to second"

    assert [str(error) for error in errors] == ["forwarding refused"]


def test_closing_stops_accepting() -> None:
    listener, open_channel = server(answers(b"pong"))
    with listener:
        relay = PortRelay(open_channel)
        serving = threading.Thread(target=relay.serve, daemon=True)
        serving.start()
        address = relay.address

        relay.close()
        serving.join(5)

        assert not serving.is_alive()
        with pytest.raises(OSError):
            socket.create_connection(address, timeout=1).close()
