"""Regression test for a GIL-starvation bug: LoopbackRndis.wait_notify()
used to return None immediately instead of blocking for its timeout, so
Bridge's control thread busy-spun and starved the rx/tx threads of the GIL
between Python's default thread-switch checks.

Measured with the real Bridge + LoopbackRndis and a socketpair in place of
utun: median echo RTT was 18.3 ms before the fix (with a busy-spinning
control thread), 0.026 ms after (wait_notify actually blocks). This test
uses a generous bound for CI noise, but would have caught the regression by
two orders of magnitude.
"""

from __future__ import annotations

import socket
import statistics
import struct
import time

from fm350mac.bridge import Bridge
from fm350mac.loopback import MAX_TRANSFER_SIZE, LoopbackRndis, checksum16
from fm350mac.utun import Utun, decode_af, encode_af

OUR_MAC = bytes.fromhex("001122334455")
OUR_IP = bytes([192, 0, 2, 2])

_ECHO_COUNT = 50
_MEDIAN_RTT_BUDGET_S = 0.002  # 2 ms: generous for CI, ~100x the measured busy-spin RTT
_CONTROL_LOOP_ITERATIONS_BUDGET = 50  # over a 1s window; a busy spin does tens of thousands


def _build_icmp_echo_request(seq: int, payload: bytes = b"ping") -> bytes:
    icmp = struct.pack("!BBHHH", 8, 0, 0, 0x1234, seq) + payload
    checksum = checksum16(icmp)
    icmp = struct.pack("!BBHHH", 8, 0, checksum, 0x1234, seq) + payload
    total_len = 20 + len(icmp)
    ip_header = struct.pack(
        "!BBHHHBBH4s4s", 0x45, 0, total_len, 0, 0, 64, 1, 0, b"\xc0\x00\x02\x01", bytes(OUR_IP)
    )
    return ip_header + icmp


def _wait_until(predicate, timeout=1.0, interval=0.005) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def test_echo_round_trip_latency_and_control_loop_does_not_busy_spin():
    modem = LoopbackRndis()
    sock_bridge, sock_kernel = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
    utun = Utun(sock_bridge, "utun-test")
    sock_kernel.settimeout(1.0)

    bridge = Bridge(modem, utun, OUR_MAC, OUR_IP, max_transfer_size=MAX_TRANSFER_SIZE)

    wait_notify_calls = {"n": 0}
    real_wait_notify = modem.wait_notify

    def counting_wait_notify(timeout=2000):
        wait_notify_calls["n"] += 1
        return real_wait_notify(timeout)

    modem.wait_notify = counting_wait_notify

    bridge.start()
    try:
        # --- control loop must actually block, not busy-spin -------------
        wait_notify_calls["n"] = 0
        time.sleep(1.0)
        iterations = wait_notify_calls["n"]
        assert iterations < _CONTROL_LOOP_ITERATIONS_BUDGET, (
            f"control loop iterated {iterations} times in 1s (busy-spinning instead of blocking "
            f"on wait_notify's timeout -- see loopback.LoopbackRndis.wait_notify)"
        )

        # --- echo round-trip latency ---------------------------------------
        rtts = []
        for seq in range(_ECHO_COUNT):
            request = _build_icmp_echo_request(seq)
            t0 = time.perf_counter()
            sock_kernel.send(encode_af(request))
            buf = sock_kernel.recv(4096)
            rtts.append(time.perf_counter() - t0)
            _af, reply = decode_af(buf)
            assert reply[0] >> 4 == 4
            assert reply[9] == 1  # IPPROTO_ICMP
            assert reply[20] == 0  # ICMP echo reply type

        median_rtt = statistics.median(rtts)
        assert median_rtt < _MEDIAN_RTT_BUDGET_S, (
            f"median echo RTT was {median_rtt * 1000:.3f} ms (budget {_MEDIAN_RTT_BUDGET_S * 1000:.1f} ms) "
            f"-- see loopback.LoopbackRndis.wait_notify for the busy-spin this guards against"
        )
    finally:
        modem.close()  # wakes any pending wait_notify() so stop() doesn't hang
        assert bridge.stop()
        sock_kernel.close()


def test_wait_notify_blocks_for_the_timeout_and_close_wakes_it_immediately():
    modem = LoopbackRndis()
    try:
        t0 = time.perf_counter()
        result = modem.wait_notify(timeout=200)
        elapsed = time.perf_counter() - t0
        assert result is None
        assert elapsed >= 0.15, f"wait_notify returned after {elapsed * 1000:.1f} ms instead of blocking ~200 ms"
    finally:
        pass

    # close() must wake a pending wait_notify() promptly (bridge.stop() relies on this).
    import threading

    woke = threading.Event()

    def waiter():
        modem.wait_notify(timeout=5000)
        woke.set()

    t = threading.Thread(target=waiter, daemon=True)
    t.start()
    assert _wait_until(lambda: t.is_alive())  # give it a moment to actually start waiting
    start = time.perf_counter()
    modem.close()
    assert woke.wait(timeout=1.0), "close() did not wake a pending wait_notify()"
    assert time.perf_counter() - start < 0.1
    t.join(timeout=1.0)
