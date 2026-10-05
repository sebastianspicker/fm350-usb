"""RNDIS (Remote NDIS) message codec.

Pure encode/decode of the little-endian RNDIS control and data messages used
by the FM350-GL over its USB CDC-like control/data interfaces. No I/O here;
see ``usb_transport.py`` and ``rndis_device.py`` for the device side.

Message layout reference (all fields little-endian ``u32``):

Control message header (12 bytes): ``MessageType``, ``MessageLength``,
``RequestID``.

``REMOTE_NDIS_INITIALIZE_MSG`` body: ``MajorVersion``, ``MinorVersion``,
``MaxTransferSize``.

``REMOTE_NDIS_INITIALIZE_CMPLT`` body: ``Status``, ``MajorVersion``,
``MinorVersion``, ``DeviceFlags``, ``Medium``, ``MaxPacketsPerTransfer``,
``MaxTransferSize``, ``PacketAlignmentFactor``, two reserved ``u32``.

``REMOTE_NDIS_QUERY_MSG`` / ``REMOTE_NDIS_SET_MSG`` body: ``Oid``,
``InformationBufferLength``, ``InformationBufferOffset`` (relative to the
start of the ``RequestID`` field, byte 8 of the message; the standard value
is 20), ``DeviceVcHandle`` (always 0 here), followed by the buffer.

``REMOTE_NDIS_QUERY_CMPLT`` body: ``Status``, ``InformationBufferLength``,
``InformationBufferOffset`` (again relative to byte 8), followed by the
buffer.

``REMOTE_NDIS_SET_CMPLT`` body: ``Status``.

``REMOTE_NDIS_KEEPALIVE_CMPLT`` body: ``Status``.

``REMOTE_NDIS_INDICATE_STATUS_MSG`` body: ``Status``, ``StatusBufferLength``,
``StatusBufferOffset`` (relative to byte 8), followed by an optional
diagnostic buffer.

``REMOTE_NDIS_PACKET_MSG`` (44-byte header): ``MessageType``,
``MessageLength``, ``DataOffset`` (relative to byte 8; standard 36),
``DataLength``, ``OOBDataOffset``, ``OOBDataLength``, ``NumOOBDataElements``,
``PerPacketInfoOffset``, ``PerPacketInfoLength``, ``VcHandle``, ``Reserved``,
followed by the Ethernet frame.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

# Message types (control channel).
PACKET = 0x00000001
INIT = 0x00000002
INIT_CMPLT = 0x80000002
HALT = 0x00000003
QUERY = 0x00000004
QUERY_CMPLT = 0x80000004
SET = 0x00000005
SET_CMPLT = 0x80000005
RESET = 0x00000006
RESET_CMPLT = 0x80000006
INDICATE_STATUS = 0x00000007
KEEPALIVE = 0x00000008
KEEPALIVE_CMPLT = 0x80000008

# OIDs.
OID_GEN_MAXIMUM_FRAME_SIZE = 0x00010106
OID_GEN_LINK_SPEED = 0x00010107
OID_GEN_CURRENT_PACKET_FILTER = 0x0001010E
OID_GEN_MAXIMUM_TOTAL_SIZE = 0x00010111
OID_GEN_MEDIA_CONNECT_STATUS = 0x00010114
OID_802_3_PERMANENT_ADDRESS = 0x01010101
OID_802_3_CURRENT_ADDRESS = 0x01010102

# Default OID_GEN_CURRENT_PACKET_FILTER value: directed | multicast | broadcast.
PACKET_FILTER_DEFAULT = 0x0B

# InformationBufferOffset / DataOffset / StatusBufferOffset are relative to
# the start of the RequestID field, i.e. byte 8 of the message.
_INFO_BUFFER_OFFSET = 20
_PACKET_DATA_OFFSET = 36
_PACKET_HEADER_LEN = 44


# Synthetic status used when we reject a message ourselves (too short, or
# offsets/lengths pointing outside the buffer) rather than the device
# reporting an error. Modelled on NDIS_STATUS_INVALID_DATA; never sent by
# the FM350 itself.
STATUS_INVALID_DATA = 0xC0010015


class RndisError(Exception):
    """Raised when a device completion carries a non-zero Status, or a
    message is too short/malformed to parse safely.
    """

    def __init__(self, status: int, context: str = "") -> None:
        self.status = status
        message = f"RNDIS error status={status:#x}"
        if context:
            message = f"{message} ({context})"
        super().__init__(message)


def _require_len(msg: bytes, min_len: int, context: str) -> None:
    """Raise RndisError (never struct.error) if ``msg`` is shorter than ``min_len``."""
    if len(msg) < min_len:
        raise RndisError(STATUS_INVALID_DATA, f"{context}: message too short ({len(msg)} < {min_len} bytes)")


@dataclass
class InitCmplt:
    """Parsed REMOTE_NDIS_INITIALIZE_CMPLT."""

    request_id: int
    status: int
    major: int
    minor: int
    device_flags: int
    medium: int
    max_packets_per_transfer: int
    max_transfer_size: int
    packet_alignment_factor: int


@dataclass
class QueryCmplt:
    """Parsed REMOTE_NDIS_QUERY_CMPLT."""

    request_id: int
    status: int
    buffer: bytes = field(repr=False)


@dataclass
class SetCmplt:
    """Parsed REMOTE_NDIS_SET_CMPLT."""

    request_id: int
    status: int


@dataclass
class KeepaliveCmplt:
    """Parsed REMOTE_NDIS_KEEPALIVE_CMPLT."""

    request_id: int
    status: int


@dataclass
class IndicateStatus:
    """Parsed REMOTE_NDIS_INDICATE_STATUS_MSG."""

    status: int
    buffer: bytes = field(repr=False)


def _header(msg_type: int, body: bytes, request_id: int) -> bytes:
    """Pack a 12-byte control message header followed by ``body``."""
    length = 12 + len(body)
    return struct.pack("<III", msg_type, length, request_id) + body


def pack_init(request_id: int, max_transfer_size: int, major: int = 1, minor: int = 0) -> bytes:
    """Encode a REMOTE_NDIS_INITIALIZE_MSG."""
    body = struct.pack("<III", major, minor, max_transfer_size)
    return _header(INIT, body, request_id)


def pack_halt(request_id: int) -> bytes:
    """Encode a REMOTE_NDIS_HALT_MSG (no body)."""
    return _header(HALT, b"", request_id)


def pack_query(request_id: int, oid: int) -> bytes:
    """Encode a REMOTE_NDIS_QUERY_MSG with an empty (zero-length) input buffer."""
    body = struct.pack("<IIII", oid, 0, _INFO_BUFFER_OFFSET, 0)
    return _header(QUERY, body, request_id)


def pack_set(request_id: int, oid: int, value: bytes) -> bytes:
    """Encode a REMOTE_NDIS_SET_MSG carrying ``value`` as the input buffer."""
    body = struct.pack("<IIII", oid, len(value), _INFO_BUFFER_OFFSET, 0) + value
    return _header(SET, body, request_id)


def pack_keepalive(request_id: int) -> bytes:
    """Encode a host-initiated REMOTE_NDIS_KEEPALIVE_MSG (no body)."""
    return _header(KEEPALIVE, b"", request_id)


def pack_keepalive_cmplt(request_id: int, status: int = 0) -> bytes:
    """Encode a REMOTE_NDIS_KEEPALIVE_CMPLT in response to a device KEEPALIVE_MSG."""
    body = struct.pack("<I", status)
    return _header(KEEPALIVE_CMPLT, body, request_id)


def parse_init_cmplt(msg: bytes) -> InitCmplt:
    """Decode a REMOTE_NDIS_INITIALIZE_CMPLT. Raises RndisError on non-zero
    status, or if ``msg`` is too short to parse.
    """
    _require_len(msg, 44, "INIT_CMPLT")
    (
        msg_type,
        _length,
        request_id,
        status,
        major,
        minor,
        device_flags,
        medium,
        max_packets_per_transfer,
        max_transfer_size,
        packet_alignment_factor,
    ) = struct.unpack_from("<IIIIIIIIIII", msg)
    if msg_type != INIT_CMPLT:
        raise RndisError(status, f"unexpected message type {msg_type:#x} for INIT_CMPLT")
    if status != 0:
        raise RndisError(status, "INIT_CMPLT")
    return InitCmplt(
        request_id=request_id,
        status=status,
        major=major,
        minor=minor,
        device_flags=device_flags,
        medium=medium,
        max_packets_per_transfer=max_packets_per_transfer,
        max_transfer_size=max_transfer_size,
        packet_alignment_factor=packet_alignment_factor,
    )


def parse_query_cmplt(msg: bytes) -> QueryCmplt:
    """Decode a REMOTE_NDIS_QUERY_CMPLT. Raises RndisError on non-zero status,
    a too-short message, or a buffer offset/length outside ``msg``.
    """
    _require_len(msg, 24, "QUERY_CMPLT")
    msg_type, _length, request_id, status, buf_len, buf_offset = struct.unpack_from("<IIIIII", msg)
    if msg_type != QUERY_CMPLT:
        raise RndisError(status, f"unexpected message type {msg_type:#x} for QUERY_CMPLT")
    if status != 0:
        raise RndisError(status, "QUERY_CMPLT")
    start = 8 + buf_offset
    end = start + buf_len
    if start < 8 or end > len(msg):
        raise RndisError(STATUS_INVALID_DATA, "QUERY_CMPLT: buffer offset/length out of range")
    return QueryCmplt(request_id=request_id, status=status, buffer=bytes(msg[start:end]))


def parse_set_cmplt(msg: bytes) -> SetCmplt:
    """Decode a REMOTE_NDIS_SET_CMPLT. Raises RndisError on non-zero status
    or a too-short message.
    """
    _require_len(msg, 16, "SET_CMPLT")
    msg_type, _length, request_id, status = struct.unpack_from("<IIII", msg)
    if msg_type != SET_CMPLT:
        raise RndisError(status, f"unexpected message type {msg_type:#x} for SET_CMPLT")
    if status != 0:
        raise RndisError(status, "SET_CMPLT")
    return SetCmplt(request_id=request_id, status=status)


def parse_keepalive(msg: bytes) -> int:
    """Decode a device REMOTE_NDIS_KEEPALIVE_MSG and return its RequestID.
    Raises RndisError on a too-short message.
    """
    _require_len(msg, 12, "KEEPALIVE")
    _msg_type, _length, request_id = struct.unpack_from("<III", msg)
    return request_id


def parse_keepalive_cmplt(msg: bytes) -> KeepaliveCmplt:
    """Decode a REMOTE_NDIS_KEEPALIVE_CMPLT (the device's answer to our own
    keepalive). Unlike the other ``parse_*_cmplt`` helpers a non-zero Status
    is returned, not raised: the caller decides what a failed keepalive
    means. Raises RndisError on a too-short message.
    """
    _require_len(msg, 16, "KEEPALIVE_CMPLT")
    _msg_type, _length, request_id, status = struct.unpack_from("<IIII", msg)
    return KeepaliveCmplt(request_id=request_id, status=status)


def parse_indicate_status(msg: bytes) -> IndicateStatus:
    """Decode a REMOTE_NDIS_INDICATE_STATUS_MSG (no RequestID field). Raises
    RndisError on a too-short message or a buffer offset/length outside ``msg``.
    """
    _require_len(msg, 20, "INDICATE_STATUS")
    _msg_type, _length, status, buf_len, buf_offset = struct.unpack_from("<IIIII", msg)
    if not buf_len:
        return IndicateStatus(status=status, buffer=b"")
    start = 8 + buf_offset
    end = start + buf_len
    if start < 8 or end > len(msg):
        raise RndisError(STATUS_INVALID_DATA, "INDICATE_STATUS: buffer offset/length out of range")
    return IndicateStatus(status=status, buffer=bytes(msg[start:end]))


def peek_request_id(msg: bytes) -> int:
    """Peek at the RequestID field (byte 8) of a control message, without
    fully parsing it. Raises RndisError on a too-short message.
    """
    _require_len(msg, 12, "peek_request_id")
    return struct.unpack_from("<I", msg, 8)[0]


def message_type(msg: bytes) -> int:
    """Peek at the MessageType of any control message without fully parsing
    it. Raises RndisError (never struct.error) on a too-short message.
    """
    _require_len(msg, 4, "message_type")
    return struct.unpack_from("<I", msg)[0]


def pack_packet(frame: bytes) -> bytes:
    """Wrap an Ethernet ``frame`` in a REMOTE_NDIS_PACKET_MSG."""
    msg_length = _PACKET_HEADER_LEN + len(frame)
    header = struct.pack(
        "<IIIIIIIIIII",
        PACKET,
        msg_length,
        _PACKET_DATA_OFFSET,
        len(frame),
        0,  # OOBDataOffset
        0,  # OOBDataLength
        0,  # NumOOBDataElements
        0,  # PerPacketInfoOffset
        0,  # PerPacketInfoLength
        0,  # VcHandle
        0,  # Reserved
    )
    return header + frame


def unpack_packets_counted(buf: bytes, alignment_factor: int = 0) -> tuple[list[bytes], int]:
    """Split a bulk-IN buffer into the Ethernet frames carried by concatenated
    REMOTE_NDIS_PACKET_MSGs, and count malformed trailing data.

    The device may batch several PACKET_MSGs in one transfer and pad the
    remainder of the transfer with zero bytes (that padding is not counted).
    ``alignment_factor`` is INIT_CMPLT's PacketAlignmentFactor, an exponent:
    each PACKET_MSG starts on a ``2**alignment_factor``-byte boundary relative
    to the start of the transfer. Returns ``(frames, malformed)`` where
    ``malformed`` is 1 if parsing stopped early on non-padding garbage
    (unknown message type, truncated/inconsistent lengths), else 0. Frames
    parsed before the malformed part are still returned.
    """
    align = 1 << min(max(alignment_factor, 0), 12)
    frames: list[bytes] = []
    pos = 0
    n = len(buf)
    while pos + _PACKET_HEADER_LEN <= n:
        msg_type = struct.unpack_from("<I", buf, pos)[0]
        if msg_type == 0:
            # Zero padding -- but only if *everything* from here on is zero;
            # a zero word followed by garbage is a malformed trailer. count()
            # scans in C without copying the tail (any(buf[pos:]) did both).
            return frames, int(buf.count(0, pos) != n - pos)
        if msg_type != PACKET:
            return frames, 1  # malformed trailer
        msg_length, data_offset, data_length = struct.unpack_from("<III", buf, pos + 4)
        if msg_length < _PACKET_HEADER_LEN or pos + msg_length > n:
            return frames, 1  # truncated/malformed
        data_start = pos + 8 + data_offset
        data_end = data_start + data_length
        if data_start < pos or data_end > pos + msg_length or data_end < data_start:
            return frames, 1  # malformed offsets
        frames.append(bytes(buf[data_start:data_end]))
        pos += msg_length
        pos = (pos + align - 1) & ~(align - 1)
    # Leftover bytes shorter than a header: padding if all zero, else malformed.
    if any(buf[pos:]):
        return frames, 1
    return frames, 0


def unpack_packets(buf: bytes, alignment_factor: int = 0) -> list[bytes]:
    """Like ``unpack_packets_counted`` but returns only the frames; malformed
    or truncated trailers are ignored rather than raised.
    """
    return unpack_packets_counted(buf, alignment_factor)[0]


def is_empty_response(msg: bytes) -> bool:
    """True if a GET_ENCAPSULATED_RESPONSE reply is the RNDIS-spec "no
    response available" answer (a single 0x00 byte; any reply too short to
    hold a message header counts), i.e. nothing is pending.
    """
    return len(msg) < 8
