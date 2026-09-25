"""Ethernet wrap/strip and ARP tests (ethernet.py). No I/O, no hardware."""

from fm350mac import ethernet


def test_wrap_strip_round_trip():
    payload = b"\x45\x00" + b"\x00" * 18  # fake IPv4-ish payload
    src = bytes.fromhex("aabbccddeeff")
    dst = bytes.fromhex("001122334455")
    frame = ethernet.wrap(payload, ethernet.ETH_P_IP, dst=dst, src=src)
    ethertype, got_src, got_payload = ethernet.strip(frame)
    assert ethertype == ethernet.ETH_P_IP
    assert got_src == src
    assert got_payload == payload


def test_wrap_puts_dst_then_src_then_ethertype():
    dst = bytes.fromhex("001122334455")
    src = bytes.fromhex("aabbccddeeff")
    frame = ethernet.wrap(b"payload", ethernet.ETH_P_IPV6, dst=dst, src=src)
    assert frame[0:6] == dst
    assert frame[6:12] == src
    assert frame[12:14] == ethernet.ETH_P_IPV6.to_bytes(2, "big")
    assert frame[14:] == b"payload"


def test_arp_reply_fields():
    requester_mac = bytes.fromhex("aabbccddeeff")
    requester_ip = bytes([10, 0, 0, 5])
    our_mac = bytes.fromhex("001122334455")
    our_ip = bytes([10, 0, 0, 1])
    request = ethernet.Arp(
        oper=ethernet.ARP_REQUEST, sha=requester_mac, spa=requester_ip, tha=b"\x00" * 6, tpa=our_ip
    )
    reply = ethernet.build_arp_reply(request, our_mac, our_ip)
    parsed = ethernet.parse_arp(reply)
    assert parsed.oper == ethernet.ARP_REPLY
    assert parsed.sha == our_mac
    assert parsed.spa == our_ip
    assert parsed.tha == requester_mac
    assert parsed.tpa == requester_ip


def test_parse_arp_round_trip_via_pack():
    packed = ethernet.pack_arp(
        ethernet.ARP_REQUEST,
        sha=bytes.fromhex("aabbccddeeff"),
        spa=bytes([1, 2, 3, 4]),
        tha=bytes.fromhex("000000000000"),
        tpa=bytes([5, 6, 7, 8]),
    )
    parsed = ethernet.parse_arp(packed)
    assert parsed.oper == ethernet.ARP_REQUEST
    assert parsed.sha == bytes.fromhex("aabbccddeeff")
    assert parsed.spa == bytes([1, 2, 3, 4])
    assert parsed.tpa == bytes([5, 6, 7, 8])


def test_peer_mac_learner_defaults_to_broadcast():
    learner = ethernet.PeerMacLearner()
    assert learner.current() == ethernet.BROADCAST_MAC


def test_peer_mac_learner_learns_first_ip_frame_only():
    learner = ethernet.PeerMacLearner()
    first = bytes.fromhex("aabbccddeeff")
    second = bytes.fromhex("112233445566")
    learner.observe(ethernet.ETH_P_IP, first)
    learner.observe(ethernet.ETH_P_IPV6, second)
    assert learner.current() == first


def test_peer_mac_learner_ignores_non_ip_frames():
    learner = ethernet.PeerMacLearner()
    learner.observe(ethernet.ETH_P_ARP, bytes.fromhex("aabbccddeeff"))
    assert learner.current() == ethernet.BROADCAST_MAC
