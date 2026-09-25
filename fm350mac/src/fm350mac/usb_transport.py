"""libusb transport for the FM350-GL's RNDIS control and data interfaces,
built on our own ctypes binding (usb_async.py) rather than pyusb.

RNDIS control messages ride the CDC "encapsulated command/response" control
transfers on interface 0; a device notification arrives on the interrupt IN
endpoint; Ethernet frames go over the bulk endpoints on interface 1.
"""

from __future__ import annotations

import logging

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

_log = logging.getLogger(__name__)


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
        """Wait for the RESPONSE_AVAILABLE interrupt notification (8 bytes).

        Returns None on timeout instead of raising, since the device
        sometimes skips the notification and the response can still be
        polled for directly.
        """
        try:
            return self.usb_device.interrupt_in(self.ep_interrupt, 8, timeout_ms=timeout)
        except UsbTimeout:
            return None

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
