"""Ethernet framing and ARP handling (pure, no I/O).

The FM350's RNDIS data path carries Ethernet frames around what is really an
IP pipe: the modem assigns no real peer MAC, so we strip Ethernet headers on
the way in, answer ARP for our own IP in user space, and add a synthetic
Ethernet header (learned peer MAC, falling back to broadcast) on the way out.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

ETH_P_IP = 0x0800
ETH_P_ARP = 0x0806
ETH_P_IPV6 = 0x86DD

BROADCAST_MAC = b"\xff\xff\xff\xff\xff\xff"

_ETH_HEADER_LEN = 14
_ARP_LEN = 28

ARP_REQUEST = 1
ARP_REPLY = 2


def strip(frame: bytes) -> tuple[int, bytes, bytes]:
    """Split an Ethernet ``frame`` into ``(ethertype, src_mac, payload)``."""
    if len(frame) < _ETH_HEADER_LEN:
        raise ValueError(f"frame too short: {len(frame)} bytes")
    src = frame[6:12]
    ethertype = int.from_bytes(frame[12:14], "big")
    return ethertype, bytes(src), bytes(frame[_ETH_HEADER_LEN:])


def wrap(payload: bytes, ethertype: int, dst: bytes, src: bytes) -> bytes:
    """Build an Ethernet frame around ``payload``."""
    if len(dst) != 6 or len(src) != 6:
        raise ValueError("MAC addresses must be 6 bytes")
    header = dst + src + ethertype.to_bytes(2, "big")
    return header + payload


@dataclass
class Arp:
    """Parsed ARP packet (Ethernet/IPv4 only)."""

    oper: int
    sha: bytes  # sender hardware address (MAC)
    spa: bytes  # sender protocol address (IPv4, 4 bytes)
    tha: bytes  # target hardware address (MAC)
    tpa: bytes  # target protocol address (IPv4, 4 bytes)


def parse_arp(payload: bytes) -> Arp:
    """Parse an ARP packet (the Ethernet payload after ``strip``)."""
    if len(payload) < _ARP_LEN:
        raise ValueError(f"ARP packet too short: {len(payload)} bytes")
    _htype, _ptype, hlen, plen, oper = struct.unpack_from(">HHBBH", payload)
    if hlen != 6 or plen != 4:
        raise ValueError(f"unsupported ARP hlen/plen: {hlen}/{plen}")
    sha = payload[8:14]
    spa = payload[14:18]
    tha = payload[18:24]
    tpa = payload[24:28]
    return Arp(oper=oper, sha=bytes(sha), spa=bytes(spa), tha=bytes(tha), tpa=bytes(tpa))


def pack_arp(oper: int, sha: bytes, spa: bytes, tha: bytes, tpa: bytes) -> bytes:
    """Encode an ARP (Ethernet/IPv4) packet."""
    header = struct.pack(">HHBBH", 1, ETH_P_IP, 6, 4, oper)
    return header + sha + spa + tha + tpa


def build_arp_reply(request: Arp, our_mac: bytes, our_ip: bytes) -> bytes:
    """Build the ARP reply payload answering ``request`` as ``our_ip``/``our_mac``."""
    return pack_arp(ARP_REPLY, sha=our_mac, spa=our_ip, tha=request.sha, tpa=request.spa)


class PeerMacLearner:
    """Remembers the source MAC of the first IPv4/IPv6 frame seen.

    Before any frame has been observed, ``current()`` returns the broadcast
    address so outgoing frames still get a destination MAC to use.
    """

    def __init__(self) -> None:
        self._mac: bytes | None = None

    def observe(self, ethertype: int, src_mac: bytes) -> None:
        """Record ``src_mac`` if this is the first IPv4/IPv6 frame seen."""
        if self._mac is None and ethertype in (ETH_P_IP, ETH_P_IPV6):
            self._mac = src_mac

    def current(self) -> bytes:
        """Return the learned peer MAC, or broadcast if none learned yet."""
        return self._mac if self._mac is not None else BROADCAST_MAC
