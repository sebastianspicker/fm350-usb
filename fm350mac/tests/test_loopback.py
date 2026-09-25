"""Loopback fake-modem tests: checksum correctness and ICMP echo/ARP round
trips through pack_packet/unpack_packets. No hardware, no sockets.
"""

import socket
import struct

from fm350mac import ethernet, rndis
from fm350mac.loopback import GATEWAY_MAC, LoopbackRndis, checksum16


def _ipv4_header(src: str, dst: str, proto: int, payload_len: int, ident: int = 1, ttl: int = 64) -> bytes:
    header = bytearray(20)
    struct.pack_into(
        "!BBHHHBBH4s4s",
        header,
        0,
        0x45,
        0,
        20 + payload_len,
        ident,
        0,
        ttl,
        proto,
        0,
        socket.inet_aton(src),
        socket.inet_aton(dst),
    )
    struct.pack_into("!H", header, 10, checksum16(bytes(header)))
    return bytes(header)


def test_checksum16_matches_a_known_ip_header():
    # A commonly cited worked example (IPv4 header, src 172.16.10.99 -> dst
    # 172.16.10.12, TCP): checksum field zeroed, expected result 0xb1e6.
    header_zeroed = bytes.fromhex("4500003c1c46400040060000ac100a63ac100a0c")
    assert checksum16(header_zeroed) == 0xB1E6


def test_checksum16_self_verifies_once_embedded():
    header_zeroed = bytes.fromhex("4500003c1c46400040060000ac100a63ac100a0c")
    csum = checksum16(header_zeroed)
    header_with_csum = header_zeroed[:10] + struct.pack("!H", csum) + header_zeroed[12:]
    assert checksum16(header_with_csum) == 0


def test_icmp_echo_request_becomes_echo_reply_round_trip():
    our_mac = bytes.fromhex("aabbccddeeff")
    src_ip = socket.inet_aton("192.0.2.2")
    dst_ip = socket.inet_aton("198.51.100.1")

    icmp = bytearray(struct.pack("!BBHHH", 8, 0, 0, 1, 1) + b"ping-data")
    struct.pack_into("!H", icmp, 2, 0)
    struct.pack_into("!H", icmp, 2, checksum16(bytes(icmp)))

    ip_header = _ipv4_header("192.0.2.2", "198.51.100.1", proto=1, payload_len=len(icmp))
    ip_packet = ip_header + bytes(icmp)
    frame = ethernet.wrap(ip_packet, ethernet.ETH_P_IP, dst=GATEWAY_MAC, src=our_mac)

    modem = LoopbackRndis()
    modem.bulk_write(rndis.pack_packet(frame))
    reply_msg = modem.bulk_read(0x4000, timeout=100)

    frames = rndis.unpack_packets(reply_msg)
    assert len(frames) == 1
    ethertype, src_mac, payload = ethernet.strip(frames[0])
    assert ethertype == ethernet.ETH_P_IP
    assert src_mac == GATEWAY_MAC
    assert frames[0][0:6] == our_mac  # addressed back to the sender

    assert payload[12:16] == dst_ip  # src/dst swapped
    assert payload[16:20] == src_ip
    assert checksum16(payload[:20]) == 0  # IP header checksum self-verifies

    icmp_reply = payload[20:]
    assert icmp_reply[0] == 0  # echo reply
    assert checksum16(icmp_reply) == 0
    assert icmp_reply[4:] == bytes(icmp)[4:]  # id/seq/data preserved


def test_arp_request_is_answered_by_the_gateway_mac():
    requester_mac = bytes.fromhex("aabbccddeeff")
    requester_ip = socket.inet_aton("192.0.2.2")
    asked_ip = socket.inet_aton("198.51.100.1")

    arp_payload = ethernet.pack_arp(
        ethernet.ARP_REQUEST, sha=requester_mac, spa=requester_ip, tha=b"\x00" * 6, tpa=asked_ip
    )
    frame = ethernet.wrap(arp_payload, ethernet.ETH_P_ARP, dst=ethernet.BROADCAST_MAC, src=requester_mac)

    modem = LoopbackRndis()
    modem.bulk_write(rndis.pack_packet(frame))
    reply_msg = modem.bulk_read(0x4000, timeout=100)

    frames = rndis.unpack_packets(reply_msg)
    assert len(frames) == 1
    ethertype, src_mac, payload = ethernet.strip(frames[0])
    assert ethertype == ethernet.ETH_P_ARP
    assert src_mac == GATEWAY_MAC
    assert frames[0][0:6] == requester_mac

    arp_reply = ethernet.parse_arp(payload)
    assert arp_reply.oper == ethernet.ARP_REPLY
    assert arp_reply.sha == GATEWAY_MAC
    assert arp_reply.spa == asked_ip
    assert arp_reply.tha == requester_mac
    assert arp_reply.tpa == requester_ip


def test_bulk_read_times_out_when_nothing_queued():
    import pytest

    from fm350mac.usb_async import UsbTimeout

    modem = LoopbackRndis()
    with pytest.raises(UsbTimeout):
        modem.bulk_read(0x4000, timeout=10)
