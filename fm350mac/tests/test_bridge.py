"""Bridge rx/tx/control thread tests, with fakes for RndisUsb and Utun. No hardware."""

import logging
import time

from fakes import FakeRndisUsb, FakeUtun

from fm350mac import ethernet, rndis
from fm350mac.bridge import Bridge
from fm350mac.usb_async import UsbNoDevice, UsbTimeout


def _wait_until(predicate, timeout=1.0, interval=0.01) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


OUR_MAC = bytes.fromhex("001122334455")
OUR_IP = bytes([10, 0, 0, 1])


def test_rx_ipv4_frame_forwarded_to_utun_and_learns_peer_mac():
    peer_mac = bytes.fromhex("aabbccddeeff")
    ip_payload = bytes([0x45]) + b"\x00" * 19
    frame = ethernet.wrap(ip_payload, ethernet.ETH_P_IP, dst=OUR_MAC, src=peer_mac)
    fake_usb = FakeRndisUsb(bulk_read_queue=[rndis.pack_packet(frame)])
    fake_utun = FakeUtun()
    bridge = Bridge(fake_usb, fake_utun, OUR_MAC, OUR_IP, max_transfer_size=0x4000)
    bridge.start()
    try:
        assert _wait_until(lambda: fake_utun.writes), "utun never received the IPv4 payload"
    finally:
        assert bridge.stop()

    assert fake_utun.writes == [ip_payload]
    assert bridge.learner.current() == peer_mac
    assert bridge.stats.rx_packets == 1


def test_rx_arp_request_for_our_ip_is_answered():
    requester_mac = bytes.fromhex("aabbccddeeff")
    requester_ip = bytes([10, 0, 0, 5])
    arp_payload = ethernet.pack_arp(
        ethernet.ARP_REQUEST, sha=requester_mac, spa=requester_ip, tha=b"\x00" * 6, tpa=OUR_IP
    )
    frame = ethernet.wrap(arp_payload, ethernet.ETH_P_ARP, dst=OUR_MAC, src=requester_mac)
    fake_usb = FakeRndisUsb(bulk_read_queue=[rndis.pack_packet(frame)])
    fake_utun = FakeUtun()
    bridge = Bridge(fake_usb, fake_utun, OUR_MAC, OUR_IP, max_transfer_size=0x4000)
    bridge.start()
    try:
        assert _wait_until(lambda: fake_usb.bulk_writes), "no ARP reply was sent"
    finally:
        assert bridge.stop()

    reply_frames = rndis.unpack_packets(fake_usb.bulk_writes[0])
    assert len(reply_frames) == 1
    ethertype, src_mac, payload = ethernet.strip(reply_frames[0])
    assert ethertype == ethernet.ETH_P_ARP
    assert src_mac == OUR_MAC
    assert reply_frames[0][0:6] == requester_mac  # dst = the requester
    arp_reply = ethernet.parse_arp(payload)
    assert arp_reply.oper == ethernet.ARP_REPLY
    assert arp_reply.sha == OUR_MAC
    assert arp_reply.spa == OUR_IP
    assert arp_reply.tha == requester_mac
    assert arp_reply.tpa == requester_ip


def test_tx_uses_broadcast_before_any_peer_mac_is_learned():
    ip_payload = bytes([0x45]) + b"\x00" * 19
    fake_usb = FakeRndisUsb()
    fake_utun = FakeUtun(read_queue=[ip_payload])
    bridge = Bridge(fake_usb, fake_utun, OUR_MAC, OUR_IP, max_transfer_size=0x4000)
    bridge.start()
    try:
        assert _wait_until(lambda: fake_usb.bulk_writes), "no frame was sent out"
    finally:
        assert bridge.stop()

    frames = rndis.unpack_packets(fake_usb.bulk_writes[0])
    assert len(frames) == 1
    ethertype, src_mac, payload = ethernet.strip(frames[0])
    assert frames[0][0:6] == ethernet.BROADCAST_MAC
    assert src_mac == OUR_MAC
    assert payload == ip_payload


def test_tx_uses_learned_peer_mac_after_an_rx_frame():
    peer_mac = bytes.fromhex("aabbccddeeff")
    ip_payload_in = bytes([0x45]) + b"\x00" * 19
    frame_in = ethernet.wrap(ip_payload_in, ethernet.ETH_P_IP, dst=OUR_MAC, src=peer_mac)
    ip_payload_out = bytes([0x45]) + b"\x11" * 19

    fake_usb = FakeRndisUsb(bulk_read_queue=[rndis.pack_packet(frame_in)])
    fake_utun = FakeUtun()
    bridge = Bridge(fake_usb, fake_utun, OUR_MAC, OUR_IP, max_transfer_size=0x4000)
    bridge.start()
    try:
        assert _wait_until(lambda: bridge.learner.current() == peer_mac), "peer MAC was never learned"
        fake_utun._read_queue.append(ip_payload_out)
        assert _wait_until(lambda: fake_usb.bulk_writes), "no frame was sent out after learning the peer MAC"
    finally:
        assert bridge.stop()

    frames = rndis.unpack_packets(fake_usb.bulk_writes[-1])
    assert frames[-1] == ethernet.wrap(ip_payload_out, ethernet.ETH_P_IP, dst=peer_mac, src=OUR_MAC)


def test_oversized_frame_is_dropped_not_sent():
    ip_payload = b"\x45" + b"\x00" * 999  # larger than the tiny max_transfer_size below
    fake_usb = FakeRndisUsb()
    fake_utun = FakeUtun(read_queue=[ip_payload])
    bridge = Bridge(fake_usb, fake_utun, OUR_MAC, OUR_IP, max_transfer_size=64)
    bridge.start()
    try:
        assert _wait_until(lambda: bridge.stats.drops >= 1), "oversized frame was not counted as a drop"
    finally:
        assert bridge.stop()

    assert fake_usb.bulk_writes == []


def test_enodev_marks_failed_and_stops_all_threads():
    exc = UsbNoDevice(-4, "LIBUSB_ERROR_NO_DEVICE", "no such device (it may have been disconnected)")
    fake_usb = FakeRndisUsb(raise_on_bulk_read=exc)
    fake_utun = FakeUtun()
    bridge = Bridge(fake_usb, fake_utun, OUR_MAC, OUR_IP, max_transfer_size=0x4000)
    bridge.start()
    try:
        assert _wait_until(lambda: bridge.failed.is_set()), "bridge.failed was never set"
    finally:
        stopped = bridge.stop()

    assert stopped
    assert bridge.failure_reason is not None
    assert "device" in bridge.failure_reason.lower()
    assert not any(t.is_alive() for t in bridge._threads)


def test_stop_returns_true_and_joins_threads_in_the_normal_case():
    fake_usb = FakeRndisUsb()
    fake_utun = FakeUtun()
    bridge = Bridge(fake_usb, fake_utun, OUR_MAC, OUR_IP, max_transfer_size=0x4000)
    bridge.start()
    assert bridge.stop() is True
    assert not any(t.is_alive() for t in bridge._threads)


# --- tx stalls: live testing found that without an active PDP context the
# FM350 accepts a handful of bulk OUT transfers and then NAKs (USBTimeoutError)
# every further write until the session recovers. These must not be treated
# as fatal USB errors. ------------------------------------------------------


class _StallingUsb(FakeRndisUsb):
    """Accepts the first ``accept_count`` bulk writes, then times out forever."""

    def __init__(self, accept_count: int):
        super().__init__()
        self._accept_count = accept_count
        self._writes_seen = 0

    def bulk_write(self, data, timeout=1000):
        self._writes_seen += 1
        if self._writes_seen > self._accept_count:
            raise UsbTimeout(-7, "LIBUSB_ERROR_TIMEOUT", "tx stall")
        return super().bulk_write(data, timeout=timeout)


class _TempStallUsb(FakeRndisUsb):
    """Times out on the first ``fail_count`` bulk writes, then recovers."""

    def __init__(self, fail_count: int):
        super().__init__()
        self._fail_count = fail_count
        self._attempts = 0

    def bulk_write(self, data, timeout=1000):
        self._attempts += 1
        if self._attempts <= self._fail_count:
            raise UsbTimeout(-7, "LIBUSB_ERROR_TIMEOUT", "tx stall")
        return super().bulk_write(data, timeout=timeout)


def test_tx_stall_is_counted_and_does_not_fail_the_bridge():
    ip_payloads = [bytes([0x45]) + bytes([i]) * 19 for i in range(5)]
    fake_usb = _StallingUsb(accept_count=3)
    fake_utun = FakeUtun(read_queue=list(ip_payloads))
    bridge = Bridge(fake_usb, fake_utun, OUR_MAC, OUR_IP, max_transfer_size=0x4000)
    bridge.start()
    try:
        assert _wait_until(lambda: bridge.stats.tx_stalls >= 1), "tx stall was never counted"
    finally:
        assert bridge.stop()

    assert not bridge.failed.is_set()
    assert bridge.stats.tx_packets == 3
    assert bridge.stats.drops >= 1


def test_tx_stall_warning_is_rate_limited(caplog):
    fake_usb = _StallingUsb(accept_count=0)
    fake_utun = FakeUtun(read_queue=[bytes([0x45]) + b"\x00" * 19 for _ in range(20)])
    bridge = Bridge(fake_usb, fake_utun, OUR_MAC, OUR_IP, max_transfer_size=0x4000)
    with caplog.at_level(logging.WARNING, logger="fm350mac.bridge"):
        bridge.start()
        try:
            assert _wait_until(lambda: bridge.stats.tx_stalls >= 5), "not enough stalls observed"
        finally:
            assert bridge.stop()

    warnings = [r for r in caplog.records if "tx stalled" in r.message]
    assert len(warnings) == 1


def test_tx_recovers_after_stall_clears():
    fake_usb = _TempStallUsb(fail_count=2)
    fake_utun = FakeUtun(read_queue=[bytes([0x45]) + b"\x00" * 19 for _ in range(5)])
    bridge = Bridge(fake_usb, fake_utun, OUR_MAC, OUR_IP, max_transfer_size=0x4000)
    bridge.start()
    try:
        assert _wait_until(lambda: bridge.stats.tx_packets >= 1), "tx never recovered after the stall cleared"
    finally:
        assert bridge.stop()

    assert bridge.stats.tx_stalls == 2
    assert not bridge.failed.is_set()
