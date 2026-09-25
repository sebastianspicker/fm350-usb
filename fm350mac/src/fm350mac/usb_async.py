"""Our own ctypes binding to libusb-1.0, including its asynchronous
transfer API. Replaces pyusb everywhere in this package.

See docs/macos-driver.md, "Async USB I/O: our own ctypes binding to
libusb", for the design this implements. stdlib only (ctypes).

Layout: ``Libusb`` declares argtypes/restype for every libusb function used
and exposes thin, snake_cased convenience methods a fake can mimic in
tests. ``LibusbTransfer`` mirrors ``struct libusb_transfer`` field by field
(checked against a compiled C program in tests/test_usb_async_layout.py).
``UsbDevice`` is one open handle per process, shared by every interface
claimed on it (see ``open_device()``), with sync helpers that map libusb
errors to typed exceptions. ``AsyncEndpoint`` is a pool of pre-allocated
transfers/buffers for one endpoint, with the lifetime rules described in
the doc: never free a buffer or the shared callback while a transfer might
still complete into it. ``EventLoop`` runs
``libusb_handle_events_timeout_completed`` on one thread, so every
callback (and hence every IN completion) is delivered in order.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import logging
import threading
import time
from dataclasses import dataclass

_log = logging.getLogger(__name__)

VID = 0x0E8D
PIDS = (0x7127, 0x7126)  # RNDIS product ids (AT interface differs: 6 or 4)

# --- libusb error codes (enum libusb_error) --------------------------------

LIBUSB_SUCCESS = 0
LIBUSB_ERROR_IO = -1
LIBUSB_ERROR_INVALID_PARAM = -2
LIBUSB_ERROR_ACCESS = -3
LIBUSB_ERROR_NO_DEVICE = -4
LIBUSB_ERROR_NOT_FOUND = -5
LIBUSB_ERROR_BUSY = -6
LIBUSB_ERROR_TIMEOUT = -7
LIBUSB_ERROR_OVERFLOW = -8
LIBUSB_ERROR_PIPE = -9
LIBUSB_ERROR_INTERRUPTED = -10
LIBUSB_ERROR_NO_MEM = -11
LIBUSB_ERROR_NOT_SUPPORTED = -12
LIBUSB_ERROR_OTHER = -99

# --- enum libusb_transfer_status -------------------------------------------

LIBUSB_TRANSFER_COMPLETED = 0
LIBUSB_TRANSFER_ERROR = 1
LIBUSB_TRANSFER_TIMED_OUT = 2
LIBUSB_TRANSFER_CANCELLED = 3
LIBUSB_TRANSFER_STALL = 4
LIBUSB_TRANSFER_NO_DEVICE = 5
LIBUSB_TRANSFER_OVERFLOW = 6

# --- enum libusb_transfer_type (only the two kinds we use) -----------------

LIBUSB_TRANSFER_TYPE_BULK = 2
LIBUSB_TRANSFER_TYPE_INTERRUPT = 3

_TRANSFER_TYPE_CODES = {"bulk": LIBUSB_TRANSFER_TYPE_BULK, "interrupt": LIBUSB_TRANSFER_TYPE_INTERRUPT}

_MAX_CONSECUTIVE_ERRORS = 8  # ERROR/OVERFLOW completions before a pool goes fatal


# --- typed exceptions -------------------------------------------------------


class UsbError(Exception):
    """A libusb error, identified by its numeric code and libusb's name for it."""

    def __init__(self, code: int, name: str | None = None, context: str = "") -> None:
        self.code = code
        self.name = name or f"LIBUSB_ERROR({code})"
        message = f"{self.name} ({code})"
        if context:
            message = f"{context}: {message}"
        super().__init__(message)


class UsbTimeout(UsbError):
    """LIBUSB_ERROR_TIMEOUT: the transfer didn't complete in time."""


class UsbNoDevice(UsbError):
    """LIBUSB_ERROR_NO_DEVICE: the device was disconnected."""


class UsbPipeError(UsbError):
    """LIBUSB_ERROR_PIPE: a stalled/halted endpoint."""


_ERROR_CLASSES = {
    LIBUSB_ERROR_TIMEOUT: UsbTimeout,
    LIBUSB_ERROR_NO_DEVICE: UsbNoDevice,
    LIBUSB_ERROR_PIPE: UsbPipeError,
}


def _raise_for_code(libusb: "Libusb", code: int, context: str = "") -> None:
    """Raise the appropriate typed exception if ``code`` is a libusb error
    (negative). A no-op for success (>= 0, which for libusb_control_transfer
    is the number of bytes transferred).
    """
    if code >= 0:
        return
    name = libusb.error_name(code)
    exc_cls = _ERROR_CLASSES.get(code, UsbError)
    raise exc_cls(code, name, context)


def _clamped_bytes(buf: ctypes.Array, n: int) -> bytes:
    """Copy ``n`` bytes out of ``buf`` (a fixed-size ctypes byte array),
    clamped to ``[0, len(buf)]``. ``n`` (an actual/transferred length
    reported by libusb, ultimately controlled by the device/driver) is
    never trusted past the buffer's real size -- a misbehaving or
    compromised device reporting more bytes than it was given room for
    must not cause an out-of-bounds read.
    """
    n = max(0, min(n, len(buf)))
    return ctypes.string_at(ctypes.addressof(buf), n)


# --- ctypes structures mirroring libusb.h -----------------------------------
# (field layout checked against a compiled C program: see
# tests/test_usb_async_layout.py)


class _LibusbContext(ctypes.Structure):
    pass


class _LibusbDevice(ctypes.Structure):
    pass


class _LibusbDeviceHandle(ctypes.Structure):
    pass


libusb_context_p = ctypes.POINTER(_LibusbContext)
libusb_device_p = ctypes.POINTER(_LibusbDevice)
libusb_device_handle_p = ctypes.POINTER(_LibusbDeviceHandle)


class LibusbDeviceDescriptor(ctypes.Structure):
    _fields_ = [
        ("bLength", ctypes.c_uint8),
        ("bDescriptorType", ctypes.c_uint8),
        ("bcdUSB", ctypes.c_uint16),
        ("bDeviceClass", ctypes.c_uint8),
        ("bDeviceSubClass", ctypes.c_uint8),
        ("bDeviceProtocol", ctypes.c_uint8),
        ("bMaxPacketSize0", ctypes.c_uint8),
        ("idVendor", ctypes.c_uint16),
        ("idProduct", ctypes.c_uint16),
        ("bcdDevice", ctypes.c_uint16),
        ("iManufacturer", ctypes.c_uint8),
        ("iProduct", ctypes.c_uint8),
        ("iSerialNumber", ctypes.c_uint8),
        ("bNumConfigurations", ctypes.c_uint8),
    ]


class LibusbEndpointDescriptor(ctypes.Structure):
    _fields_ = [
        ("bLength", ctypes.c_uint8),
        ("bDescriptorType", ctypes.c_uint8),
        ("bEndpointAddress", ctypes.c_uint8),
        ("bmAttributes", ctypes.c_uint8),
        ("wMaxPacketSize", ctypes.c_uint16),
        ("bInterval", ctypes.c_uint8),
        ("bRefresh", ctypes.c_uint8),
        ("bSynchAddress", ctypes.c_uint8),
        ("extra", ctypes.c_void_p),
        ("extra_length", ctypes.c_int),
    ]


class LibusbInterfaceDescriptor(ctypes.Structure):
    _fields_ = [
        ("bLength", ctypes.c_uint8),
        ("bDescriptorType", ctypes.c_uint8),
        ("bInterfaceNumber", ctypes.c_uint8),
        ("bAlternateSetting", ctypes.c_uint8),
        ("bNumEndpoints", ctypes.c_uint8),
        ("bInterfaceClass", ctypes.c_uint8),
        ("bInterfaceSubClass", ctypes.c_uint8),
        ("bInterfaceProtocol", ctypes.c_uint8),
        ("iInterface", ctypes.c_uint8),
        ("endpoint", ctypes.POINTER(LibusbEndpointDescriptor)),
        ("extra", ctypes.c_void_p),
        ("extra_length", ctypes.c_int),
    ]


class LibusbInterface(ctypes.Structure):
    _fields_ = [
        ("altsetting", ctypes.POINTER(LibusbInterfaceDescriptor)),
        ("num_altsetting", ctypes.c_int),
    ]


class LibusbConfigDescriptor(ctypes.Structure):
    _fields_ = [
        ("bLength", ctypes.c_uint8),
        ("bDescriptorType", ctypes.c_uint8),
        ("wTotalLength", ctypes.c_uint16),
        ("bNumInterfaces", ctypes.c_uint8),
        ("bConfigurationValue", ctypes.c_uint8),
        ("iConfiguration", ctypes.c_uint8),
        ("bmAttributes", ctypes.c_uint8),
        ("MaxPower", ctypes.c_uint8),
        ("interface", ctypes.POINTER(LibusbInterface)),
        ("extra", ctypes.c_void_p),
        ("extra_length", ctypes.c_int),
    ]


class LibusbTransfer(ctypes.Structure):
    """Mirrors ``struct libusb_transfer``, minus the trailing flexible
    ``iso_packet_desc`` array (never used: no isochronous transfers here).
    ``libusb_fill_bulk_transfer``/``libusb_fill_interrupt_transfer`` are
    ``static inline`` in the header, so callers fill these fields directly.
    """


LibusbTransferCbFn = ctypes.CFUNCTYPE(None, ctypes.POINTER(LibusbTransfer))

LibusbTransfer._fields_ = [
    ("dev_handle", libusb_device_handle_p),
    ("flags", ctypes.c_uint8),
    ("endpoint", ctypes.c_uint8),
    ("type", ctypes.c_uint8),
    ("timeout", ctypes.c_uint),
    ("status", ctypes.c_int),  # enum libusb_transfer_status
    ("length", ctypes.c_int),
    ("actual_length", ctypes.c_int),
    ("callback", LibusbTransferCbFn),
    ("user_data", ctypes.c_void_p),
    ("buffer", ctypes.POINTER(ctypes.c_uint8)),
    ("num_iso_packets", ctypes.c_int),
]

libusb_transfer_p = ctypes.POINTER(LibusbTransfer)


class _Timeval(ctypes.Structure):
    _fields_ = [("tv_sec", ctypes.c_long), ("tv_usec", ctypes.c_int32)]


# --- library loading ---------------------------------------------------


def _load_cdll() -> ctypes.CDLL:
    """Load libusb-1.0, preferring the Homebrew build (same search order pyusb used)."""
    for path in ("/opt/homebrew/lib/libusb-1.0.dylib", "/usr/local/lib/libusb-1.0.dylib"):
        try:
            return ctypes.CDLL(path)
        except OSError:
            continue
    name = ctypes.util.find_library("usb-1.0")
    if name:
        return ctypes.CDLL(name)
    return ctypes.CDLL("libusb-1.0.dylib")  # last resort; raises OSError if truly not found


class Libusb:
    """ctypes wrapper around libusb-1.0. Declares argtypes/restype for every
    function used, and exposes a snake_case convenience method per function
    so tests can substitute a fake with matching method names.
    """

    def __init__(self, lib: ctypes.CDLL | None = None) -> None:
        self.lib = lib if lib is not None else _load_cdll()
        self._bind()

    def _bind(self) -> None:
        lib = self.lib
        pp_ctx = ctypes.POINTER(libusb_context_p)

        if hasattr(lib, "libusb_init_context"):
            lib.libusb_init_context.argtypes = [pp_ctx, ctypes.c_void_p, ctypes.c_int]
            lib.libusb_init_context.restype = ctypes.c_int
        lib.libusb_init.argtypes = [pp_ctx]
        lib.libusb_init.restype = ctypes.c_int
        lib.libusb_exit.argtypes = [libusb_context_p]
        lib.libusb_exit.restype = None

        lib.libusb_get_device_list.argtypes = [libusb_context_p, ctypes.POINTER(ctypes.POINTER(libusb_device_p))]
        lib.libusb_get_device_list.restype = ctypes.c_ssize_t
        lib.libusb_free_device_list.argtypes = [ctypes.POINTER(libusb_device_p), ctypes.c_int]
        lib.libusb_free_device_list.restype = None
        lib.libusb_get_device_descriptor.argtypes = [libusb_device_p, ctypes.POINTER(LibusbDeviceDescriptor)]
        lib.libusb_get_device_descriptor.restype = ctypes.c_int

        lib.libusb_open.argtypes = [libusb_device_p, ctypes.POINTER(libusb_device_handle_p)]
        lib.libusb_open.restype = ctypes.c_int
        lib.libusb_close.argtypes = [libusb_device_handle_p]
        lib.libusb_close.restype = None
        lib.libusb_get_device.argtypes = [libusb_device_handle_p]
        lib.libusb_get_device.restype = libusb_device_p

        lib.libusb_get_active_config_descriptor.argtypes = [
            libusb_device_p, ctypes.POINTER(ctypes.POINTER(LibusbConfigDescriptor))
        ]
        lib.libusb_get_active_config_descriptor.restype = ctypes.c_int
        lib.libusb_free_config_descriptor.argtypes = [ctypes.POINTER(LibusbConfigDescriptor)]
        lib.libusb_free_config_descriptor.restype = None

        lib.libusb_claim_interface.argtypes = [libusb_device_handle_p, ctypes.c_int]
        lib.libusb_claim_interface.restype = ctypes.c_int
        lib.libusb_release_interface.argtypes = [libusb_device_handle_p, ctypes.c_int]
        lib.libusb_release_interface.restype = ctypes.c_int
        lib.libusb_clear_halt.argtypes = [libusb_device_handle_p, ctypes.c_ubyte]
        lib.libusb_clear_halt.restype = ctypes.c_int
        lib.libusb_reset_device.argtypes = [libusb_device_handle_p]
        lib.libusb_reset_device.restype = ctypes.c_int

        lib.libusb_control_transfer.argtypes = [
            libusb_device_handle_p, ctypes.c_uint8, ctypes.c_uint8, ctypes.c_uint16, ctypes.c_uint16,
            ctypes.POINTER(ctypes.c_uint8), ctypes.c_uint16, ctypes.c_uint,
        ]
        lib.libusb_control_transfer.restype = ctypes.c_int
        lib.libusb_bulk_transfer.argtypes = [
            libusb_device_handle_p, ctypes.c_ubyte, ctypes.POINTER(ctypes.c_uint8), ctypes.c_int,
            ctypes.POINTER(ctypes.c_int), ctypes.c_uint,
        ]
        lib.libusb_bulk_transfer.restype = ctypes.c_int
        lib.libusb_interrupt_transfer.argtypes = [
            libusb_device_handle_p, ctypes.c_ubyte, ctypes.POINTER(ctypes.c_uint8), ctypes.c_int,
            ctypes.POINTER(ctypes.c_int), ctypes.c_uint,
        ]
        lib.libusb_interrupt_transfer.restype = ctypes.c_int

        lib.libusb_alloc_transfer.argtypes = [ctypes.c_int]
        lib.libusb_alloc_transfer.restype = libusb_transfer_p
        lib.libusb_free_transfer.argtypes = [libusb_transfer_p]
        lib.libusb_free_transfer.restype = None
        lib.libusb_submit_transfer.argtypes = [libusb_transfer_p]
        lib.libusb_submit_transfer.restype = ctypes.c_int
        lib.libusb_cancel_transfer.argtypes = [libusb_transfer_p]
        lib.libusb_cancel_transfer.restype = ctypes.c_int

        lib.libusb_handle_events_timeout_completed.argtypes = [
            libusb_context_p, ctypes.POINTER(_Timeval), ctypes.POINTER(ctypes.c_int)
        ]
        lib.libusb_handle_events_timeout_completed.restype = ctypes.c_int

        lib.libusb_error_name.argtypes = [ctypes.c_int]
        lib.libusb_error_name.restype = ctypes.c_char_p
        lib.libusb_get_version.argtypes = []
        lib.libusb_get_version.restype = ctypes.c_void_p  # struct libusb_version*; only used for logging

        lib.libusb_get_bus_number.argtypes = [libusb_device_p]
        lib.libusb_get_bus_number.restype = ctypes.c_uint8
        lib.libusb_get_port_numbers.argtypes = [libusb_device_p, ctypes.POINTER(ctypes.c_uint8), ctypes.c_int]
        lib.libusb_get_port_numbers.restype = ctypes.c_int

    # --- convenience methods (real calls; a fake Libusb mimics these) ------

    def init_context(self):
        ctx = libusb_context_p()
        if hasattr(self.lib, "libusb_init_context"):
            code = self.lib.libusb_init_context(ctypes.byref(ctx), None, 0)
        else:
            code = self.lib.libusb_init(ctypes.byref(ctx))
        _raise_for_code(self, code, "libusb_init")
        return ctx

    def exit(self, ctx) -> None:
        self.lib.libusb_exit(ctx)

    def get_device_list(self, ctx):
        list_pp = ctypes.POINTER(libusb_device_p)()
        count = self.lib.libusb_get_device_list(ctx, ctypes.byref(list_pp))
        if count < 0:
            _raise_for_code(self, count, "libusb_get_device_list")
        return list_pp, count

    def free_device_list(self, list_pp, unref_devices: int = 1) -> None:
        self.lib.libusb_free_device_list(list_pp, unref_devices)

    def get_device_descriptor(self, dev) -> LibusbDeviceDescriptor:
        desc = LibusbDeviceDescriptor()
        code = self.lib.libusb_get_device_descriptor(dev, ctypes.byref(desc))
        _raise_for_code(self, code, "libusb_get_device_descriptor")
        return desc

    def open(self, dev):
        handle = libusb_device_handle_p()
        code = self.lib.libusb_open(dev, ctypes.byref(handle))
        _raise_for_code(self, code, "libusb_open")
        return handle

    def close(self, handle) -> None:
        self.lib.libusb_close(handle)

    def get_device(self, handle):
        return self.lib.libusb_get_device(handle)

    def get_active_config_descriptor(self, dev):
        cfg_pp = ctypes.POINTER(LibusbConfigDescriptor)()
        code = self.lib.libusb_get_active_config_descriptor(dev, ctypes.byref(cfg_pp))
        _raise_for_code(self, code, "libusb_get_active_config_descriptor")
        return cfg_pp

    def free_config_descriptor(self, cfg_pp) -> None:
        self.lib.libusb_free_config_descriptor(cfg_pp)

    def claim_interface(self, handle, iface: int) -> int:
        return self.lib.libusb_claim_interface(handle, iface)

    def release_interface(self, handle, iface: int) -> int:
        return self.lib.libusb_release_interface(handle, iface)

    def clear_halt(self, handle, endpoint: int) -> int:
        return self.lib.libusb_clear_halt(handle, endpoint)

    def reset_device(self, handle) -> int:
        return self.lib.libusb_reset_device(handle)

    def control_transfer(self, handle, request_type, request, value, index, data_ptr, length, timeout_ms) -> int:
        return self.lib.libusb_control_transfer(handle, request_type, request, value, index, data_ptr, length, timeout_ms)

    def bulk_transfer(self, handle, endpoint, data_ptr, length, timeout_ms) -> tuple[int, int]:
        transferred = ctypes.c_int(0)
        code = self.lib.libusb_bulk_transfer(handle, endpoint, data_ptr, length, ctypes.byref(transferred), timeout_ms)
        return code, transferred.value

    def interrupt_transfer(self, handle, endpoint, data_ptr, length, timeout_ms) -> tuple[int, int]:
        transferred = ctypes.c_int(0)
        code = self.lib.libusb_interrupt_transfer(handle, endpoint, data_ptr, length, ctypes.byref(transferred), timeout_ms)
        return code, transferred.value

    def alloc_transfer(self, iso_packets: int = 0):
        transfer = self.lib.libusb_alloc_transfer(iso_packets)
        if not transfer:
            raise UsbError(LIBUSB_ERROR_NO_MEM, "LIBUSB_ERROR_NO_MEM", "libusb_alloc_transfer")
        return transfer

    def free_transfer(self, transfer) -> None:
        self.lib.libusb_free_transfer(transfer)

    def submit_transfer(self, transfer) -> int:
        return self.lib.libusb_submit_transfer(transfer)

    def cancel_transfer(self, transfer) -> int:
        return self.lib.libusb_cancel_transfer(transfer)

    def handle_events_timeout_completed(self, ctx, timeout_s: float) -> int:
        tv = _Timeval(tv_sec=int(timeout_s), tv_usec=int((timeout_s % 1.0) * 1_000_000))
        return self.lib.libusb_handle_events_timeout_completed(ctx, ctypes.byref(tv), None)

    def error_name(self, code: int) -> str:
        raw = self.lib.libusb_error_name(code)
        return raw.decode() if raw else f"LIBUSB_ERROR({code})"

    def get_bus_number(self, dev) -> int:
        return self.lib.libusb_get_bus_number(dev)

    def get_port_numbers(self, dev, max_ports: int = 8) -> tuple[int, ...]:
        buf = (ctypes.c_uint8 * max_ports)()
        n = self.lib.libusb_get_port_numbers(dev, buf, max_ports)
        if n < 0:
            _raise_for_code(self, n, "libusb_get_port_numbers")
        return tuple(buf[:n])


# --- leak registry -------------------------------------------------------
#
# When an EventLoop gives up waiting for in-flight transfers to retire (see
# EventLoop.stop()), the only safe thing left to do is never touch that
# context/handle again -- not even to free Python-side objects, since a
# transfer might still complete into freed memory. Everything reachable
# from that point (the EventLoop, its pools -- and hence their CFUNCTYPE
# callbacks, buffers and libusb_transfer structs -- and the UsbDevice) is
# kept referenced here forever so the GC can never collect it.

_leaked_lock = threading.Lock()
_LEAKED: list[object] = []


def _leak(*objects: object) -> None:
    with _leaked_lock:
        _LEAKED.extend(obj for obj in objects if obj is not None)


# --- shared-handle factory ---------------------------------------------

_shared_lock = threading.Lock()
_shared_device: "UsbDevice | None" = None


def open_device(vid: int = VID, pids: tuple[int, ...] = PIDS, *, libusb: "Libusb | None" = None) -> "UsbDevice":
    """Find and open the FM350-GL, returning a ``UsbDevice`` shared by every
    caller in this process for as long as it stays open.

    The RNDIS control/data interfaces (0/1) and the AT interface (6) must
    end up on the *same* libusb handle (see docs/macos-driver.md). Rather
    than thread a handle through every call site, this factory caches the
    last ``UsbDevice`` it opened and hands it out again as long as it's
    still open; ``UsbDevice.close()`` evicts itself from the cache, so a
    lost/re-enumerated device gets a fresh handle on the next call.
    """
    global _shared_device
    with _shared_lock:
        if _shared_device is not None and not _shared_device.closed:
            return _shared_device
        dev = UsbDevice._open(vid, pids, libusb)
        _shared_device = dev
        return dev


class UsbDevice:
    """One open libusb device handle, shared by every interface claimed on
    it (see ``open_device()``). Ctx-managed: releases any interfaces still
    claimed and closes the handle/context on ``close()``.
    """

    def __init__(self, libusb: Libusb, ctx, handle, pid: int) -> None:
        self._libusb = libusb
        self.ctx = ctx
        self.handle = handle
        self.pid = pid
        self.closed = False
        self._claimed: set[int] = set()
        self._unsafe_reason: str | None = None

    @property
    def unsafe_to_close(self) -> bool:
        """True once ``mark_unsafe_to_close()`` has been called: some
        in-flight USB transfer using this device's context was leaked
        (never freed) rather than risk a use-after-free, so ``close()`` is
        now permanently a no-op for this instance.
        """
        return self._unsafe_reason is not None

    def mark_unsafe_to_close(self, reason: str) -> None:
        """Record that this device's context may still be referenced by
        libusb from a transfer that was leaked rather than freed (see
        EventLoop.stop()). After this, ``close()`` never calls
        libusb_close/libusb_exit -- doing so while libusb might still write
        into that leaked memory would corrupt the process instead of just
        leaking it.
        """
        if self._unsafe_reason is None:
            self._unsafe_reason = reason
            _log.error("UsbDevice marked unsafe to close: %s", reason)
            global _shared_device
            with _shared_lock:
                if _shared_device is self:
                    _shared_device = None

    @property
    def libusb(self) -> Libusb:
        """The Libusb binding this device was opened through (needed to
        build an EventLoop/AsyncEndpoint sharing this device's context/handle).
        """
        return self._libusb

    @classmethod
    def _open(cls, vid: int, pids: tuple[int, ...], libusb: "Libusb | None") -> "UsbDevice":
        lib = libusb if libusb is not None else Libusb()
        ctx = lib.init_context()
        opened: UsbDevice | None = None
        try:
            list_pp, count = lib.get_device_list(ctx)
            try:
                for i in range(count):
                    dev = list_pp[i]
                    desc = lib.get_device_descriptor(dev)
                    if desc.idVendor == vid and desc.idProduct in pids:
                        handle = lib.open(dev)
                        opened = cls(lib, ctx, handle, desc.idProduct)
                        return opened
            finally:
                lib.free_device_list(list_pp, 1)
            raise UsbNoDevice(
                LIBUSB_ERROR_NOT_FOUND, "LIBUSB_ERROR_NOT_FOUND",
                f"FM350 not found (vid={vid:#06x}, pid in {[f'{p:#06x}' for p in pids]})",
            )
        finally:
            # Any exit from this function other than the happy-path return
            # above (device not found, or an exception from get_device_list/
            # get_device_descriptor/open) must not leak the freshly-created
            # libusb context.
            if opened is None:
                lib.exit(ctx)

    def claim_interface(self, iface: int) -> None:
        code = self._libusb.claim_interface(self.handle, iface)
        _raise_for_code(self._libusb, code, f"claim_interface({iface})")
        self._claimed.add(iface)

    def release_interface(self, iface: int) -> None:
        if iface not in self._claimed:
            return
        self._claimed.discard(iface)
        code = self._libusb.release_interface(self.handle, iface)
        if code != 0:
            _log.debug("release_interface(%d) failed: %s", iface, self._libusb.error_name(code))

    def port_path(self) -> tuple[int, tuple[int, ...]]:
        """Return ``(bus_number, port_numbers)`` identifying the physical
        USB port this device is plugged into, independent of the VID/PID it
        happened to enumerate with. Used to pin the modem's identity across
        a re-enumeration (see cli.py's ``up --supervise`` restart loop).
        """
        dev = self._libusb.get_device(self.handle)
        bus = self._libusb.get_bus_number(dev)
        ports = self._libusb.get_port_numbers(dev)
        return bus, ports

    def find_endpoint(self, iface: int, direction: str, transfer_type: str | None = None) -> tuple[int, int]:
        """Return ``(bEndpointAddress, wMaxPacketSize)`` for the first
        endpoint matching ``direction`` ("in"/"out") and, if given,
        ``transfer_type`` ("bulk"/"interrupt") on interface ``iface``'s
        first alt-setting -- discovered from the active config descriptor
        rather than hardcoded (see docs/macos-driver.md for expected values).
        """
        if direction not in ("in", "out"):
            raise ValueError(direction)
        dev = self._libusb.get_device(self.handle)
        cfg_pp = self._libusb.get_active_config_descriptor(dev)
        try:
            cfg = cfg_pp.contents
            for i in range(cfg.bNumInterfaces):
                usb_iface = cfg.interface[i]
                if usb_iface.num_altsetting <= 0:
                    continue
                alt = usb_iface.altsetting[0]
                if alt.bInterfaceNumber != iface:
                    continue
                for e in range(alt.bNumEndpoints):
                    ep = alt.endpoint[e]
                    ep_in = bool(ep.bEndpointAddress & 0x80)
                    if ep_in != (direction == "in"):
                        continue
                    if transfer_type is not None and (ep.bmAttributes & 0x03) != _TRANSFER_TYPE_CODES[transfer_type]:
                        continue
                    return ep.bEndpointAddress, ep.wMaxPacketSize
            raise UsbError(0, "NOT_FOUND", f"no {direction} endpoint on interface {iface}")
        finally:
            self._libusb.free_config_descriptor(cfg_pp)

    # --- sync I/O ------------------------------------------------------

    def control_out(self, request_type: int, request: int, value: int, index: int, data: bytes, timeout_ms: int = 1000) -> int:
        buf = (ctypes.c_uint8 * len(data)).from_buffer_copy(data) if data else (ctypes.c_uint8 * 1)()
        ptr = ctypes.cast(buf, ctypes.POINTER(ctypes.c_uint8))
        code = self._libusb.control_transfer(self.handle, request_type, request, value, index, ptr, len(data), timeout_ms)
        _raise_for_code(self._libusb, code, "control_transfer(out)")
        return code

    def control_in(self, request_type: int, request: int, value: int, index: int, length: int, timeout_ms: int = 1000) -> bytes:
        buf = (ctypes.c_uint8 * max(length, 1))()
        ptr = ctypes.cast(buf, ctypes.POINTER(ctypes.c_uint8))
        code = self._libusb.control_transfer(self.handle, request_type, request, value, index, ptr, length, timeout_ms)
        _raise_for_code(self._libusb, code, "control_transfer(in)")
        return _clamped_bytes(buf, code)

    def bulk_out(self, endpoint: int, data: bytes, timeout_ms: int = 1000) -> int:
        buf = (ctypes.c_uint8 * len(data)).from_buffer_copy(data) if data else (ctypes.c_uint8 * 1)()
        ptr = ctypes.cast(buf, ctypes.POINTER(ctypes.c_uint8))
        code, transferred = self._libusb.bulk_transfer(self.handle, endpoint, ptr, len(data), timeout_ms)
        _raise_for_code(self._libusb, code, f"bulk_transfer(out, ep={endpoint:#04x})")
        return transferred

    def bulk_in(self, endpoint: int, length: int, timeout_ms: int = 1000) -> bytes:
        buf = (ctypes.c_uint8 * max(length, 1))()
        ptr = ctypes.cast(buf, ctypes.POINTER(ctypes.c_uint8))
        code, transferred = self._libusb.bulk_transfer(self.handle, endpoint, ptr, length, timeout_ms)
        _raise_for_code(self._libusb, code, f"bulk_transfer(in, ep={endpoint:#04x})")
        return _clamped_bytes(buf, transferred)

    def interrupt_in(self, endpoint: int, length: int, timeout_ms: int = 1000) -> bytes:
        buf = (ctypes.c_uint8 * max(length, 1))()
        ptr = ctypes.cast(buf, ctypes.POINTER(ctypes.c_uint8))
        code, transferred = self._libusb.interrupt_transfer(self.handle, endpoint, ptr, length, timeout_ms)
        _raise_for_code(self._libusb, code, f"interrupt_transfer(in, ep={endpoint:#04x})")
        return _clamped_bytes(buf, transferred)

    def clear_halt(self, endpoint: int) -> None:
        code = self._libusb.clear_halt(self.handle, endpoint)
        _raise_for_code(self._libusb, code, f"clear_halt(ep={endpoint:#04x})")

    def reset(self) -> None:
        code = self._libusb.reset_device(self.handle)
        _raise_for_code(self._libusb, code, "reset_device")

    def close(self) -> None:
        if self.closed:
            return
        if self._unsafe_reason is not None:
            _log.error(
                "refusing to close UsbDevice (libusb_close/libusb_exit skipped): %s -- "
                "an in-flight USB transfer using this context was leaked earlier",
                self._unsafe_reason,
            )
            return
        self.closed = True
        for iface in list(self._claimed):
            self.release_interface(iface)
        self._libusb.close(self.handle)
        self._libusb.exit(self.ctx)
        global _shared_device
        with _shared_lock:
            if _shared_device is self:
                _shared_device = None

    def __enter__(self) -> "UsbDevice":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()


# --- async transfer pool -----------------------------------------------


@dataclass
class _Slot:
    buffer: ctypes.Array
    transfer: ctypes.pointer
    in_flight: bool = False
    retired: bool = True  # not submitted (yet)
    metadata: object = None  # caller-supplied, round-tripped to on_out_result


class AsyncEndpoint:
    """A pool of ``count`` pre-allocated transfers (+ buffers) for one bulk
    or interrupt endpoint.

    Lifetime rules (see docs/macos-driver.md -- getting this wrong crashes a
    root process): transfers, buffers and the single shared callback are
    allocated once in ``__init__`` and kept referenced until every transfer
    has reported a final status; ``free_all()`` must only be called once
    ``all_retired()`` is true. The callback never lets an exception escape
    into C.
    """

    def __init__(
        self,
        libusb: Libusb,
        dev_handle,
        endpoint: int,
        direction: str,
        transfer_type: str,
        count: int,
        buffer_size: int,
        timeout_ms: int,
        *,
        on_complete=None,
        on_out_result=None,
        on_fatal=None,
    ) -> None:
        if direction not in ("in", "out"):
            raise ValueError(direction)
        self._libusb = libusb
        self._dev_handle = dev_handle
        self.endpoint = endpoint
        self.direction = direction
        self._type_code = _TRANSFER_TYPE_CODES[transfer_type]
        self._buffer_size = buffer_size
        self._timeout_ms = timeout_ms
        self._on_complete = on_complete
        self._on_out_result = on_out_result
        self._on_fatal = on_fatal
        self._error_streak = 0
        self._stopping = False
        self._failed = False
        # Guards _stopping plus every slot's in_flight/retired flags and the
        # actual libusb_submit_transfer/cancel_transfer calls, so a submit
        # (from the tx thread or the event thread -- ARP replies come from
        # _handle_arp on the event thread, ordinary tx from the tx thread)
        # can never race a concurrent cancel_all() into leaving a transfer
        # in flight, or into double-submitting the same transfer.
        self._lock = threading.Lock()
        # Kept referenced for the pool's whole lifetime: freeing it (or
        # letting it be garbage collected) while a transfer is in flight
        # would leave libusb holding a dangling function pointer.
        self._callback = LibusbTransferCbFn(self._on_transfer_done)
        self._slots: list[_Slot] = []
        self._slots_by_addr: dict[int, _Slot] = {}
        for _ in range(count):
            buf = (ctypes.c_uint8 * buffer_size)()
            transfer = libusb.alloc_transfer(0)
            slot = _Slot(buffer=buf, transfer=transfer)
            self._slots.append(slot)
            self._slots_by_addr[ctypes.addressof(transfer.contents)] = slot

    @property
    def failed(self) -> bool:
        return self._failed

    def start(self) -> None:
        """IN pools only: submit every slot immediately so the pool starts full."""
        if self.direction != "in":
            return
        for slot in self._slots:
            self._submit(slot, self._buffer_size)

    def submit_out(self, data: bytes, metadata=None) -> bool:
        """Submit ``data`` on a free OUT slot. Returns False if none is free
        (all in flight -- the caller should count a drop/stall) or the pool
        is stopping.

        ``metadata`` (e.g. the caller's own idea of this write's length) is
        round-tripped back to ``on_out_result(ok, actual_length, metadata)``
        when this specific transfer retires -- slots are reused for later,
        unrelated submissions, so this can't be tracked on the pool itself.

        Called from more than one thread (the tx thread for ordinary
        traffic, the event thread for ARP replies): the free-slot scan, the
        buffer copy and the actual submit all happen under ``self._lock``,
        so two callers can never pick the same free slot.
        """
        if self.direction != "out":
            raise ValueError("submit_out() is only for OUT pools")
        if len(data) > self._buffer_size:
            raise ValueError(f"{len(data)} bytes > pool buffer size {self._buffer_size}")
        with self._lock:
            if self._stopping:
                return False
            target = None
            for slot in self._slots:
                if not slot.in_flight and slot.retired:
                    target = slot
                    break
            if target is None:
                return False
            target.metadata = metadata
            ctypes.memmove(target.buffer, data, len(data))
            code = self._submit_locked(target, len(data))
        self._report_submit_error(code)
        return code == 0

    def cancel_all(self) -> None:
        """Cancel every in-flight transfer. Their callbacks will each fire
        with CANCELLED (or, rarely, a status that raced the cancel).

        ``_stopping`` is set and the transfers are cancelled atomically
        (under ``self._lock``), so a concurrent submit/resubmit either
        completes first and gets cancelled right after, or sees
        ``_stopping`` already set and refuses -- either way, nothing can end
        up in flight after ``cancel_all()`` returns that isn't cancelled.
        """
        with self._lock:
            self._stopping = True
            to_cancel = [slot for slot in self._slots if slot.in_flight]
            for slot in to_cancel:
                self._libusb.cancel_transfer(slot.transfer)

    def all_retired(self) -> bool:
        return all(slot.retired for slot in self._slots)

    def free_all(self) -> None:
        """Free every transfer. Only safe once ``all_retired()`` is true."""
        for slot in self._slots:
            self._libusb.free_transfer(slot.transfer)
        self._slots = []
        self._slots_by_addr = {}

    # --- internals -------------------------------------------------------

    def _fill(self, slot: _Slot, length: int) -> None:
        t = slot.transfer.contents
        t.dev_handle = self._dev_handle
        t.endpoint = self.endpoint
        t.type = self._type_code
        t.timeout = self._timeout_ms
        t.length = length
        t.callback = self._callback
        t.user_data = None
        t.buffer = ctypes.cast(slot.buffer, ctypes.POINTER(ctypes.c_uint8))

    def _submit_locked(self, slot: _Slot, length: int) -> int:
        """Fill and submit ``slot``. Caller must hold ``self._lock``.

        Returns the libusb result code, or 0 without actually submitting if
        the pool is stopping (checked-then-acted atomically with
        cancel_all()/submit_out() under the same lock -- see the class
        docstring's lifetime rules).
        """
        if self._stopping:
            slot.in_flight = False
            slot.retired = True
            return 0
        self._fill(slot, length)
        slot.in_flight = True
        slot.retired = False
        code = self._libusb.submit_transfer(slot.transfer)
        if code != 0:
            slot.in_flight = False
            slot.retired = True
        return code

    def _submit(self, slot: _Slot, length: int) -> None:
        with self._lock:
            code = self._submit_locked(slot, length)
        self._report_submit_error(code)

    def _report_submit_error(self, code: int) -> None:
        if code == 0:
            return
        name = self._libusb.error_name(code)
        if code == LIBUSB_ERROR_NO_DEVICE:
            self._mark_fatal(f"submit_transfer: {name}", device_lost=True)
        else:
            _log.error("submit_transfer on endpoint %#04x failed: %s", self.endpoint, name)

    def _mark_fatal(self, reason: str, device_lost: bool = False) -> None:
        if not self._failed:
            _log.error("endpoint %#04x failed: %s", self.endpoint, reason)
        self._failed = True
        if self._on_fatal:
            try:
                self._on_fatal(reason, device_lost)
            except Exception:
                _log.exception("on_fatal callback raised")

    def _on_transfer_done(self, transfer_ptr) -> None:
        # Never let an exception propagate into C: it would corrupt libusb's
        # internal state (or the interpreter, depending on the ctypes
        # callback path) instead of just failing this pool.
        try:
            self._handle_completion(transfer_ptr)
        except Exception:
            _log.exception("callback crashed on endpoint %#04x", self.endpoint)
            self._mark_fatal("callback exception")

    def _handle_completion(self, transfer_ptr) -> None:
        slot = self._slots_by_addr.get(ctypes.addressof(transfer_ptr.contents))
        if slot is None:
            _log.error("completion for unknown transfer on endpoint %#04x", self.endpoint)
            return
        t = transfer_ptr.contents
        status = t.status
        actual_length = t.actual_length
        slot.in_flight = False

        if self._stopping:
            slot.retired = True
            return

        if status == LIBUSB_TRANSFER_COMPLETED:
            self._error_streak = 0
            if self.direction == "in":
                data = _clamped_bytes(slot.buffer, actual_length)
                self._submit(slot, self._buffer_size)
                if self._on_complete:
                    self._on_complete(data)
            else:
                slot.retired = True
                if self._on_out_result:
                    self._on_out_result(True, actual_length, slot.metadata)
            return

        if status == LIBUSB_TRANSFER_TIMED_OUT:
            if self.direction == "in":
                self._submit(slot, self._buffer_size)
            else:
                slot.retired = True
                if self._on_out_result:
                    self._on_out_result(False, actual_length, slot.metadata)
            return

        if status == LIBUSB_TRANSFER_CANCELLED:
            slot.retired = True
            return

        if status == LIBUSB_TRANSFER_NO_DEVICE:
            slot.retired = True
            self._mark_fatal("device disconnected", device_lost=True)
            return

        if status == LIBUSB_TRANSFER_STALL:
            # clear_halt is a sync libusb call; run it (and the resubmit) off
            # the event thread so a slow/blocked clear_halt never delays
            # every other transfer's completion.
            threading.Thread(target=self._handle_stall, args=(slot,), daemon=True).start()
            return

        # LIBUSB_TRANSFER_ERROR / LIBUSB_TRANSFER_OVERFLOW: count, resubmit
        # with backoff (off-thread, same reasoning as STALL), fatal after
        # too many in a row.
        self._error_streak += 1
        if self._error_streak >= _MAX_CONSECUTIVE_ERRORS:
            slot.retired = True
            self._mark_fatal(f"{self._error_streak} consecutive transfer errors (status={status})")
            return
        delay = min(0.01 * self._error_streak, 0.5)
        threading.Thread(target=self._resubmit_after, args=(slot, delay), daemon=True).start()

    def _handle_stall(self, slot: _Slot) -> None:
        try:
            self._libusb.clear_halt(self._dev_handle, self.endpoint)
        except Exception:
            _log.exception("clear_halt failed for endpoint %#04x", self.endpoint)
        # _submit()'s lock-guarded _stopping check is the single source of
        # truth for whether this resubmit actually happens (see cancel_all()
        # / _submit_locked()); a stale, unlocked check here would race it.
        self._submit(slot, self._buffer_size)

    def _resubmit_after(self, slot: _Slot, delay: float) -> None:
        time.sleep(delay)
        self._submit(slot, self._buffer_size)


class EventLoop:
    """Runs ``libusb_handle_events_timeout_completed`` on one thread, so
    every ``AsyncEndpoint`` callback (and hence RX order) is delivered in
    order, on that thread.
    """

    def __init__(self, libusb: Libusb, ctx, usb_device: "UsbDevice | None" = None, poll_interval_s: float = 0.1) -> None:
        self._libusb = libusb
        self._ctx = ctx
        self._usb_device = usb_device
        self._poll_interval_s = poll_interval_s
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._pools: list[AsyncEndpoint] = []

    def register(self, pool: AsyncEndpoint) -> None:
        self._pools.append(pool)

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="fm350mac-usb-events", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._libusb.handle_events_timeout_completed(self._ctx, self._poll_interval_s)
            except Exception:
                _log.exception("libusb event handling failed")
                time.sleep(0.01)

    def stop(self, drain_timeout_s: float = 2.0) -> bool:
        """Cancel every registered pool, then keep handling events (from
        this thread, after the background one has exited) until every pool
        reports all transfers retired, bounded by ``drain_timeout_s``.

        Returns True if every pool fully drained (safe to reuse the device
        handle afterwards, e.g. for an RNDIS HALT). Returns False, marks the
        associated UsbDevice (if any) unsafe to close, and moves this loop
        and its pools into the module-level leak registry if either the
        background thread refuses to exit, or the drain bound is hit --
        leaking rather than freeing (or closing the device) is the only way
        to avoid a use-after-free once libusb might still complete into
        memory we no longer control.
        """
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            if self._thread.is_alive():
                self._give_up("EventLoop background thread did not exit within 1.0s")
                return False
            self._thread = None

        for pool in self._pools:
            pool.cancel_all()

        deadline = time.monotonic() + drain_timeout_s
        while time.monotonic() < deadline and not all(p.all_retired() for p in self._pools):
            try:
                self._libusb.handle_events_timeout_completed(self._ctx, 0.05)
            except Exception:
                _log.exception("libusb event handling failed while draining")
                break

        if not all(p.all_retired() for p in self._pools):
            self._give_up(
                f"timed out after {drain_timeout_s:.1f}s waiting for in-flight USB transfers to retire"
            )
            return False
        for pool in self._pools:
            pool.free_all()
        return True

    def _give_up(self, reason: str) -> None:
        _log.error("%s; leaking rather than risking a use-after-free", reason)
        if self._usb_device is not None:
            self._usb_device.mark_unsafe_to_close(reason)
        _leak(self, self._usb_device, *self._pools)
