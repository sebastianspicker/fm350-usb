"""Main-process client for the root helper (see ``helper/fm350mac_helper.py``
and docs/macos-driver.md, "Privilege separation").

``HelperClient`` speaks the helper's JSON-lines protocol over a Unix socket:
connect, ``hello`` (protocol version check), and ``open_utun`` (receives the
new utun's file descriptor via ``SCM_RIGHTS``, wrapped in the same
``fm350mac.utun.Utun`` class the direct/root code path uses -- no separate
utun implementation to keep in sync). ``HelperNetConfig`` implements the
``NetConfig`` interface cli.py/supervisor.py already use, by forwarding each
call to the helper as a request.

Both classes only ever talk this one process's *own* connection: the helper
tracks and undoes everything for a connection when it closes (including a
SIGKILL of this process), so there is no separate "cleanup on crash" path to
implement here.
"""

from __future__ import annotations

import json
import logging
import socket
import struct
from typing import Any

from .utun import Utun

_log = logging.getLogger(__name__)

HELPER_SOCKET_PATH = "/var/run/fm350mac-helper.sock"
PROTOCOL_VERSION = 1
_MAX_MESSAGE_BYTES = 4096
_FD_CMSG_SPACE = socket.CMSG_LEN(struct.calcsize("i"))


class HelperError(Exception):
    """The helper rejected a request, or the connection failed/closed."""


class _LineReader:
    """Reads ``\\n``-delimited messages off a stream socket. Mirrors the
    helper's own reader; kept here rather than shared since the helper file
    must stay import-free from this package (see its module docstring).
    """

    def __init__(self, sock: socket.socket, max_bytes: int = _MAX_MESSAGE_BYTES) -> None:
        self._sock = sock
        self._buf = b""
        self._max_bytes = max_bytes

    def read_line(self) -> bytes | None:
        while b"\n" not in self._buf:
            if len(self._buf) >= self._max_bytes:
                raise HelperError(f"helper response exceeds {self._max_bytes} bytes")
            chunk = self._sock.recv(4096)
            if not chunk:
                return None
            self._buf += chunk
        line, self._buf = self._buf.split(b"\n", 1)
        return line


class HelperClient:
    """A connected session with the root helper."""

    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock
        self._reader = _LineReader(sock)
        self.pid: int | None = None

    @classmethod
    def connect(cls, path: str = HELPER_SOCKET_PATH, timeout: float = 5.0) -> "HelperClient":
        """Connect to the helper's socket. Raises OSError (e.g. FileNotFoundError,
        ConnectionRefusedError) if it isn't installed or isn't listening.
        """
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        try:
            sock.connect(path)
        except OSError:
            sock.close()
            raise
        return cls(sock)

    def request(self, op: str, **fields: Any) -> dict:
        """Send one JSON-lines request and return its decoded response.
        Raises HelperError if the helper reports failure or the connection
        drops.
        """
        payload = {"op": op}
        payload.update(fields)
        self._sock.sendall((json.dumps(payload) + "\n").encode())
        line = self._reader.read_line()
        if line is None:
            raise HelperError("the helper closed the connection")
        resp = json.loads(line.decode())
        if not resp.get("ok"):
            raise HelperError(resp.get("error", f"{op} failed"))
        return resp

    def hello(self) -> dict:
        """Send `hello`, check the protocol version, and record the helper's pid."""
        resp = self.request("hello", version=PROTOCOL_VERSION)
        if resp.get("version") != PROTOCOL_VERSION:
            raise HelperError(f"helper protocol version mismatch: {resp.get('version')!r} (expected {PROTOCOL_VERSION})")
        self.pid = resp.get("pid")
        return resp

    def open_utun(self) -> Utun:
        """Ask the helper to create a utun interface, and return it wrapped
        in a normal ``fm350mac.utun.Utun`` -- same read/write/settimeout/close
        API as the direct/root code path. Max one call per connection (the
        helper rejects a second).
        """
        payload = {"op": "open_utun"}
        self._sock.sendall((json.dumps(payload) + "\n").encode())
        data, ancdata, _flags, _addr = self._sock.recvmsg(_MAX_MESSAGE_BYTES, _FD_CMSG_SPACE)
        if not data:
            raise HelperError("the helper closed the connection during open_utun")
        resp = json.loads(data.split(b"\n", 1)[0].decode())
        if not resp.get("ok"):
            raise HelperError(resp.get("error", "open_utun failed"))
        fd = None
        for level, cmsg_type, cmsg_data in ancdata:
            if level == socket.SOL_SOCKET and cmsg_type == socket.SCM_RIGHTS:
                fd = struct.unpack("i", cmsg_data[:4])[0]
        if fd is None:
            raise HelperError("open_utun response carried no file descriptor")
        sock = socket.socket(socket.PF_SYSTEM, socket.SOCK_DGRAM, socket.SYSPROTO_CONTROL, fileno=fd)
        return Utun(sock, resp["ifname"])

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass

    def __enter__(self) -> "HelperClient":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()


class HelperNetConfig:
    """Implements the same interface as ``fm350mac.netconfig.NetConfig``
    (``configure_interface``, ``reconfigure_address``, ``add_host_route``,
    ``add_default_route``, ``set_dns``, ``teardown``), by forwarding every
    call to the helper over ``client``. ``ifname`` parameters are accepted
    for interface compatibility but not sent: the helper always acts on the
    one utun it created for this connection, never on an interface name
    supplied by the (unprivileged) caller.
    """

    def __init__(self, client: HelperClient) -> None:
        self._client = client
        self.dry_run = False  # for parity with NetConfig; the helper path never runs dry

    def open_utun(self) -> Utun:
        """Passthrough to the underlying HelperClient, so callers only need
        to hold a HelperNetConfig (both the utun and NetConfig factories in
        cli.py's DI wiring come from the same helper session).
        """
        return self._client.open_utun()

    def configure_interface(self, ifname: str, ip: str, mtu: int = 1500) -> None:
        self._client.request("set_address", ip=ip)

    def reconfigure_address(self, ifname: str, old_ip: str, new_ip: str) -> None:
        self._client.request("reconfigure_address", old_ip=old_ip, new_ip=new_ip)

    def add_host_route(self, ifname: str, host_ip: str) -> None:
        self._client.request("add_host_route", dest=host_ip)

    def add_default_route(self, ifname: str) -> None:
        self._client.request("set_default_route", enable=True)

    def remove_default_route(self) -> None:
        self._client.request("set_default_route", enable=False)

    def set_dns(self, servers: list[str]) -> None:
        if not servers:
            return
        self._client.request("set_dns", servers=servers)

    def clear_dns(self) -> None:
        self._client.request("clear_dns")

    def teardown(self) -> None:
        try:
            self._client.request("teardown")
        except HelperError:
            _log.exception("helper teardown request failed")

    def close(self) -> None:
        """Close the underlying connection to the helper (which also drops
        any changes still tracked for it, in reverse order, on the helper
        side -- see teardown()/the module docstring).
        """
        self._client.close()


def probe(path: str = HELPER_SOCKET_PATH, timeout: float = 2.0) -> HelperClient | None:
    """Try to connect to the helper and complete a `hello` round trip.
    Returns a ready-to-use, hello-verified HelperClient, or None if the
    helper isn't installed, isn't running, or doesn't answer -- the caller
    decides what that means (cli.py's ``up`` prints a message and exits;
    ``helper status`` just reports it).
    """
    try:
        client = HelperClient.connect(path, timeout=timeout)
    except OSError:
        return None
    try:
        client.hello()
    except (HelperError, OSError, ValueError):
        client.close()
        return None
    return client
