"""utun AF-header framing tests (utun.py). Pure functions, no socket/root needed."""

import pytest

from fm350mac import utun


def test_encode_af_ipv4():
    packet = bytes([0x45]) + b"\x00" * 19
    framed = utun.encode_af(packet)
    assert framed[:4] == utun.AF_INET.to_bytes(4, "big")
    assert framed[4:] == packet


def test_encode_af_ipv6():
    packet = bytes([0x60]) + b"\x00" * 39
    framed = utun.encode_af(packet)
    assert framed[:4] == utun.AF_INET6.to_bytes(4, "big")
    assert framed[4:] == packet


def test_encode_af_rejects_unknown_version():
    with pytest.raises(ValueError):
        utun.encode_af(bytes([0x00]))


def test_encode_af_rejects_empty_packet():
    with pytest.raises(ValueError):
        utun.encode_af(b"")


def test_decode_af_round_trip_ipv4():
    packet = bytes([0x45]) + b"\xab" * 19
    framed = utun.encode_af(packet)
    af, decoded = utun.decode_af(framed)
    assert af == utun.AF_INET
    assert decoded == packet


def test_decode_af_round_trip_ipv6():
    packet = bytes([0x60]) + b"\xcd" * 39
    framed = utun.encode_af(packet)
    af, decoded = utun.decode_af(framed)
    assert af == utun.AF_INET6
    assert decoded == packet


def test_decode_af_rejects_short_buffer():
    with pytest.raises(ValueError):
        utun.decode_af(b"\x00\x00\x00")
