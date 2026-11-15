"""A local port that relays each connection to a port inside the cluster.

The client library can open a port-forward to a pod, but it hands back one socket-like
channel per forward and no listener: nothing on the submitter's machine can connect to it.
:class:`PortRelay` is that listener. It binds a local port, and for every connection it
accepts it opens a channel of its own to the pod and copies bytes both ways until either
side closes. One channel per connection, because a browser opens several, and an HTTP
connection's bytes must not interleave with another's.

Nothing here imports the client library: what opens a channel is given, so the relay is
tested against a plain local server.
"""

from __future__ import annotations

import contextlib
import socket
import threading
from collections.abc import Callable
from typing import Protocol

__all__ = ["Channel", "ForwardingNotAllowed", "PortRelay"]


class ForwardingNotAllowed(Exception):
    """The credentials in use may not open a port-forward: nothing is listened on."""


_CHUNK = 64 * 1024
#: How often the accept loop looks whether it should stop.
_ACCEPT_POLL_SECONDS = 0.5


class Channel(Protocol):
    """The part of a socket a relayed connection uses, on either side."""

    def recv(self, bufsize: int, /) -> bytes: ...

    def sendall(self, data: bytes, /) -> None: ...

    def shutdown(self, how: int, /) -> None: ...

    def close(self) -> None: ...


class PortRelay:
    """Listen on a local port, and relay each connection through a channel of its own.

    *open_channel* opens one channel to the remote port; it may raise, and the connection
    that asked for it is then closed and *on_error* told why. The relay binds at once, so
    :attr:`address` is known before :meth:`serve` runs. Port ``0`` picks a free one.
    """

    #: How long a response may still arrive after the local side has stopped sending.
    grace: float = 30.0

    def __init__(
        self,
        open_channel: Callable[[], Channel],
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        on_error: Callable[[Exception], None] = lambda error: None,
    ) -> None:
        self._open_channel = open_channel
        self._on_error = on_error
        self._listener = socket.create_server((host, port))
        self._listener.settimeout(_ACCEPT_POLL_SECONDS)
        self._stopping = threading.Event()

    @property
    def address(self) -> tuple[str, int]:
        host, port = self._listener.getsockname()[:2]
        return str(host), int(port)

    def serve(self) -> None:
        """Accept and relay connections until :meth:`close`. Each runs on its own threads."""
        while not self._stopping.is_set():
            try:
                connection, _ = self._listener.accept()
            except TimeoutError:
                continue
            except OSError:
                if self._stopping.is_set():
                    return
                raise
            threading.Thread(
                target=self._relay, args=(connection,), name="dokimi-relay", daemon=True
            ).start()

    def close(self) -> None:
        """Stop accepting. Connections already relayed end when either side closes."""
        self._stopping.set()
        self._listener.close()

    def __enter__(self) -> PortRelay:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _relay(self, connection: socket.socket) -> None:
        try:
            channel = self._open_channel()
        except Exception as error:  # the connection is refused; the relay goes on
            connection.close()
            self._on_error(error)
            return
        back = threading.Thread(
            target=_copy,
            args=(channel, connection),
            kwargs={"half_close": True},
            name="dokimi-relay-back",
            daemon=True,
        )
        back.start()
        # The local side's end is not passed on: the client library's forward closes its
        # whole connection on it, and drops a response still on its way. So the answer
        # gets a grace period to arrive, and then both ends are closed.
        _copy(connection, channel, half_close=False)
        back.join(self.grace)
        for end in (connection, channel):
            with contextlib.suppress(OSError):
                end.close()


def _copy(source: Channel, target: Channel, *, half_close: bool) -> None:
    """Copy until *source* has nothing more to send; with *half_close*, tell *target* so."""
    try:
        while data := source.recv(_CHUNK):
            target.sendall(data)
    except OSError:
        return
    if half_close:
        with contextlib.suppress(OSError):
            target.shutdown(socket.SHUT_WR)
