"""libusb transport for the FM350-GL's RNDIS control and data interfaces,
built on our own ctypes binding (usb_async.py) rather than pyusb.

RNDIS control messages ride the CDC "encapsulated command/response" control
transfers on interface 0; a device notification arrives on the interrupt IN
endpoint; Ethernet frames go over the bulk endpoints on interface 1.
"""

from __future__ import annotations

import logging
import threading
import time

from .usb_async import PIDS, VID, UsbDevice, UsbTimeout, open_device

CONTROL_IFACE = 0
DATA_IFACE = 1

# Expected endpoint addresses, discovered from descriptors (see docs/macos-driver.md).
INTERRUPT_IN_DEFAULT = 0x82
BULK_IN_DEFAULT = 0x81
BULK_OUT_DEFAULT = 0x01

_SEND_ENCAPSULATED_COMMAND = 0x00
_GET_ENCAPSULATED_RESPONSE = 0x01
_REQTYPE_HOST_TO_DEVICE = 0x21  # class, interface, host->device
_REQTYPE_DEVICE_TO_HOST = 0xA1  # class, interface, device->host

_NOTIFY_READ_SIZE = 16  # RESPONSE_AVAILABLE is 8 bytes, CONNECTION_SPEED_CHANGE 16

_log = logging.getLogger(__name__)


class RateLimiter:
    """``allow()`` is True at most once per ``interval_s`` (thread-safe);
    for rate-limiting log lines that could otherwise fire per packet.
    """

    def __init__(self, interval_s: float) -> None:
        self._interval_s = interval_s
        self._last = float("-inf")
        self._lock = threading.Lock()

    def allow(self) -> bool:
        now = time.monotonic()
        with self._lock:
            if now - self._last >= self._interval_s:
                self._last = now
                return True
            return False


def mark_usb_unsafe(rndis_usb, reason: str) -> None:
    """Tell the underlying UsbDevice (if ``rndis_usb`` has one) not to close
    its handle: a bridge thread that wouldn't exit may still be inside a call
    on it.
    """
    device = getattr(rndis_usb, "usb_device", None)
    mark = getattr(device, "mark_unsafe_to_close", None)
    if mark is not None:
        mark(reason)


def is_response_available(data: bytes) -> bool:
    """True if ``data`` is a RESPONSE_AVAILABLE notification (at least 8 bytes).

    Two encodings exist: the RNDIS spec's own (ULONG Notification = 1, ULONG
    Reserved = 0, i.e. ``01 00 00 00 00 00 00 00`` -- what the FM350 actually
    sends, seen on hardware 2026-10-05) and the CDC class-request form
    (bmRequestType 0xA1, bNotificationCode 0x01). Accept both.
    """
    if len(data) < 8:
        return False
    if data[:4] == b"\x01\x00\x00\x00":
        return True
    return data[0] == 0xA1 and data[1] == 0x01


def find_device() -> UsbDevice:
    """Find and open the FM350-GL RNDIS USB device (0e8d:7126/7127).

    Returns the shared per-process UsbDevice handle (see
    usb_async.open_device()); raises UsbNoDevice if not found.
    """
    return open_device(VID, PIDS)


class RndisUsb:
    """Claims the RNDIS control (0) and data (1) interfaces on a shared
    UsbDevice and does the I/O.

    Use as a context manager to guarantee the interfaces are released:

        with RndisUsb(find_device()) as usb_dev:
            ...

    ``close()`` only releases interfaces 0/1; it does not close the
    underlying UsbDevice, which may still be claimed by an AtPort sharing
    the same handle (see usb_async.open_device()).
    """

    def __init__(self, usb_device: UsbDevice) -> None:
        self.usb_device = usb_device
        usb_device.claim_interface(CONTROL_IFACE)
        usb_device.claim_interface(DATA_IFACE)
        self._claimed = True

        self.ep_interrupt, _ = usb_device.find_endpoint(CONTROL_IFACE, "in", "interrupt")
        self.ep_bulk_in, _ = usb_device.find_endpoint(DATA_IFACE, "in", "bulk")
        self.ep_bulk_out, self.bulk_out_max_packet = usb_device.find_endpoint(DATA_IFACE, "out", "bulk")
        _log.debug(
            "RNDIS endpoints: interrupt=%#04x bulk_in=%#04x bulk_out=%#04x",
            self.ep_interrupt, self.ep_bulk_in, self.ep_bulk_out,
        )

    def send_encapsulated(self, msg: bytes) -> None:
        """SEND_ENCAPSULATED_COMMAND: deliver an RNDIS control message to the device."""
        self.usb_device.control_out(_REQTYPE_HOST_TO_DEVICE, _SEND_ENCAPSULATED_COMMAND, 0, CONTROL_IFACE, msg)

    def get_encapsulated(self, size: int = 4096) -> bytes:
        """GET_ENCAPSULATED_RESPONSE: fetch a pending RNDIS control response."""
        return self.usb_device.control_in(_REQTYPE_DEVICE_TO_HOST, _GET_ENCAPSULATED_RESPONSE, 0, CONTROL_IFACE, size)

    def wait_notify(self, timeout: int = 2000) -> bytes | None:
        """Wait for a RESPONSE_AVAILABLE interrupt notification (8 bytes).

        Other notifications (e.g. the 16-byte CONNECTION_SPEED_CHANGE) are
        logged and skipped, still within the overall ``timeout``. Returns
        None on timeout instead of raising, since the device sometimes skips
        the notification and the response can still be polled for directly.
        Each read takes up to 16 bytes so a longer notification can't overflow.
        """
        deadline = time.monotonic() + timeout / 1000
        while True:
            remaining_ms = int((deadline - time.monotonic()) * 1000)
            if remaining_ms <= 0:
                return None
            try:
                data = self.usb_device.interrupt_in(self.ep_interrupt, _NOTIFY_READ_SIZE, timeout_ms=remaining_ms)
            except UsbTimeout:
                return None
            if is_response_available(data):
                return data
            _log.debug("ignoring non-RESPONSE_AVAILABLE notification: %s", data.hex())

    def bulk_read(self, size: int, timeout: int = 1000) -> bytes:
        """Read up to ``size`` bytes from the RNDIS data bulk IN endpoint."""
        return self.usb_device.bulk_in(self.ep_bulk_in, size, timeout_ms=timeout)

    def bulk_write(self, data: bytes, timeout: int = 1000) -> int:
        """Write ``data`` to the RNDIS data bulk OUT endpoint.

        If ``data`` is an exact multiple of the endpoint's max packet size,
        USB would end the transfer on a short packet, but the device is
        still expecting more; pad with one zero byte so the transfer ends on
        a short packet immediately. This matches Linux's usbnet/rndis_host
        (see drivers/net/usb/usbnet.c): RNDIS trusts MessageLength inside the
        PACKET_MSG, not the USB transfer length, so the extra byte is ignored.
        """
        if len(data) % self.bulk_out_max_packet == 0:
            data = data + b"\x00"
        return self.usb_device.bulk_out(self.ep_bulk_out, data, timeout_ms=timeout)

    def close(self) -> None:
        """Release the claimed interfaces (not the shared UsbDevice)."""
        if not self._claimed:
            return
        self._claimed = False
        for iface in (CONTROL_IFACE, DATA_IFACE):
            self.usb_device.release_interface(iface)

    def __enter__(self) -> "RndisUsb":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()
