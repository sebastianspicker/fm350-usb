"""AsyncBridge tests, with the fake Libusb from test_usb_async_pool.py and
fake RndisUsb/utun stand-ins. No real hardware.
"""

from __future__ import annotations

import ctypes
import queue
import threading
import time

from test_usb_async_pool import FakeLibusb  # noqa: I001 (local test helper, kept after package imports)

from fm350mac import ethernet, rndis
from fm350mac import usb_async as ua
from fm350mac.async_bridge import AsyncBridge

OUR_MAC = bytes.fromhex("001122334455")
OUR_IP = bytes([10, 0, 0, 1])


def _wait_until(predicate, timeout=1.0, interval=0.005) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


class _FakeUsbDeviceHandle:
    """Just enough of usb_async.UsbDevice for AsyncBridge to wire up its
    AsyncEndpoint pools/EventLoop against a fake Libusb.
    """

    def __init__(self, libusb: FakeLibusb) -> None:
        self.libusb = libusb
        self.handle = None  # ctypes accepts None as NULL for any pointer field
        self.ctx = None


class FakeAsyncRndisUsb:
    """Stands in for usb_transport.RndisUsb: endpoint addresses/max-packet
    for pool wiring, plus the sync control calls the control thread uses.
    """

    def __init__(self, libusb: FakeLibusb) -> None:
        self.usb_device = _FakeUsbDeviceHandle(libusb)
        self.ep_bulk_in = 0x81
        self.ep_bulk_out = 0x01
        self.ep_interrupt = 0x82
        self.bulk_out_max_packet = 1024
        self.sent: list[bytes] = []

    def send_encapsulated(self, msg: bytes) -> None:
        self.sent.append(bytes(msg))

    def get_encapsulated(self, size: int = 4096) -> bytes:
        return b""


class FakeUtun:
    """Queue-backed stand-in for utun.Utun: push() feeds the tx thread."""

    def __init__(self, raise_on_write: Exception | None = None) -> None:
        self.writes: list[bytes] = []
        self._queue: queue.Queue = queue.Queue()
        self._raise_on_write = raise_on_write

    def settimeout(self, timeout: float) -> None:
        pass

    def push(self, payload: bytes) -> None:
        self._queue.put(payload)

    def read(self) -> bytes:
        try:
            return self._queue.get(timeout=0.05)
        except queue.Empty:
            raise TimeoutError("no data") from None

    def write(self, data: bytes) -> int:
        self.writes.append(bytes(data))
        return len(data)

    def write_nonblocking(self, data: bytes) -> int:
        if self._raise_on_write is not None:
            raise self._raise_on_write
        return self.write(data)

    def close(self) -> None:
        pass


def _retire_pending_transfers(bridge: AsyncBridge) -> None:
    """Simulate libusb delivering CANCELLED for whatever's currently in
    flight on every pool -- used to let bridge.stop() finish promptly
    against a fake Libusb that never completes transfers on its own.
    """
    for pool in (bridge._rx_pool, bridge._tx_pool, bridge._control_pool):
        for slot in list(pool._slots):
            if slot.in_flight:
                FakeLibusb.fire(slot.transfer, ua.LIBUSB_TRANSFER_CANCELLED)


def _stop_after_draining(bridge: AsyncBridge) -> bool:
    """bridge.stop() with a background helper that retires whatever's in
    flight once cancellation has been requested, so it returns promptly.
    """

    def _drain() -> None:
        assert _wait_until(lambda: bridge._rx_pool._stopping)
        _retire_pending_transfers(bridge)

    t = threading.Thread(target=_drain, daemon=True)
    t.start()
    stopped = bridge.stop()
    t.join(timeout=1.0)
    return stopped


def _ip_frame(byte: int, src_mac: bytes = bytes.fromhex("aabbccddeeff")) -> tuple[bytes, bytes]:
    """Returns (payload, packed RNDIS PACKET_MSG) for a minimal IPv4 frame."""
    payload = bytes([0x45, byte]) + b"\x00" * 18
    frame = ethernet.wrap(payload, ethernet.ETH_P_IP, dst=OUR_MAC, src=src_mac)
    return payload, rndis.pack_packet(frame)


def test_rx_order_is_preserved():
    lib = FakeLibusb()
    rndis_usb = FakeAsyncRndisUsb(lib)
    utun = FakeUtun()
    bridge = AsyncBridge(rndis_usb, utun, OUR_MAC, OUR_IP, max_transfer_size=0x4000, rx_urbs=2, tx_urbs=2)
    bridge.start()
    try:
        assert len(lib.submitted) == 3  # 2 rx slots + 1 control slot, submitted synchronously by start()
        rx_transfers = lib.submitted[:2]

        payloads_and_msgs = [_ip_frame(i) for i in range(3)]
        # Fire completions out of slot order but in a specific delivery
        # order; the same transfer pointer is reused once it's resubmitted.
        for i, transfer in enumerate([rx_transfers[0], rx_transfers[1], rx_transfers[0]]):
            _payload, msg = payloads_and_msgs[i]
            lib.fire(transfer, ua.LIBUSB_TRANSFER_COMPLETED, actual_length=len(msg), data=msg)

        assert utun.writes == [p for p, _ in payloads_and_msgs]
    finally:
        assert _stop_after_draining(bridge)


def test_arp_request_is_answered_on_the_tx_pool():
    lib = FakeLibusb()
    rndis_usb = FakeAsyncRndisUsb(lib)
    utun = FakeUtun()
    bridge = AsyncBridge(rndis_usb, utun, OUR_MAC, OUR_IP, max_transfer_size=0x4000, rx_urbs=1, tx_urbs=1)
    bridge.start()
    try:
        requester_mac = bytes.fromhex("aabbccddeeff")
        requester_ip = bytes([10, 0, 0, 5])
        arp_payload = ethernet.pack_arp(
            ethernet.ARP_REQUEST, sha=requester_mac, spa=requester_ip, tha=b"\x00" * 6, tpa=OUR_IP
        )
        frame = ethernet.wrap(arp_payload, ethernet.ETH_P_ARP, dst=OUR_MAC, src=requester_mac)
        msg = rndis.pack_packet(frame)

        rx_transfer = lib.submitted[0]
        lib.fire(rx_transfer, ua.LIBUSB_TRANSFER_COMPLETED, actual_length=len(msg), data=msg)

        assert _wait_until(lambda: len(lib.submitted) >= 3)  # rx resubmit + control(1) + tx submit
        tx_transfer = lib.submitted[-1]
        sent = ctypes.string_at(tx_transfer.contents.buffer, tx_transfer.contents.length)
        reply_frames = rndis.unpack_packets(sent)
        assert len(reply_frames) == 1
        ethertype, src_mac, _payload = ethernet.strip(reply_frames[0])
        assert ethertype == ethernet.ETH_P_ARP
        assert src_mac == OUR_MAC
    finally:
        assert _stop_after_draining(bridge)


def test_tx_pool_exhausted_drops_and_counts_a_stall():
    lib = FakeLibusb()
    rndis_usb = FakeAsyncRndisUsb(lib)
    utun = FakeUtun()
    bridge = AsyncBridge(rndis_usb, utun, OUR_MAC, OUR_IP, max_transfer_size=0x4000, rx_urbs=1, tx_urbs=2)
    bridge.start()
    try:
        for i in range(4):
            utun.push(_ip_frame(i)[0])
        assert _wait_until(lambda: bridge.stats.tx_stalls >= 1), "tx pool exhaustion was never counted"
    finally:
        assert _stop_after_draining(bridge)

    assert bridge.stats.drops >= 1
    # The two OUT slots stayed in flight the whole time (nothing in this
    # fake ever completes a submitted OUT transfer), so nothing after the
    # first two pushes could have been accepted.
    assert bridge.stats.tx_packets == 0


def test_rx_write_never_blocks_and_counts_drops_on_blockingioerror():
    """AsyncBridge must never block the libusb event thread on a utun write
    (see utun.Utun.write_nonblocking()): a full send buffer must show up as
    a counted drop, not a block.
    """
    lib = FakeLibusb()
    rndis_usb = FakeAsyncRndisUsb(lib)
    utun = FakeUtun(raise_on_write=BlockingIOError())
    bridge = AsyncBridge(rndis_usb, utun, OUR_MAC, OUR_IP, max_transfer_size=0x4000, rx_urbs=1, tx_urbs=1)
    bridge.start()
    try:
        _payload, msg = _ip_frame(0)
        rx_transfer = lib.submitted[0]

        start = time.monotonic()
        lib.fire(rx_transfer, ua.LIBUSB_TRANSFER_COMPLETED, actual_length=len(msg), data=msg)
        elapsed = time.monotonic() - start

        assert elapsed < 0.1, f"firing the RX completion took {elapsed * 1000:.1f} ms -- utun write blocked"
        assert bridge.stats.drops >= 1
        assert utun.writes == []  # write_nonblocking() raised before recording anything
    finally:
        assert _stop_after_draining(bridge)


def test_stop_joins_both_threads_and_drains_the_pools():
    lib = FakeLibusb()
    rndis_usb = FakeAsyncRndisUsb(lib)
    utun = FakeUtun()
    bridge = AsyncBridge(rndis_usb, utun, OUR_MAC, OUR_IP, max_transfer_size=0x4000, rx_urbs=2, tx_urbs=2)
    bridge.start()

    stopped = _stop_after_draining(bridge)

    assert stopped is True
    assert not bridge._tx_thread.is_alive()
    assert not bridge._control_thread.is_alive()
    assert lib.freed  # EventLoop.stop() only frees once every pool is fully retired


def test_stats_increments_from_two_threads_are_never_lost():
    """stats.* (and _tx_stalled) are touched from both the tx thread and the
    EventLoop thread; a bare += would be able to lose updates.
    """
    lib = FakeLibusb()
    rndis_usb = FakeAsyncRndisUsb(lib)
    utun = FakeUtun()
    bridge = AsyncBridge(rndis_usb, utun, OUR_MAC, OUR_IP, max_transfer_size=0x4000, rx_urbs=1, tx_urbs=1)

    n_threads = 8
    per_thread = 500

    def hammer():
        for _ in range(per_thread):
            bridge._incr_stat("drops")

    threads = [threading.Thread(target=hammer) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5.0)

    assert bridge.stats.drops == n_threads * per_thread
