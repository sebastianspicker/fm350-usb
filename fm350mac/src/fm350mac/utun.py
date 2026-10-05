"""macOS `utun` point-to-point tunnel interface.

Uses the public `PF_SYSTEM` / `com.apple.net.utun_control` kernel control
socket API (the same mechanism every VPN client on macOS uses), so no
entitlements or kernel extensions are needed. Creating the interface still
needs root, since `utun` device creation is privileged.
"""

from __future__ import annotations

import fcntl
import logging
import os
import select
import socket
import struct

_log = logging.getLogger(__name__)

UTUN_CONTROL_NAME = "com.apple.net.utun_control"
UTUN_OPT_IFNAME = 2

AF_INET = 2
AF_INET6 = 30

# Darwin sys/sys/kern_control.h: struct ctl_info { u_int32_t ctl_id; char ctl_name[96]; }
_CTLIOCGINFO = 0xC0644E03
_CTL_INFO_FMT = "I96s"
_MAX_KCTL_NAME = 96


def encode_af(packet: bytes) -> bytes:
    """Prepend the 4-byte big-endian address-family header utun expects."""
    if not packet:
        raise ValueError("empty packet")
    version = packet[0] >> 4
    if version == 4:
        af = AF_INET
    elif version == 6:
        af = AF_INET6
    else:
        raise ValueError(f"unrecognised IP version: {version}")
    return struct.pack(">I", af) + packet


def decode_af(buf: bytes) -> tuple[int, bytes]:
    """Split a utun read buffer into ``(address_family, packet)``."""
    if len(buf) < 4:
        raise ValueError(f"buffer too short for AF header: {len(buf)} bytes")
    (af,) = struct.unpack(">I", buf[:4])
    return af, buf[4:]


def _resolve_ctl_id(sock: socket.socket, name: str) -> int:
    """Resolve a kernel control name to its ctl_id via CTLIOCGINFO."""
    name_bytes = name.encode() + b"\x00"
    if len(name_bytes) > _MAX_KCTL_NAME:
        raise ValueError(f"control name too long: {name!r}")
    info = struct.pack(_CTL_INFO_FMT, 0, name_bytes.ljust(_MAX_KCTL_NAME, b"\x00"))
    info = fcntl.ioctl(sock.fileno(), _CTLIOCGINFO, info)
    ctl_id, _name = struct.unpack(_CTL_INFO_FMT, info)
    return ctl_id


# SO_SNDBUF size the async bridge asks the kernel for (see tune_buffers).
DEFAULT_SOCKET_BUFFER = 1 << 20


def tune_socket_buffers(sock: socket.socket, size: int = DEFAULT_SOCKET_BUFFER) -> dict[str, tuple[int | None, int | None]]:
    """Log ``sock``'s SO_RCVBUF/SO_SNDBUF at DEBUG, try to raise SO_SNDBUF
    to ``size`` (never lowering it), and log the result. Returns
    ``{"rcvbuf": (before, after), "sndbuf": (before, after)}``; a value is
    None where the option couldn't be read. Never raises: a utun
    kernel-control socket may refuse (or clamp) the request, which only
    costs buffering headroom.

    Only SO_SNDBUF (our inbound writes into the utun) is raised, so a burst
    from the modem doesn't hit ENOBUFS. SO_RCVBUF (the outbound queue the
    kernel holds for us to read) stays at the system default: a deep queue
    there only adds upload latency (bufferbloat).
    """
    result: dict[str, tuple[int | None, int | None]] = {}
    for label, opt, raise_it in (("rcvbuf", socket.SO_RCVBUF, False), ("sndbuf", socket.SO_SNDBUF, True)):
        before = after = None
        try:
            before = after = sock.getsockopt(socket.SOL_SOCKET, opt)
            if raise_it and before < size:
                try:
                    sock.setsockopt(socket.SOL_SOCKET, opt, size)
                except OSError as exc:
                    _log.debug("utun SO_%s: could not raise %d to %d: %s", label.upper(), before, size, exc)
                after = sock.getsockopt(socket.SOL_SOCKET, opt)
        except OSError as exc:
            _log.debug("utun SO_%s: unavailable: %s", label.upper(), exc)
        result[label] = (before, after)
        if raise_it:
            _log.debug("utun SO_%s: %s -> %s (requested %d)", label.upper(), before, after, size)
        else:
            _log.debug("utun SO_%s: %s (system default, left unchanged)", label.upper(), before)
    return result


class Utun:
    """An open utun interface (a connected PF_SYSTEM/SYSPROTO_CONTROL socket)."""

    def __init__(self, sock: socket.socket, name: str) -> None:
        self._sock = sock
        self.name = name
        self._poller: select.poll | None = None  # set by set_read_wait()
        self._poll_timeout_ms = 0

    @classmethod
    def open(cls, unit: int | None = None) -> "Utun":
        """Create (or attach to) a utun interface.

        ``unit`` is the utunN suffix; 0 (the default) means "let the kernel
        auto-assign the next free unit". Requires root.
        """
        if os.geteuid() != 0:
            raise PermissionError("creating a utun interface requires root")

        sock = socket.socket(socket.PF_SYSTEM, socket.SOCK_DGRAM, socket.SYSPROTO_CONTROL)
        try:
            ctl_id = _resolve_ctl_id(sock, UTUN_CONTROL_NAME)
            sock.connect((ctl_id, 0 if unit is None else unit))
            name = sock.getsockopt(socket.SYSPROTO_CONTROL, UTUN_OPT_IFNAME, 16)
            name = name.split(b"\x00", 1)[0].decode()
        except Exception:
            sock.close()
            raise
        return cls(sock, name)

    def fileno(self) -> int:
        """Return the underlying socket's file descriptor (for select/poll)."""
        return self._sock.fileno()

    def settimeout(self, timeout: float | None) -> None:
        """Set a read timeout, so rx/tx threads can poll a stop flag. Leaves
        any ``set_read_wait()`` mode: read() is a plain socket recv again.
        """
        self._poller = None
        self._sock.settimeout(timeout)

    def set_read_wait(self, timeout: float) -> None:
        """Put the socket in non-blocking mode and make ``read()`` try a bare
        recv first, waiting up to ``timeout`` seconds (poll) only when
        nothing is queued. Cheaper per packet than a socket in timeout mode,
        whose recv() does a poll() before every read. The wait is bounded, so
        a caller still wakes up regularly to check its stop flag.
        """
        self._sock.settimeout(0.0)
        poller = select.poll()
        poller.register(self._sock.fileno(), select.POLLIN)
        self._poll_timeout_ms = max(1, int(timeout * 1000))
        self._poller = poller

    def tune_buffers(self, size: int = DEFAULT_SOCKET_BUFFER) -> dict[str, tuple[int | None, int | None]]:
        """Log and try to raise the socket buffers (see tune_socket_buffers)."""
        return tune_socket_buffers(self._sock, size)

    def read(self, size: int = 4096) -> bytes:
        """Read one packet (with its AF header stripped) from the tunnel.

        In ``set_read_wait()`` mode, raises ``socket.timeout`` if nothing
        arrived within the wait, like a socket in timeout mode does.
        """
        if self._poller is None:
            buf = self._sock.recv(size)
        else:
            try:
                buf = self._sock.recv(size)
            except BlockingIOError:
                if not self._poller.poll(self._poll_timeout_ms):
                    raise socket.timeout("timed out") from None
                try:
                    buf = self._sock.recv(size)
                except BlockingIOError:  # a spurious wakeup: report it as an empty wait
                    raise socket.timeout("timed out") from None
        _af, packet = decode_af(buf)
        return packet

    def write(self, packet: bytes) -> int:
        """Write one IP packet (AF header added) to the tunnel."""
        return self._sock.send(encode_af(packet))

    def write_nonblocking(self, packet: bytes) -> int:
        """Like ``write()``, but a single non-blocking attempt: raises
        ``BlockingIOError`` (or, as macOS datagram sockets report it,
        ``OSError`` ENOBUFS) immediately if the kernel send buffer is full,
        rather than blocking (Python's ``socket.send()`` can still block up
        to the configured timeout even in "non-blocking with timeout" mode).
        For callers that must never block, e.g. AsyncBridge's libusb event
        thread.

        With a timeout set (what the bridges do; ``set_read_wait()`` is
        timeout 0), Python has already put the fd in O_NONBLOCK mode, so a
        plain write() is one attempt. Without
        one, MSG_DONTWAIT makes just this send non-blocking: O_NONBLOCK is a
        property of the shared open file description (a dup'd fd too), so
        setting it would turn the blocking reads into EAGAIN errors.
        """
        data = encode_af(packet)
        if self._sock.gettimeout() is None:
            return self._sock.send(data, socket.MSG_DONTWAIT)
        return os.write(self._sock.fileno(), data)

    def close(self) -> None:
        """Close the underlying socket."""
        self._sock.close()

    def __enter__(self) -> "Utun":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()
