"""usb_transport.RndisUsb.bulk_write short-packet padding test, with a fake
UsbDevice (no real USB access).
"""

from fm350mac.usb_transport import RndisUsb


class _FakeUsbDevice:
    def __init__(self):
        self.writes: list[bytes] = []

    def bulk_out(self, endpoint, data, timeout_ms=1000):
        self.writes.append(bytes(data))
        return len(data)


def _make_rndis_usb(wMaxPacketSize: int) -> RndisUsb:
    """Build an RndisUsb without going through __init__ (which needs a real device)."""
    usb_dev = RndisUsb.__new__(RndisUsb)
    usb_dev.usb_device = _FakeUsbDevice()
    usb_dev.ep_bulk_out = 0x01
    usb_dev.bulk_out_max_packet = wMaxPacketSize
    return usb_dev


def test_bulk_write_pads_exact_multiple_of_max_packet_size():
    usb_dev = _make_rndis_usb(1024)
    usb_dev.bulk_write(b"\x01" * 1024)
    assert usb_dev.usb_device.writes[-1] == b"\x01" * 1024 + b"\x00"


def test_bulk_write_does_not_pad_short_packet():
    usb_dev = _make_rndis_usb(1024)
    usb_dev.bulk_write(b"\x01" * 1023)
    assert usb_dev.usb_device.writes[-1] == b"\x01" * 1023
