"""macOS `utun` point-to-point tunnel interface.

Uses the public `PF_SYSTEM` / `com.apple.net.utun_control` kernel control
socket API (the same mechanism every VPN client on macOS uses), so no
entitlements or kernel extensions are needed. Creating the interface still
needs root, since `utun` device creation is privileged.
"""

from __future__ import annotations

import fcntl
import os
import socket
import struct

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


class Utun:
    """An open utun interface (a connected PF_SYSTEM/SYSPROTO_CONTROL socket)."""

    def __init__(self, sock: socket.socket, name: str) -> None:
        self._sock = sock
        self.name = name

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
        """Set a read timeout, so rx/tx threads can poll a stop flag."""
        self._sock.settimeout(timeout)

    def read(self, size: int = 4096) -> bytes:
        """Read one packet (with its AF header stripped) from the tunnel."""
        buf = self._sock.recv(size)
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

        With a timeout set (what the bridges do), Python has already put the
        fd in O_NONBLOCK mode, so a plain write() is one attempt. Without
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
