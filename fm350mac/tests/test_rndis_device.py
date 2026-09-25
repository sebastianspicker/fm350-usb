"""RndisDevice control-message matching/robustness tests, with a fake
RndisUsb standing in for the real USB transport. No hardware.
"""

import struct

import pytest

from fm350mac import rndis
from fm350mac.rndis_device import RndisDevice


class FakeRndisUsb:
    """Minimal fake standing in for usb_transport.RndisUsb's control API."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.sent: list[bytes] = []

    def send_encapsulated(self, msg: bytes) -> None:
        self.sent.append(msg)

    def wait_notify(self, timeout: int = 2000):
        return b"\x00" * 8  # pretend the notification always arrives

    def get_encapsulated(self, size: int = 4096) -> bytes:
        if self._responses:
            return self._responses.pop(0)
        return b""


def test_get_response_discards_stale_completion_and_answers_device_keepalive():
    mac = bytes.fromhex("000011121314")
    stale = struct.pack("<IIIIII", rndis.QUERY_CMPLT, 24, 99, 0, 0, 16)  # wrong request_id
    device_keepalive = struct.pack("<III", rndis.KEEPALIVE, 12, 55)
    real = struct.pack("<IIIIII", rndis.QUERY_CMPLT, 24 + len(mac), 1, 0, len(mac), 16) + mac

    fake = FakeRndisUsb([stale, device_keepalive, real])
    device = RndisDevice(fake)

    result = device.query(rndis.OID_802_3_CURRENT_ADDRESS)

    assert result == mac
    assert rndis.pack_keepalive_cmplt(55) in fake.sent


def test_get_response_times_out_when_nothing_matches():
    fake = FakeRndisUsb([])  # get_encapsulated always returns b""
    device = RndisDevice(fake)
    with pytest.raises(TimeoutError):
        device._get_response(request_id=1, expect_type=rndis.QUERY_CMPLT, timeout=50)
