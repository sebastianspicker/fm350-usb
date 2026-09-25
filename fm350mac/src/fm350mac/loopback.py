"""In-process fake modem for ``fm350mac up --loopback``: a smoke test for the
utun + netconfig + bridge path with no SIM and no real USB device.

``LoopbackRndis`` implements the same interface Bridge uses on
``usb_transport.RndisUsb`` (bulk_read/bulk_write plus the control-channel
methods, which are no-ops here -- there's no RNDIS handshake to do since
cli.py skips RndisDevice entirely in loopback mode). It receives
REMOTE_NDIS_PACKET_MSGs on ``bulk_write``, decodes the Ethernet/IP inside,
and answers ARP requests and ICMP echo requests for *any* address (it isn't
choosy about the destination -- it's standing in for "the network"), queuing
replies for ``bulk_read`` with the source MAC set to the synthetic gateway
address ``00:00:11:12:13:14``.

Addressing (TEST-NET / TEST-NET-2, RFC 5737): our side is 192.0.2.2, and the
only route added is a host route to 198.51.100.1 via the utun -- never the
default route.
"""

from __future__ import annotations

import queue
import struct
import threading

from . import ethernet, rndis
from .usb_async import UsbTimeout

OUR_IP = "192.0.2.2"
PEER_IP = "198.51.100.1"
OUR_MAC = bytes.fromhex("020000000001")  # locally administered, arbitrary
GATEWAY_MAC = bytes.fromhex("000011121314")  # matches the real FM350's fixed MAC
MAX_TRANSFER_SIZE = 0x4000

_ICMP_ECHO_REQUEST = 8
_ICMP_ECHO_REPLY = 0
_IPPROTO_ICMP = 1


def checksum16(data: bytes) -> int:
    """Internet checksum (RFC 1071) over ``data``, padded with a zero byte if odd-length."""
    if len(data) % 2:
        data = data + b"\x00"
    total = sum(struct.unpack(f"!{len(data) // 2}H", data))
    while total >> 16:
        total = (total & 0xFFFF) + (total >> 16)
    return (~total) & 0xFFFF


def _build_icmp_echo_reply(ip_packet: bytes) -> bytes | None:
    """Build the reply IPv4/ICMP packet for an echo request, or None if
    ``ip_packet`` isn't a v4 ICMP echo request.
    """
    if len(ip_packet) < 20:
        return None
    version_ihl = ip_packet[0]
    if version_ihl >> 4 != 4:
        return None
    ihl = (version_ihl & 0x0F) * 4
    if len(ip_packet) < ihl + 8 or ip_packet[9] != _IPPROTO_ICMP:
        return None
    icmp = ip_packet[ihl:]
    if icmp[0] != _ICMP_ECHO_REQUEST:
        return None

    src_ip, dst_ip = ip_packet[12:16], ip_packet[16:20]

    reply_icmp = bytearray(icmp)
    reply_icmp[0] = _ICMP_ECHO_REPLY
    reply_icmp[1] = 0
    reply_icmp[2:4] = b"\x00\x00"
    struct.pack_into("!H", reply_icmp, 2, checksum16(bytes(reply_icmp)))

    header = bytearray(ip_packet[:ihl])
    header[8] = 64  # fresh TTL
    header[10:12] = b"\x00\x00"
    header[12:16] = dst_ip
    header[16:20] = src_ip
    struct.pack_into("!H", header, 10, checksum16(bytes(header)))

    return bytes(header) + bytes(reply_icmp)


class LoopbackRndis:
    """Fake modem: answers ICMP echo and ARP in-process, no USB involved."""

    def __init__(self) -> None:
        self._replies: queue.Queue[bytes] = queue.Queue()
        self._closed = threading.Event()

    # --- RndisUsb-compatible bulk interface ---------------------------------

    def bulk_write(self, data: bytes, timeout: int = 1000) -> int:
        for frame in rndis.unpack_packets(data):
            self._handle_frame(frame)
        return len(data)

    def bulk_read(self, size: int, timeout: int = 1000) -> bytes:
        try:
            frame = self._replies.get(timeout=timeout / 1000)
        except queue.Empty:
            raise UsbTimeout(-7, "LIBUSB_ERROR_TIMEOUT", "loopback bulk_read") from None
        return rndis.pack_packet(frame)

    # --- RndisUsb-compatible control interface (no-ops: no RNDIS handshake) -

    def send_encapsulated(self, msg: bytes) -> None:
        return None

    def get_encapsulated(self, size: int = 4096) -> bytes:
        return b""

    def wait_notify(self, timeout: int = 2000) -> bytes | None:
        """Always "times out" (there's no RNDIS handshake to notify about),
        but -- like the real interrupt endpoint -- blocks for (up to) the
        requested timeout instead of returning immediately. A fake transport
        that returns instantly makes Bridge's control thread busy-spin,
        starving the rx/tx threads of the GIL between Python's default
        thread-switch checks (measured: 18 ms median echo RTT instead of
        <1 ms). ``close()`` wakes any pending wait immediately for a fast
        shutdown.
        """
        self._closed.wait(timeout / 1000)
        return None

    def close(self) -> None:
        self._closed.set()

    # --- frame handling ------------------------------------------------------

    def _handle_frame(self, frame: bytes) -> None:
        try:
            ethertype, src_mac, payload = ethernet.strip(frame)
        except ValueError:
            return
        if ethertype == ethernet.ETH_P_ARP:
            self._handle_arp(payload, src_mac)
        elif ethertype == ethernet.ETH_P_IP:
            self._handle_ip(payload, src_mac)
        # IPv6 and anything else: no loopback support, just drop.

    def _handle_arp(self, payload: bytes, src_mac: bytes) -> None:
        try:
            arp = ethernet.parse_arp(payload)
        except ValueError:
            return
        if arp.oper != ethernet.ARP_REQUEST:
            return
        # Answer for whatever address was asked about -- this fake stands in
        # for "the network", not a specific host.
        reply_payload = ethernet.build_arp_reply(arp, GATEWAY_MAC, arp.tpa)
        frame = ethernet.wrap(reply_payload, ethernet.ETH_P_ARP, dst=arp.sha, src=GATEWAY_MAC)
        self._replies.put(frame)

    def _handle_ip(self, payload: bytes, src_mac: bytes) -> None:
        reply_ip = _build_icmp_echo_reply(payload)
        if reply_ip is None:
            return
        frame = ethernet.wrap(reply_ip, ethernet.ETH_P_IP, dst=src_mac, src=GATEWAY_MAC)
        self._replies.put(frame)
