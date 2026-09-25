"""fm350mac-helper: the root-privileged half of fm350mac's privilege
separation (see ../../../docs/macos-driver.md, "Privilege separation").

STANDALONE FILE. This is copied verbatim to
``/usr/local/libexec/fm350mac-helper`` and run as root by the system
``/usr/bin/python3 -I -S`` (Apple's Command Line Tools Python, 3.9.6),
launched by a LaunchDaemon. It must therefore:

  * use only the standard library (no third-party dependencies, no venv);
  * run correctly on Python 3.9 (no ``match``, no ``X | Y`` unions evaluated
    at runtime -- only inside ``from __future__ import annotations``, which
    just stringifies them);
  * never import anything from the ``fm350mac`` package. A few small pieces
    (utun creation, IPv4 validation, the NetConfig capture/restore logic)
    are therefore deliberately duplicated here rather than shared -- see
    ``fm350mac.utun``, ``fm350mac.at.valid_assigned_ipv4`` and
    ``fm350mac.netconfig.NetConfig`` for the originals.

Protocol: one JSON object per line over a Unix socket, request -> response,
at most ``MAX_MESSAGE_BYTES`` per message. See ``_OP_SCHEMAS`` for the exact
set of operations and fields; unknown ops/fields are rejected. The
``open_utun`` response carries the new utun's file descriptor as ancillary
data (``SCM_RIGHTS``) alongside its JSON line.

Concurrency: connections are accepted and handled one at a time in a single
thread (a second, concurrent client simply waits in the kernel's listen
backlog until the first disconnects -- queued, not rejected). This keeps a
root-owned process free of any locking between requests.

Every change a connection makes (interface address, routes, DNS) is undone,
in reverse order, when that connection closes -- for any reason, including
the client being SIGKILLed or crashing: closing the JSON socket is what
drives cleanup, and needs no cooperation from the client.
"""

from __future__ import annotations

import argparse
import ctypes
import fcntl
import ipaddress
import json
import logging
import os
import re
import signal
import socket
import stat
import struct
import subprocess
import sys
import tempfile
from typing import Any, Callable, Dict, List, Optional, Tuple

PROTOCOL_VERSION = 1
DEFAULT_SOCKET_PATH = "/var/run/fm350mac-helper.sock"
MAX_MESSAGE_BYTES = 4096

# The key under the LaunchDaemon plist's `Sockets` dict (see cli.py's
# `helper install`). If launchd didn't hand us a socket under this name, we
# refuse to start unless run with --standalone (manual runs/testing only),
# in which case we bind the path ourselves -- see main()/bind_own_socket().
_LAUNCHD_SOCKET_NAME = "Listener"

# Absolute paths only: never resolved through $PATH, and no shell is ever
# invoked for any of these.
IFCONFIG = "/sbin/ifconfig"
ROUTE = "/sbin/route"
SCUTIL = "/usr/sbin/scutil"
COMMAND_TIMEOUT_S = 10
_MINIMAL_ENV = {"PATH": "/usr/bin:/usr/sbin:/bin:/sbin"}

_DNS_KEY = "State:/Network/Service/fm350mac/DNS"

# add_host_route is only ever used for `up --loopback`'s smoke-test host
# route (see loopback.py): restrict it to that one /24 (RFC 5737 TEST-NET-2)
# rather than accepting an arbitrary destination from the main process.
_LOOPBACK_HOST_NET = ipaddress.ip_network("198.51.100.0/24")

_log = logging.getLogger("fm350mac-helper")

# The in-flight connection's handler, if any -- read only by the SIGTERM
# handler below, so a `helper uninstall` (launchctl bootout -> SIGTERM)
# during an active `up` session still tears down its routes/DNS rather than
# just dropping them. Ordinary disconnects need no signal at all: closing
# the JSON socket already drives ConnectionHandler.handle()'s cleanup.
_active_handler: Optional["ConnectionHandler"] = None


class HelperProtocolError(Exception):
    """A malformed, disallowed, or otherwise rejected request."""


# --- IPv4 validation (copy of fm350mac.at.valid_assigned_ipv4) --------------


def valid_assigned_ipv4(value: Any) -> Optional[str]:
    """True-ish (returns the address) if ``value`` is a plausible unicast
    IPv4 address suitable for ``ifconfig``: rejects anything that isn't a
    string, isn't parseable, or is unspecified/multicast/loopback/link-local
    (in particular this is what rejects "0.0.0.0" and malformed octets like
    "999.1.1.1").
    """
    if not isinstance(value, str):
        return None
    try:
        addr = ipaddress.IPv4Address(value)
    except ValueError:
        return None
    if addr.is_unspecified or addr.is_multicast or addr.is_loopback or addr.is_link_local:
        return None
    return value


# --- utun creation (copy of fm350mac.utun.Utun.open()'s ioctl logic) --------

UTUN_CONTROL_NAME = "com.apple.net.utun_control"
UTUN_OPT_IFNAME = 2
# Darwin sys/sys/kern_control.h: struct ctl_info { u_int32_t ctl_id; char ctl_name[96]; }
_CTLIOCGINFO = 0xC0644E03
_CTL_INFO_FMT = "I96s"
_MAX_KCTL_NAME = 96


def open_utun() -> Tuple[int, str]:
    """Create a new utun interface (unit 0: let the kernel auto-assign the
    next free one) and return ``(fd, ifname)``. The caller owns the fd.
    """
    sock = socket.socket(socket.PF_SYSTEM, socket.SOCK_DGRAM, socket.SYSPROTO_CONTROL)
    try:
        name_bytes = UTUN_CONTROL_NAME.encode() + b"\x00"
        info = struct.pack(_CTL_INFO_FMT, 0, name_bytes.ljust(_MAX_KCTL_NAME, b"\x00"))
        info = fcntl.ioctl(sock.fileno(), _CTLIOCGINFO, info)
        ctl_id, _name = struct.unpack(_CTL_INFO_FMT, info)
        sock.connect((ctl_id, 0))
        raw_name = sock.getsockopt(socket.SYSPROTO_CONTROL, UTUN_OPT_IFNAME, 16)
        ifname = raw_name.split(b"\x00", 1)[0].decode()
    except Exception:
        sock.close()
        raise
    return sock.detach(), ifname


# --- peer credential check (LOCAL_PEERCRED / struct xucred) -----------------

SOL_LOCAL = 0
LOCAL_PEERCRED = 0x0001
# macOS <sys/ucred.h>: struct xucred { u_int cr_version; uid_t cr_uid; short
# cr_ngroups; gid_t cr_groups[NGROUPS_MAX=16]; } -- uid_t/gid_t are 4 bytes,
# so there are 2 padding bytes before the (4-byte aligned) groups array.
_XUCRED_FMT = "=IIH2x16I"
_XUCRED_SIZE = struct.calcsize(_XUCRED_FMT)


def get_peer_uid(conn: socket.socket) -> int:
    """The connecting process's real uid, via LOCAL_PEERCRED. Raises OSError
    if the credential lookup itself fails (e.g. the peer already vanished).
    """
    raw = conn.getsockopt(SOL_LOCAL, LOCAL_PEERCRED, _XUCRED_SIZE)
    _version, uid, _ngroups = struct.unpack_from(_XUCRED_FMT, raw)[:3]
    return uid


# --- launchd socket activation (launch_activate_socket via ctypes) ---------


def get_launchd_sockets(name: str = _LAUNCHD_SOCKET_NAME) -> Optional[List[int]]:
    """Retrieve the socket(s) launchd created for us per the plist's
    `Sockets` dict, via ``launch_activate_socket(3)``. Returns None if we
    weren't launched by launchd with that socket name (e.g. run by hand),
    in which case the caller binds the path itself.
    """
    try:
        libsystem = ctypes.CDLL(None, use_errno=True)
        func = libsystem.launch_activate_socket
    except (OSError, AttributeError):
        return None
    func.argtypes = [ctypes.c_char_p, ctypes.POINTER(ctypes.POINTER(ctypes.c_int)), ctypes.POINTER(ctypes.c_size_t)]
    func.restype = ctypes.c_int
    fds_ptr = ctypes.POINTER(ctypes.c_int)()
    count = ctypes.c_size_t()
    rc = func(name.encode(), ctypes.byref(fds_ptr), ctypes.byref(count))
    if rc != 0 or count.value == 0:
        return None
    fds = [fds_ptr[i] for i in range(count.value)]
    ctypes.CDLL(None).free(fds_ptr)
    return fds


class HelperStartError(Exception):
    """A startup-time safety check failed; refuse to run."""


def bind_own_socket(path: str, allowed_uid: int) -> socket.socket:
    """Bind ``path`` ourselves (mode 0600, owned by ``allowed_uid``) for
    ``--standalone`` mode (no launchd socket activation -- see the module
    docstring's Concurrency/main() notes). Never binds directly at ``path``:
    the socket is created inside a fresh temporary directory next to it
    (``mkdtemp`` guarantees mode 0700, owned by whoever is calling this --
    root, in production), given its final mode/owner there, and only then
    ``rename()``d into place -- atomically, and never through a symlink or
    half-configured permissions that a connecting client could race.
    """
    target_dir = os.path.dirname(path) or "."
    try:
        existing = os.lstat(path)
    except FileNotFoundError:
        pass
    else:
        if not stat.S_ISSOCK(existing.st_mode):
            raise HelperStartError(f"{path} already exists and isn't a socket; refusing to bind over it")

    # mkdtemp() already guarantees a fresh directory, mode 0700, owned by
    # whoever is calling this (root, in production) -- nothing else to set.
    tmp_dir = tempfile.mkdtemp(prefix=".fm350mac-helper-", dir=target_dir)
    try:
        tmp_sock_path = os.path.join(tmp_dir, "s")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.bind(tmp_sock_path)
            os.chmod(tmp_sock_path, 0o600)
            os.chown(tmp_sock_path, allowed_uid, -1)
            sock.listen(5)
            os.rename(tmp_sock_path, path)
        except Exception:
            sock.close()
            raise
    finally:
        try:
            os.rmdir(tmp_dir)
        except OSError:
            pass
    return sock


# --- command execution -------------------------------------------------


def run_command(
    argv: List[str],
    *,
    runner: Callable[..., Any] = subprocess.run,
    timeout: float = COMMAND_TIMEOUT_S,
    input_text: Optional[str] = None,
) -> str:
    """Run ``argv`` (absolute paths only) with a minimal environment, no
    shell, and a timeout. Raises on a non-zero exit or timeout.
    """
    result = runner(
        argv, capture_output=True, text=True, timeout=timeout, env=dict(_MINIMAL_ENV), input=input_text, check=True
    )
    return result.stdout


# --- per-connection network state (copy of fm350mac.netconfig.NetConfig) ---


class NetState:
    """Tracks and undoes the network changes made for one utun interface,
    with the same capture/restore semantics as ``fm350mac.netconfig.NetConfig``:
    idempotent, and never captures an interface it owns as "the previous
    default route". ``teardown()`` undoes everything, in reverse order,
    best-effort (a failure in one step never skips the rest).
    """

    def __init__(self, ifname: str, *, runner: Callable[..., Any] = subprocess.run) -> None:
        self.ifname = ifname
        self._runner = runner
        self.commands: List[List[str]] = []  # every argv run, in order (tests only)
        self._configured_ip: Optional[str] = None
        self._host_route: Optional[str] = None
        self._default_route_added = False
        self._prev_default_restore_cmd: Optional[List[str]] = None
        self._dns_set = False

    def _run(self, argv: List[str]) -> str:
        self.commands.append(list(argv))
        return run_command(argv, runner=self._runner)

    def _run_scutil(self, script: str) -> str:
        self.commands.append([SCUTIL] + script.strip().splitlines())
        return run_command([SCUTIL], runner=self._runner, input_text=script)

    def configure_interface(self, ip: str, mtu: int = 1500) -> None:
        self._run([IFCONFIG, self.ifname, "inet", ip, ip, "mtu", str(mtu), "up"])
        self._configured_ip = ip

    def reconfigure_address(self, old_ip: str, new_ip: str) -> None:
        if old_ip != self._configured_ip:
            raise HelperProtocolError(f"reconfigure_address: old_ip {old_ip!r} isn't the configured address")
        self._run([IFCONFIG, self.ifname, "inet", old_ip, "delete"])
        self._run([IFCONFIG, self.ifname, "inet", new_ip, new_ip, "mtu", "1500", "up"])
        self._configured_ip = new_ip

    def add_host_route(self, dest: str) -> None:
        if self._host_route is not None:
            raise HelperProtocolError("a host route is already installed on this connection")
        self._run([ROUTE, "add", "-host", dest, "-interface", self.ifname])
        self._host_route = dest

    def enable_default_route(self) -> None:
        """See fm350mac.netconfig.NetConfig.add_default_route()'s docstring
        for the full rationale: idempotent for our own interface, and never
        re-captures "the previous default" once it's really just our route.
        """
        if self._default_route_added:
            return
        prev_gateway = prev_iface = None
        try:
            out = self._runner(
                [ROUTE, "-n", "get", "default"],
                capture_output=True, text=True, timeout=COMMAND_TIMEOUT_S, env=dict(_MINIMAL_ENV), check=True,
            ).stdout
            gw_match = re.search(r"gateway:\s*(\S+)", out)
            if_match = re.search(r"interface:\s*(\S+)", out)
            prev_gateway = gw_match.group(1) if gw_match else None
            prev_iface = if_match.group(1) if if_match else None
        except subprocess.CalledProcessError:
            _log.warning("could not read the previous default route")
        if prev_iface == self.ifname:
            prev_gateway = prev_iface = None
        if prev_gateway:
            self._prev_default_restore_cmd = [ROUTE, "add", "default", prev_gateway]
        elif prev_iface:
            self._prev_default_restore_cmd = [ROUTE, "add", "default", "-interface", prev_iface]
        else:
            self._prev_default_restore_cmd = None
        if self._prev_default_restore_cmd is not None:
            self._run([ROUTE, "delete", "default"])
        self._run([ROUTE, "add", "default", "-interface", self.ifname])
        self._default_route_added = True

    def disable_default_route(self) -> None:
        if not self._default_route_added:
            return
        self._default_route_added = False
        try:
            self._run([ROUTE, "delete", "default"])
        except Exception:
            _log.exception("failed to delete our default route")
        restore_cmd, self._prev_default_restore_cmd = self._prev_default_restore_cmd, None
        if restore_cmd is not None:
            try:
                self._run(restore_cmd)
            except Exception:
                _log.exception("failed to restore the previous default route")

    def set_dns(self, servers: List[str]) -> None:
        if not servers:
            return
        script = "d.init\n" + f"d.add ServerAddresses * {' '.join(servers)}\n" + f"set {_DNS_KEY}\n"
        self._run_scutil(script)
        self._dns_set = True

    def clear_dns(self) -> None:
        if not self._dns_set:
            return
        self._dns_set = False
        try:
            self._run_scutil(f"remove {_DNS_KEY}\n")
        except Exception:
            _log.exception("failed to remove DNS key")

    def remove_host_route(self) -> None:
        if self._host_route is None:
            return
        host_ip, self._host_route = self._host_route, None
        try:
            self._run([ROUTE, "delete", "-host", host_ip])
        except Exception:
            _log.exception("failed to remove host route to %s", host_ip)

    def bring_down(self) -> None:
        if self._configured_ip is None:
            return
        self._configured_ip = None
        try:
            self._run([IFCONFIG, self.ifname, "down"])
        except Exception:
            _log.exception("failed to bring down %s", self.ifname)

    def teardown(self) -> None:
        """Undo everything, in reverse order: DNS, default route, host
        route, then the interface itself. Idempotent (each step clears its
        own state before running, so calling this twice is a no-op the
        second time) and best-effort (never raises).
        """
        self.clear_dns()
        self.disable_default_route()
        self.remove_host_route()
        self.bring_down()


# --- request schema ----------------------------------------------------

_OP_SCHEMAS: Dict[str, set] = {
    "hello": {"version"},
    "open_utun": set(),
    "set_address": {"ip"},
    "reconfigure_address": {"old_ip", "new_ip"},
    "add_host_route": {"dest"},
    "set_default_route": {"enable"},
    "set_dns": {"servers"},
    "clear_dns": set(),
    "teardown": set(),
}


def _validate_request(req: Any) -> str:
    if not isinstance(req, dict):
        raise HelperProtocolError("request must be a JSON object")
    op = req.get("op")
    if not isinstance(op, str) or op not in _OP_SCHEMAS:
        raise HelperProtocolError(f"unknown op: {op!r}")
    allowed = _OP_SCHEMAS[op] | {"op"}
    extra = set(req.keys()) - allowed
    if extra:
        raise HelperProtocolError(f"unknown field(s) for {op}: {sorted(extra)}")
    missing = _OP_SCHEMAS[op] - set(req.keys())
    if missing:
        raise HelperProtocolError(f"missing field(s) for {op}: {sorted(missing)}")
    return op


# --- line-buffered JSON reader -------------------------------------------


class _LineReader:
    """Reads ``\\n``-delimited messages off a stream socket, rejecting
    anything that grows past ``max_bytes`` before a newline ever arrives.
    """

    def __init__(self, sock: socket.socket, max_bytes: int = MAX_MESSAGE_BYTES) -> None:
        self._sock = sock
        self._buf = b""
        self._max_bytes = max_bytes

    def read_line(self) -> Optional[bytes]:
        """Return the next line (without its trailing ``\\n``), or None on EOF."""
        while b"\n" not in self._buf:
            if len(self._buf) >= self._max_bytes:
                raise HelperProtocolError(f"request exceeds {self._max_bytes} bytes")
            chunk = self._sock.recv(4096)
            if not chunk:
                return None
            self._buf += chunk
        line, self._buf = self._buf.split(b"\n", 1)
        if len(line) > self._max_bytes:
            raise HelperProtocolError(f"request exceeds {self._max_bytes} bytes")
        return line


# --- connection handler ---------------------------------------------------


class ConnectionHandler:
    """Handles the JSON-lines request/response loop for one connection, and
    tears down (via ``NetState.teardown()``) whatever it configured when the
    loop ends -- for any reason.
    """

    def __init__(
        self,
        conn: socket.socket,
        *,
        open_utun_fn: Callable[[], Tuple[int, str]] = open_utun,
        runner: Callable[..., Any] = subprocess.run,
        log: Optional[logging.Logger] = None,
    ) -> None:
        self._conn = conn
        self._reader = _LineReader(conn)
        self._open_utun_fn = open_utun_fn
        self._runner = runner
        self._log = log or _log
        self._utun_opened = False
        self.net: Optional[NetState] = None

    def handle(self) -> None:
        try:
            while True:
                try:
                    line = self._reader.read_line()
                except HelperProtocolError as exc:
                    # e.g. an oversize message: reject it and stop, rather
                    # than trying to resynchronise on a stream we can no
                    # longer trust the framing of.
                    self._send_error(str(exc))
                    break
                if line is None:
                    break
                self._handle_line(line)
        finally:
            if self.net is not None:
                self.net.teardown()

    def _handle_line(self, line: bytes) -> None:
        try:
            req = json.loads(line.decode())
            op = _validate_request(req)
        except (json.JSONDecodeError, HelperProtocolError, UnicodeDecodeError, ValueError, RecursionError) as exc:
            # ValueError covers things like a too-deeply-nested JSON payload
            # tripping the decoder's own recursion guard as a plain
            # ValueError on some platforms; RecursionError covers the rest
            # (e.g. "[[[[...]]]]" deep enough to blow the interpreter's
            # stack) -- either way, this is a malformed/hostile request, not
            # a crash.
            self._send_error(str(exc) or f"{type(exc).__name__} while parsing the request")
            return
        try:
            self._dispatch(op, req)
        except HelperProtocolError as exc:
            self._send_error(str(exc))
        except Exception as exc:
            self._log.exception("op %r failed", op)
            self._send_error(f"{op} failed: {exc}")

    def _dispatch(self, op: str, req: Dict[str, Any]) -> None:
        if op == "hello":
            self._op_hello(req)
        elif op == "open_utun":
            self._op_open_utun()
        elif op == "set_address":
            self._require_utun()
            ip = self._require_valid_ip(req["ip"], "ip")
            self.net.configure_interface(ip)
            self._send_ok()
        elif op == "reconfigure_address":
            self._require_utun()
            old_ip = self._require_valid_ip(req["old_ip"], "old_ip")
            new_ip = self._require_valid_ip(req["new_ip"], "new_ip")
            self.net.reconfigure_address(old_ip, new_ip)
            self._send_ok()
        elif op == "add_host_route":
            self._require_utun()
            dest = self._require_loopback_host(req["dest"])
            self.net.add_host_route(dest)
            self._send_ok()
        elif op == "set_default_route":
            self._require_utun()
            enable = req["enable"]
            if not isinstance(enable, bool):
                raise HelperProtocolError("enable must be a boolean")
            if enable:
                self.net.enable_default_route()
            else:
                self.net.disable_default_route()
            self._send_ok()
        elif op == "set_dns":
            self._require_utun()
            servers = self._require_dns_servers(req["servers"])
            self.net.set_dns(servers)
            self._send_ok()
        elif op == "clear_dns":
            self._require_utun()
            self.net.clear_dns()
            self._send_ok()
        elif op == "teardown":
            if self.net is not None:
                self.net.teardown()
            self._send_ok()
        else:
            raise HelperProtocolError(f"unhandled op: {op!r}")  # pragma: no cover (schema already rejects this)

    # --- op implementations -------------------------------------------

    def _op_hello(self, req: Dict[str, Any]) -> None:
        version = req["version"]
        if version != PROTOCOL_VERSION:
            raise HelperProtocolError(f"unsupported protocol version {version!r} (helper is {PROTOCOL_VERSION})")
        self._send({"ok": True, "version": PROTOCOL_VERSION, "pid": os.getpid()})

    def _op_open_utun(self) -> None:
        if self._utun_opened:
            raise HelperProtocolError("open_utun already called on this connection")
        fd, ifname = self._open_utun_fn()
        self._utun_opened = True
        self.net = NetState(ifname, runner=self._runner)
        try:
            line = (json.dumps({"ok": True, "ifname": ifname}) + "\n").encode()
            self._conn.sendmsg([line], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, struct.pack("i", fd))])
        finally:
            # Our copy is no longer needed once it's been sent: the client
            # now owns the only reference, so the utun goes away with it
            # (immediately if it never reads the message, or when its
            # process exits/is killed) -- see the module docstring.
            os.close(fd)

    # --- validators (raise HelperProtocolError) -------------------------

    def _require_utun(self) -> None:
        if self.net is None:
            raise HelperProtocolError("open_utun must be called first")

    def _require_valid_ip(self, value: Any, field: str) -> str:
        ip = valid_assigned_ipv4(value)
        if ip is None:
            raise HelperProtocolError(f"invalid {field}: {value!r}")
        return ip

    def _require_loopback_host(self, value: Any) -> str:
        if not isinstance(value, str):
            raise HelperProtocolError("dest must be a string")
        try:
            addr = ipaddress.IPv4Address(value)
        except ValueError:
            raise HelperProtocolError(f"invalid dest: {value!r}") from None
        if addr not in _LOOPBACK_HOST_NET:
            raise HelperProtocolError(f"dest must be within {_LOOPBACK_HOST_NET} (loopback mode only): {value!r}")
        return value

    def _require_dns_servers(self, value: Any) -> List[str]:
        if not isinstance(value, list) or not (1 <= len(value) <= 3):
            raise HelperProtocolError("servers must be a list of 1-3 IPv4 addresses")
        for server in value:
            if valid_assigned_ipv4(server) is None:
                raise HelperProtocolError(f"invalid DNS server: {server!r}")
        return value

    # --- responses --------------------------------------------------------

    def _send(self, obj: Dict[str, Any]) -> None:
        self._conn.sendall((json.dumps(obj) + "\n").encode())

    def _send_ok(self, **extra: Any) -> None:
        self._send({"ok": True, **extra})

    def _send_error(self, message: str) -> None:
        self._send({"ok": False, "error": message})


# --- server loop -----------------------------------------------------------


def _send_reject(conn: socket.socket, message: str) -> None:
    try:
        conn.sendall((json.dumps({"ok": False, "error": message}) + "\n").encode())
    except OSError:
        pass


def handle_one_connection(
    conn: socket.socket,
    allowed_uid: int,
    *,
    open_utun_fn: Callable[[], Tuple[int, str]] = open_utun,
    runner: Callable[..., Any] = subprocess.run,
    get_peer_uid_fn: Callable[[socket.socket], int] = get_peer_uid,
    log: Optional[logging.Logger] = None,
) -> None:
    """Authenticate the peer, then run its request loop until it disconnects."""
    log = log or _log
    try:
        peer_uid = get_peer_uid_fn(conn)
    except OSError:
        log.exception("could not read peer credentials; rejecting connection")
        _send_reject(conn, "could not verify peer credentials")
        return
    if peer_uid != allowed_uid:
        log.warning("rejecting connection from uid %d (allowed: %d)", peer_uid, allowed_uid)
        _send_reject(conn, "unauthorized")
        return
    log.info("connection accepted from uid %d", peer_uid)
    global _active_handler
    handler = ConnectionHandler(conn, open_utun_fn=open_utun_fn, runner=runner, log=log)
    _active_handler = handler
    try:
        handler.handle()
    finally:
        _active_handler = None
    log.info("connection closed; teardown complete")


def serve_forever(
    listen_sock: socket.socket,
    allowed_uid: int,
    *,
    open_utun_fn: Callable[[], Tuple[int, str]] = open_utun,
    runner: Callable[..., Any] = subprocess.run,
    get_peer_uid_fn: Callable[[socket.socket], int] = get_peer_uid,
    log: Optional[logging.Logger] = None,
) -> None:
    """Accept and fully handle one connection at a time, forever (until
    ``listen_sock`` is closed, e.g. by another thread for a clean shutdown in
    tests -- accept() then raises OSError and this just returns). A second,
    concurrent client just waits in the kernel's listen backlog until the
    first disconnects (queued, not rejected) -- see the module docstring.
    """
    log = log or _log
    while True:
        try:
            conn, _addr = listen_sock.accept()
        except OSError:
            return
        try:
            handle_one_connection(
                conn, allowed_uid, open_utun_fn=open_utun_fn, runner=runner, get_peer_uid_fn=get_peer_uid_fn, log=log
            )
        except Exception:
            log.exception("connection handler crashed")
        finally:
            conn.close()


# --- entry point -----------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fm350mac-helper")
    parser.add_argument("--allowed-uid", type=int, required=True, help="only this uid's connections are served")
    parser.add_argument("--socket-path", default=DEFAULT_SOCKET_PATH, help="only used with --standalone")
    parser.add_argument(
        "--standalone", action="store_true",
        help="bind our own socket instead of requiring launchd socket activation "
        "(manual runs/testing only -- the installed LaunchDaemon never needs this)",
    )
    return parser


def _setup_logging() -> None:
    # No syslog socket dependency: launchd captures stderr on its own
    # (see the plist's StandardErrorPath, or the unified log by default).
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="fm350mac-helper[%(process)d] %(levelname)s %(message)s")


def main(argv: Optional[List[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    _setup_logging()

    if args.allowed_uid == 0:
        _log.error("--allowed-uid 0 (root) is refused: the allowed uid must be an unprivileged user")
        return 1

    fds = get_launchd_sockets()
    if fds:
        listen_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM, 0, fileno=fds[0])
        _log.info("using the launchd-activated socket")
    elif args.standalone:
        try:
            listen_sock = bind_own_socket(args.socket_path, args.allowed_uid)
        except HelperStartError as exc:
            _log.error("could not bind %s: %s", args.socket_path, exc)
            return 1
        _log.info("bound %s (mode 0600, uid %d) [--standalone]", args.socket_path, args.allowed_uid)
    else:
        _log.error(
            "no launchd socket activation available (expected the LaunchDaemon's Sockets entry); "
            "pass --standalone to bind a socket ourselves instead (manual runs/testing only)"
        )
        return 1

    _log.info("fm350mac-helper started: pid=%d allowed_uid=%d", os.getpid(), args.allowed_uid)

    # Best-effort: if we're killed while a connection is mid-flight (e.g. an
    # operator runs `helper uninstall` while `up` is running), still try to
    # undo whatever that connection had configured rather than leaving
    # routes/DNS behind. Not a substitute for the per-connection cleanup on
    # ordinary disconnect, which needs no signal at all.
    def _on_term(_signum, _frame):
        _log.warning("received SIGTERM; tearing down the active connection (if any) and exiting")
        if _active_handler is not None and _active_handler.net is not None:
            _active_handler.net.teardown()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _on_term)

    try:
        serve_forever(listen_sock, args.allowed_uid)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
