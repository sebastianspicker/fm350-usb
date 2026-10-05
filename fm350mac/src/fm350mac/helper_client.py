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

import errno
import json
import logging
import os
import socket
import struct
from typing import Any

from . import __version__
from .utun import Utun

_log = logging.getLogger(__name__)

HELPER_SOCKET_PATH = "/var/run/fm350mac-helper.sock"
HELPER_LOG_PATH = "/var/log/fm350mac-helper.log"
PROTOCOL_VERSION = 1
_MAX_MESSAGE_BYTES = 4096
_FD_CMSG_SPACE = socket.CMSG_LEN(struct.calcsize("i"))


class HelperError(Exception):
    """The helper rejected a request, or the connection failed/closed."""


# Error texts that mean this driver and the installed helper don't speak the
# same protocol (client- or helper-side), as opposed to an operational
# refusal like "at most 8 host routes per connection".
_MISMATCH_MARKERS = (
    "protocol version mismatch",
    "unsupported protocol version",
    "unknown op:",
    "unknown field(s)",
    "missing field(s)",
)


def is_version_mismatch(exc: BaseException) -> bool:
    """True if ``exc`` says the installed helper doesn't match this driver
    (so reinstalling it is the fix), False for any other helper error.
    """
    text = str(exc)
    return any(marker in text for marker in _MISMATCH_MARKERS)


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
        self.helper_version: str | None = None  # None: not yet said hello, or an older helper without it

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
        version = resp.get("helper_version")
        self.helper_version = version if isinstance(version, str) else None
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
    ``add_default_route``, ``set_dns``, ``teardown``, plus the ``remove_*``/
    ``clear_dns`` calls used while waiting for a re-enumeration), by forwarding every
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
        """Up to 8 per connection (loopback's smoke-test route and
        ``up --route-host`` share the limit); the helper deletes them, newest
        first, on teardown.
        """
        self._client.request("add_host_route", dest=host_ip)

    def remove_host_routes(self) -> None:
        self._client.request("clear_host_routes")

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


# launchd starts the helper on the first connection; a cold start of
# /usr/bin/python3 measured >2s on macOS 27 (hello timed out, a retry seconds
# later succeeded), so allow generously for it.
PROBE_TIMEOUT_S = 10.0


def helper_version_warning(client: Any) -> str | None:
    """A warning text if the helper's reported version is missing (an older
    helper) or differs from this driver's, else None. Warn-only: callers
    never refuse on a mismatch.
    """
    reported = getattr(client, "helper_version", None)
    if reported == __version__:
        return None
    shown = reported if reported else "older than 0.1.0a1 (no version reported)"
    return (
        f"installed helper is {shown}, driver is {__version__}: "
        'run sudo "$(command -v fm350mac)" helper install'
    )


_REASON_UNREACHABLE = "not reachable (not installed, not running, or a protocol mismatch)"


def as_probe_result(result: Any) -> tuple[Any, str | None]:
    """Normalize a probe factory's return value to ``(client | None, reason)``.
    Accepts both ``probe()``'s ``client | None`` and ``probe_with_reason()``'s tuple.
    """
    if isinstance(result, tuple):
        return result
    if result is None:
        return None, _REASON_UNREACHABLE
    return result, None


def _permission_denied_reason(path: str) -> str:
    """Why connecting to the helper socket at ``path`` was refused: report
    its owner uid and ours. A differing owner means the helper was installed
    for another user (reinstall); a matching one means something else -- a
    sandbox or privacy setting -- blocked the connection.
    """
    uid = os.getuid()
    try:
        owner: int | None = os.stat(path).st_uid
    except OSError:
        owner = None
    shown = "unknown" if owner is None else str(owner)
    reason = (
        f"permission denied connecting to {path} (socket owner uid {shown}, your uid {uid}); "
        "if the uids match, the connection was blocked by a sandbox/privacy setting"
    )
    if owner is not None and owner != uid:
        reason += '; the helper was installed for another user: reinstall it as this user with sudo "$(command -v fm350mac)" helper install'
    return reason


def probe_with_reason(
    path: str = HELPER_SOCKET_PATH, timeout: float = PROBE_TIMEOUT_S
) -> tuple[HelperClient | None, str | None]:
    """Like ``probe()``, but returns ``(client, None)`` or ``(None, reason)``
    with a human-readable reason the helper is unreachable.
    """
    try:
        client = HelperClient.connect(path, timeout=timeout)
    except socket.timeout:
        return None, f"the helper is not running (connect timed out); see {HELPER_LOG_PATH}"
    except OSError as exc:
        code = exc.errno
        if code == errno.ENOENT:
            return None, "the helper is not installed (no socket); run: sudo \"$(command -v fm350mac)\" helper install"
        if code in (errno.EACCES, errno.EPERM):
            return None, _permission_denied_reason(path)
        if code == errno.ECONNREFUSED:
            return None, f"the helper is not running (connection refused); see {HELPER_LOG_PATH}"
        return None, f"cannot connect to the helper: {exc}"
    try:
        client.hello()
    except HelperError as exc:
        client.close()
        return None, f"helper protocol mismatch: {exc}; reinstall the helper"
    except socket.timeout:
        client.close()
        return None, f"the helper is not running (hello timed out); see {HELPER_LOG_PATH}"
    except (OSError, ValueError) as exc:
        client.close()
        return None, f"the helper did not answer correctly: {exc}; see {HELPER_LOG_PATH}"
    return client, None


def probe(path: str = HELPER_SOCKET_PATH, timeout: float = PROBE_TIMEOUT_S) -> HelperClient | None:
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
