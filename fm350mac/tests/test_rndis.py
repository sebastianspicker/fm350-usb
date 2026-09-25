"""Byte-exact and round-trip tests for the RNDIS codec (rndis.py). No I/O, no hardware."""

import struct

import pytest

from fm350mac import rndis


def test_pack_init_byte_exact():
    msg = rndis.pack_init(request_id=1, max_transfer_size=0x4000)
    expected = struct.pack("<III", 0x2, 24, 1) + struct.pack("<III", 1, 0, 0x4000)
    assert msg == expected


def test_pack_query_byte_exact():
    msg = rndis.pack_query(request_id=7, oid=rndis.OID_802_3_CURRENT_ADDRESS)
    expected = struct.pack("<III", 0x4, 12 + 16, 7) + struct.pack(
        "<IIII", rndis.OID_802_3_CURRENT_ADDRESS, 0, 20, 0
    )
    assert msg == expected


def test_pack_set_byte_exact():
    value = (0x0B).to_bytes(4, "little")
    msg = rndis.pack_set(request_id=3, oid=rndis.OID_GEN_CURRENT_PACKET_FILTER, value=value)
    expected = (
        struct.pack("<III", 0x5, 12 + 16 + 4, 3)
        + struct.pack("<IIII", rndis.OID_GEN_CURRENT_PACKET_FILTER, 4, 20, 0)
        + value
    )
    assert msg == expected


def test_pack_halt_byte_exact():
    msg = rndis.pack_halt(request_id=99)
    expected = struct.pack("<III", 0x3, 12, 99)
    assert msg == expected


def test_parse_init_cmplt_measured_values():
    """Real values measured against the FM350-GL (see docs/macos-driver.md):
    status 0, v1.0, flags 1, medium 0, max_packets_per_transfer 1,
    max_transfer_size 2048, packet_alignment_factor 3.
    """
    msg = struct.pack(
        "<IIIIIIIIIII",
        rndis.INIT_CMPLT, 52, 42,
        0, 1, 0, 1, 0, 1, 2048, 3,
    ) + struct.pack("<II", 0, 0)  # two reserved u32s
    result = rndis.parse_init_cmplt(msg)
    assert result.request_id == 42
    assert result.status == 0
    assert result.major == 1
    assert result.minor == 0
    assert result.device_flags == 1
    assert result.medium == 0
    assert result.max_packets_per_transfer == 1
    assert result.max_transfer_size == 2048
    assert result.packet_alignment_factor == 3


def test_parse_init_cmplt_raises_on_error_status():
    msg = struct.pack("<IIIIIIIIIII", rndis.INIT_CMPLT, 52, 1, 1, 1, 0, 1, 0, 1, 2048, 3) + struct.pack(
        "<II", 0, 0
    )
    with pytest.raises(rndis.RndisError):
        rndis.parse_init_cmplt(msg)


def test_parse_query_cmplt():
    mac = bytes.fromhex("000011121314")  # fixed/fake device MAC (see docs)
    header = struct.pack("<IIIIII", rndis.QUERY_CMPLT, 24 + len(mac), 5, 0, len(mac), 16)
    result = rndis.parse_query_cmplt(header + mac)
    assert result.request_id == 5
    assert result.status == 0
    assert result.buffer == mac


def test_parse_query_cmplt_raises_on_error_status():
    header = struct.pack("<IIIIII", rndis.QUERY_CMPLT, 24, 5, 1, 0, 16)
    with pytest.raises(rndis.RndisError):
        rndis.parse_query_cmplt(header)


def test_parse_set_cmplt():
    msg = struct.pack("<IIII", rndis.SET_CMPLT, 16, 9, 0)
    result = rndis.parse_set_cmplt(msg)
    assert result.request_id == 9
    assert result.status == 0


def test_pack_unpack_packet_round_trip():
    frame = b"hello world, this is an ethernet frame payload"
    msg = rndis.pack_packet(frame)
    assert len(msg) == 44 + len(frame)
    frames = rndis.unpack_packets(msg)
    assert frames == [frame]


def test_unpack_packets_multiple_concatenated_with_padding():
    frame1 = b"A" * 20
    frame2 = b"B" * 30
    buf = rndis.pack_packet(frame1) + rndis.pack_packet(frame2) + b"\x00" * 16
    frames = rndis.unpack_packets(buf)
    assert frames == [frame1, frame2]


def test_unpack_packets_ignores_malformed_trailer():
    frame = b"C" * 10
    buf = rndis.pack_packet(frame) + b"\x01\x02\x03"  # too short to be a header
    frames = rndis.unpack_packets(buf)
    assert frames == [frame]


def test_unpack_packets_empty_buffer():
    assert rndis.unpack_packets(b"") == []


# --- Short-buffer robustness: every parse_* (and message_type) must raise
# RndisError, never struct.error, on truncated input. ------------------------


@pytest.mark.parametrize("size", [0, 3])
def test_message_type_raises_on_short_buffer(size):
    with pytest.raises(rndis.RndisError):
        rndis.message_type(b"\x00" * size)


def test_message_type_ok_at_minimum_length():
    assert rndis.message_type(struct.pack("<I", rndis.KEEPALIVE)) == rndis.KEEPALIVE


@pytest.mark.parametrize("size", [0, 3, 11])
def test_parse_keepalive_raises_on_short_buffer(size):
    with pytest.raises(rndis.RndisError):
        rndis.parse_keepalive(b"\x00" * size)


@pytest.mark.parametrize("size", [0, 3, 11])
def test_peek_request_id_raises_on_short_buffer(size):
    with pytest.raises(rndis.RndisError):
        rndis.peek_request_id(b"\x00" * size)


@pytest.mark.parametrize("size", [0, 3, 11])
def test_parse_indicate_status_raises_on_short_buffer(size):
    with pytest.raises(rndis.RndisError):
        rndis.parse_indicate_status(b"\x00" * size)


@pytest.mark.parametrize("size", [0, 3, 11])
def test_parse_init_cmplt_raises_on_short_buffer(size):
    with pytest.raises(rndis.RndisError):
        rndis.parse_init_cmplt(b"\x00" * size)


@pytest.mark.parametrize("size", [0, 3, 11])
def test_parse_query_cmplt_raises_on_short_buffer(size):
    with pytest.raises(rndis.RndisError):
        rndis.parse_query_cmplt(b"\x00" * size)


@pytest.mark.parametrize("size", [0, 3, 11])
def test_parse_set_cmplt_raises_on_short_buffer(size):
    with pytest.raises(rndis.RndisError):
        rndis.parse_set_cmplt(b"\x00" * size)


def test_parse_query_cmplt_raises_on_out_of_range_buffer_offset():
    # buf_len/buf_offset point past the end of the (otherwise valid-length) message.
    header = struct.pack("<IIIIII", rndis.QUERY_CMPLT, 24, 5, 0, 100, 16)
    with pytest.raises(rndis.RndisError):
        rndis.parse_query_cmplt(header)
