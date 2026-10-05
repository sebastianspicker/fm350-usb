"""AsyncBridge: the async-I/O sibling of bridge.Bridge, keeping several
bulk transfers in flight per direction via usb_async's libusb async
transfer pools instead of one synchronous transfer per packet.

Same public API as Bridge (start/stop/failed/failure_reason/stats/
set_our_ip/our_ip/device_lost), so cli.py's ``up --io async|sync`` can use
either. See docs/macos-driver.md, "New data path AsyncBridge", for the design.
"""

from __future__ import annotations

import logging
import queue
import socket
import threading
import time
from dataclasses import dataclass

from . import ethernet, rndis
from .usb_async import AsyncEndpoint, EventLoop, UsbError, UsbNoDevice
from .usb_transport import RateLimiter, RndisUsb, is_response_available, mark_usb_unsafe
from .utun import Utun

_log = logging.getLogger(__name__)

DEFAULT_RX_URBS = 8
DEFAULT_TX_URBS = 8
_RX_BUFFER_SIZE = 0x4000  # 16 KiB
_INTERRUPT_BUFFER_SIZE = 64  # matches the control interface's interrupt endpoint
_OUT_TIMEOUT_MS = 500
_UTUN_TIMEOUT_S = 0.5
_KEEPALIVE_INTERVAL_S = 5.0
_CONTROL_NOTIFY_TIMEOUT_S = 1.0
_JOIN_TIMEOUT_S = 2.0
_TX_STALL_WARN_INTERVAL_S = 10.0
_LOG_INTERVAL_S = 10.0  # rate limit for per-packet error logs
_UTUN_ERROR_BACKOFF_S = 0.05  # first backoff after a utun.read() OSError; grows linearly
_UTUN_ERROR_BACKOFF_MAX_S = 1.0
_UTUN_ERROR_FAIL_STREAK = 30  # consecutive utun.read() errors before the bridge fails
_MAX_PENDING_RESPONSES = 8  # cap on queued RESPONSE_AVAILABLE notifications
# How long the tx thread waits for a free OUT slot before dropping a packet.
# Without this wait the OUT pool acted as an 8-packet tail-drop queue: on
# hardware (2026-10-05) a TCP upload lost 53 packets in its first second and
# collapsed from 5 to 1 Mbit/s. Waiting pushes back into the utun's kernel
# queue instead, so TCP's own congestion control sees the real link rate;
# the bound keeps a stalled modem (no data session) from wedging the thread.
_TX_SLOT_WAIT_S = 1.0
_TX_SLOT_POLL_S = 0.05
# Keepalive watchdog: the FM350 answers each of our keepalives (every
# _KEEPALIVE_INTERVAL_S) with a KEEPALIVE_CMPLT, delivered through the normal
# RESPONSE_AVAILABLE -> GET_ENCAPSULATED_RESPONSE flow. If none arrives for
# this many intervals the device's control side is wedged (a data session
# that silently stopped passing traffic), so the bridge fails and the
# caller's rebuild path takes over. Only timestamps: never an extra GET.
_KEEPALIVE_MISSED_LIMIT = 3
# ARP replies that found every OUT slot busy, waiting for the tx thread (see
# _handle_arp). A handful is plenty: the modem asks for one MAC.
_CONTROL_TX_QUEUE_MAX = 4

# Sane range for the device-reported max_transfer_size (INIT_CMPLT): the FM350
# reports 2048. Out-of-range values are clamped (with a warning); one too
# small to carry an MTU-1500 frame is refused (see clamp_max_transfer_size).
MIN_MAX_TRANSFER_SIZE = 1600
MAX_MAX_TRANSFER_SIZE = 0x4000
_MTU = 1500
# PACKET_MSG header + Ethernet header + a full-MTU IP packet.
_MTU_FRAME_TRANSFER_SIZE = len(rndis.pack_packet(b"\x00" * (14 + _MTU)))


def clamp_max_transfer_size(reported: int) -> int:
    """Clamp a device-reported max_transfer_size into
    [MIN_MAX_TRANSFER_SIZE, MAX_MAX_TRANSFER_SIZE], warning when it had to.

    Raises ValueError if ``reported`` can't even carry one MTU-1500 frame:
    the data path would silently drop every full-size packet. Clamping up
    to MIN_MAX_TRANSFER_SIZE is safe otherwise, since nothing larger than
    an MTU-1500 frame is ever sent.
    """
    if reported < _MTU_FRAME_TRANSFER_SIZE:
        raise ValueError(
            f"device max_transfer_size {reported} is too small for an MTU-{_MTU} frame "
            f"({_MTU_FRAME_TRANSFER_SIZE} bytes needed)"
        )
    if reported < MIN_MAX_TRANSFER_SIZE or reported > MAX_MAX_TRANSFER_SIZE:
        clamped = min(max(reported, MIN_MAX_TRANSFER_SIZE), MAX_MAX_TRANSFER_SIZE)
        _log.warning(
            "device reported max_transfer_size=%d, outside [%d, %d]; using %d",
            reported, MIN_MAX_TRANSFER_SIZE, MAX_MAX_TRANSFER_SIZE, clamped,
        )
        return clamped
    return reported


def _is_device_gone(exc: UsbError) -> bool:
    return isinstance(exc, UsbNoDevice)


@dataclass
class AsyncBridgeStats:
    """Running counters for the async data pump (same shape as bridge.BridgeStats)."""

    rx_packets: int = 0
    rx_bytes: int = 0
    tx_packets: int = 0
    tx_bytes: int = 0
    drops: int = 0
    tx_stalls: int = 0  # genuine OUT timeouts plus pool-full drops
    tx_timeouts: int = 0  # genuine OUT timeouts/failures only (not pool-full drops)
    # Instrumentation (see AsyncBridge.perf_snapshot). Each is written by a
    # single thread, so none needs _stats_lock.
    tx_slot_waits: int = 0  # times the tx thread found every OUT slot busy and had to wait (tx thread)
    tx_slot_wait_ns: int = 0  # total time spent in those waits (tx thread)
    rx_urbs: int = 0  # completed RX bulk transfers (event thread)
    rx_urb_frames: int = 0  # Ethernet frames across them, so frames/URB = rx_urb_frames / rx_urbs (event thread)
    rx_urb_frames_max: int = 0  # most frames seen in one RX URB (event thread)


class AsyncBridge:
    """Bridges RNDIS Ethernet frames to/from a utun interface, keeping
    ``rx_urbs``/``tx_urbs`` bulk transfers in flight per direction.
    """

    def __init__(
        self,
        rndis_usb: RndisUsb,
        utun: Utun,
        our_mac: bytes,
        our_ip: bytes,
        max_transfer_size: int,
        *,
        rx_urbs: int = DEFAULT_RX_URBS,
        tx_urbs: int = DEFAULT_TX_URBS,
        packet_alignment_factor: int = 0,
    ) -> None:
        self.usb = rndis_usb
        self.utun = utun
        self.our_mac = our_mac
        self._our_ip_lock = threading.Lock()
        self._our_ip = our_ip
        max_transfer_size = clamp_max_transfer_size(max_transfer_size)
        self.max_transfer_size = max_transfer_size
        self.rx_urbs = rx_urbs
        self.tx_urbs = tx_urbs
        self.packet_alignment_factor = packet_alignment_factor
        self.learner = ethernet.PeerMacLearner()
        self.stats = AsyncBridgeStats()
        self.failed = threading.Event()
        self.failure_reason: str | None = None
        self.device_lost = False
        self._stop = threading.Event()
        # One count per RESPONSE_AVAILABLE notification not yet answered
        # with a GET_ENCAPSULATED_RESPONSE; the condition's lock guards it.
        self._notify_cond = threading.Condition()
        self._pending_responses = 0
        self._keepalive_request_id = 0
        self._tx_thread: threading.Thread | None = None
        self._control_thread: threading.Thread | None = None
        self._tx_stall_warn_last = 0.0
        # Signalled whenever an OUT transfer retires, i.e. a slot may be free.
        # _tx_slots_freed counts those events (under the condition's lock) so
        # the tx thread can tell whether one happened between its failed
        # submit_out() and its wait -- otherwise that wakeup is lost and it
        # sleeps a whole _TX_SLOT_POLL_S with a slot available.
        self._tx_slot_freed = threading.Condition()
        self._tx_slots_freed = 0
        self._tx_stall_warn_count_since = 0
        self._utun_write_log = RateLimiter(_LOG_INTERVAL_S)
        self._malformed_log = RateLimiter(_LOG_INTERVAL_S)
        self._utun_read_log = RateLimiter(_LOG_INTERVAL_S)
        self._control_log = RateLimiter(_LOG_INTERVAL_S)
        # ARP replies handed from the EventLoop thread to the tx thread when
        # no OUT slot was free (see _handle_arp).
        self._control_tx: queue.Queue[bytes] = queue.Queue(maxsize=_CONTROL_TX_QUEUE_MAX)
        # monotonic time of the last successful KEEPALIVE_CMPLT (or of start()), only
        # touched by the control thread: see _KEEPALIVE_MISSED_LIMIT.
        self._last_keepalive_ack = time.monotonic()
        # stats are touched from both the tx thread
        # (_send_frame's pool-exhausted path) and the EventLoop thread
        # (_on_rx_complete/_handle_arp, _on_tx_result): a bare += is not
        # atomic (LOAD/ADD/STORE), so concurrent increments from two threads
        # can lose an update without this lock.
        self._stats_lock = threading.Lock()

        device = rndis_usb.usb_device
        self._event_loop = EventLoop(device.libusb, device.ctx, usb_device=device, on_fatal=self._on_pool_fatal)
        self._rx_pool = AsyncEndpoint(
            device.libusb, device.handle, rndis_usb.ep_bulk_in, "in", "bulk",
            count=rx_urbs, buffer_size=_RX_BUFFER_SIZE, timeout_ms=0,
            on_complete=self._on_rx_complete, on_fatal=self._on_pool_fatal,
        )
        self._tx_pool = AsyncEndpoint(
            device.libusb, device.handle, rndis_usb.ep_bulk_out, "out", "bulk",
            count=tx_urbs, buffer_size=max_transfer_size + 1, timeout_ms=_OUT_TIMEOUT_MS,
            on_out_result=self._on_tx_result, on_fatal=self._on_pool_fatal,
        )
        self._control_pool = AsyncEndpoint(
            device.libusb, device.handle, rndis_usb.ep_interrupt, "in", "interrupt",
            count=1, buffer_size=_INTERRUPT_BUFFER_SIZE, timeout_ms=0,
            on_complete=self._on_control_notify, on_fatal=self._on_pool_fatal,
        )
        self._event_loop.register(self._rx_pool)
        self._event_loop.register(self._tx_pool)
        self._event_loop.register(self._control_pool)

    @property
    def our_ip(self) -> bytes:
        """The IP address the ARP responder answers for (thread-safe)."""
        with self._our_ip_lock:
            return self._our_ip

    def set_our_ip(self, ip: str) -> None:
        """Update the IP the ARP responder answers for (thread-safe)."""
        with self._our_ip_lock:
            self._our_ip = socket.inet_aton(ip)

    def start(self) -> None:
        """Start the event loop, submit the RX/control pools, and start the tx/control threads."""
        self._stop.clear()
        self.failed.clear()
        self.failure_reason = None
        self.device_lost = False
        # Non-blocking reads (recv first, poll only when idle) rather than a
        # socket timeout, whose recv() polls before every packet.
        self.utun.set_read_wait(_UTUN_TIMEOUT_S)
        self.utun.tune_buffers()
        with self._notify_cond:
            self._pending_responses = 0
        while not self._control_tx.empty():
            self._control_tx.get_nowait()
        self._last_keepalive_ack = time.monotonic()
        self._event_loop.start()
        # A failed initial submit marks the pool fatal, which fails the bridge
        # (via _on_pool_fatal) so the caller sees it instead of a dead pump.
        self._rx_pool.start()
        self._control_pool.start()
        loops = (
            ("fm350mac-async-tx", self._tx_loop),
            ("fm350mac-async-control", self._control_loop),
        )
        threads = [
            threading.Thread(target=self._run_guarded, args=(loop, name), name=name, daemon=True)
            for name, loop in loops
        ]
        self._tx_thread, self._control_thread = threads
        for t in threads:
            t.start()

    def stop(self) -> bool:
        """Signal both threads and the event loop to stop and wait for them.

        Returns True if the tx/control threads exited and every in-flight
        USB transfer retired within their join timeouts -- if False, the
        caller must not reuse the USB handle (e.g. for RNDIS HALT), since
        that would race a transfer whose memory was leaked rather than freed.
        """
        self._stop.set()
        self._wake_control()  # wake the control thread if it's waiting
        all_stopped = True
        for t in (self._tx_thread, self._control_thread):
            if t is None:
                continue
            t.join(timeout=_JOIN_TIMEOUT_S)
            if t.is_alive():
                all_stopped = False
                _log.warning("thread %s did not exit within %.1fs", t.name, _JOIN_TIMEOUT_S)
                # The thread may still be inside a call on the USB handle:
                # closing it underneath would be a use-after-free.
                mark_usb_unsafe(self.usb, f"thread {t.name} did not exit within {_JOIN_TIMEOUT_S:.1f}s")
        drained = self._event_loop.stop()
        return all_stopped and drained

    def fail(self, reason: str) -> None:
        """Fail the bridge from outside (e.g. the supervisor's stall
        detector), exactly like an internal fatal error: ``failed`` is set,
        both threads stop, and the caller's rebuild path takes over.
        """
        self._mark_failed(reason)

    def _run_guarded(self, target, name: str) -> None:
        try:
            target()
        except Exception:
            _log.exception("%s crashed", name)
            self._mark_failed(f"{name}: unexpected exception")

    def _mark_failed(self, reason: str, device_lost: bool = False) -> None:
        if not self.failed.is_set():
            self.failure_reason = reason
            _log.error("bridge failed: %s", reason)
        if device_lost:
            self.device_lost = True
        self.failed.set()
        self._stop.set()
        self._wake_control()

    def _wake_control(self) -> None:
        with self._notify_cond:
            self._notify_cond.notify_all()

    def _on_pool_fatal(self, reason: str, device_lost: bool) -> None:
        self._mark_failed(reason, device_lost=device_lost)

    def _incr_stat(self, name: str, amount: int = 1) -> None:
        """Thread-safe ``self.stats.<name> += amount`` (see the lock's docstring in __init__)."""
        with self._stats_lock:
            setattr(self.stats, name, getattr(self.stats, name) + amount)

    # --- rx: pool callback runs on the EventLoop thread, so RX order (and
    # the utun writes derived from it) is preserved --------------------------

    def _on_rx_complete(self, data: bytes) -> None:
        frames, malformed = rndis.unpack_packets_counted(data, self.packet_alignment_factor)
        stats = self.stats
        stats.rx_urbs += 1
        stats.rx_urb_frames += len(frames)
        if len(frames) > stats.rx_urb_frames_max:
            stats.rx_urb_frames_max = len(frames)
        if malformed:
            self._incr_stat("drops")
            if self._malformed_log.allow():
                _log.warning("rx: malformed trailing data in a %d-byte bulk transfer (dropped)", len(data))
        # Each frame is independent: one bad frame must never take down the
        # callback (and with it the whole RX pool).
        for frame in frames:
            try:
                self._handle_frame(frame)
            except ValueError:
                self._incr_stat("drops")
                if self._malformed_log.allow():
                    _log.warning("rx: dropped a malformed frame", exc_info=True)

    def _handle_frame(self, frame: bytes) -> None:
        try:
            ethertype, src_mac, payload = ethernet.strip(frame)
        except ValueError:
            self._incr_stat("drops")
            return
        if ethertype in (ethernet.ETH_P_IP, ethernet.ETH_P_IPV6):
            # rx/tx stats count IP bytes only (what the SIM is billed for):
            # not the synthetic 14-byte Ethernet header, nor link-local ARP.
            self._incr_stat("rx_packets")
            self._incr_stat("rx_bytes", len(payload))
            self.learner.observe(ethertype, src_mac)
            try:
                # This runs on the single libusb event thread: a write that
                # can block (even briefly, e.g. socket.send()'s internal
                # timeout-bounded retry) would delay every other pool's
                # completion. write_nonblocking() is a single non-blocking
                # attempt that raises immediately if the kernel send buffer
                # is full, instead of blocking up to utun's read timeout.
                self.utun.write_nonblocking(payload)
            except BlockingIOError:
                self._incr_stat("drops")
            except OSError as exc:
                self._incr_stat("drops")
                if self._utun_write_log.allow():
                    _log.warning("utun write failed: %s (further failures counted as drops)", exc)
        elif ethertype == ethernet.ETH_P_ARP:
            self._handle_arp(payload)
        else:
            self._incr_stat("drops")

    def _handle_arp(self, payload: bytes) -> None:
        try:
            arp = ethernet.parse_arp(payload)
        except ValueError:
            self._incr_stat("drops")
            return
        if arp.oper != ethernet.ARP_REQUEST or arp.tpa != self.our_ip:
            return
        reply_payload = ethernet.build_arp_reply(arp, self.our_mac, self.our_ip)
        frame = ethernet.wrap(reply_payload, ethernet.ETH_P_ARP, dst=arp.sha, src=self.our_mac)
        msg = self._out_msg(frame)
        if msg is None or self._tx_pool.submit_out(msg):
            return
        # Every OUT slot is busy (e.g. under upload load): this runs on the
        # EventLoop thread, which must never wait for a slot, so hand the
        # reply to the tx thread, which sends it before its next utun packet.
        # A small queue rather than an OUT slot reserved for control frames:
        # reserving one would shrink the data pool (to nothing with
        # --tx-urbs 1) and needs a pool API change, while the queue leaves
        # the pool alone and costs at most one utun read timeout of latency.
        try:
            self._control_tx.put_nowait(msg)
        except queue.Full:
            # Not a tx_stall: those count data packets the modem didn't take.
            self._incr_stat("drops")

    # --- tx: utun -> async OUT pool ------------------------------------------

    def _drain_control_tx(self) -> None:
        """Send queued ARP replies (tx thread only, see _handle_arp)."""
        while not self._stop.is_set():
            try:
                msg = self._control_tx.get_nowait()
            except queue.Empty:
                return
            if not self._submit_waiting(msg, None) and not self._stop.is_set():
                self._incr_stat("drops")  # an ARP reply, not a data packet: no tx_stall

    def _tx_loop(self) -> None:
        read_errors = 0
        while not self._stop.is_set():
            self._drain_control_tx()
            try:
                payload = self.utun.read()
            except (socket.timeout, TimeoutError):
                read_errors = 0
                continue
            except (OSError, ValueError):
                if self._stop.is_set():
                    break
                read_errors += 1
                if self._utun_read_log.allow():
                    _log.warning("utun read failed (%d in a row)", read_errors, exc_info=True)
                if read_errors >= _UTUN_ERROR_FAIL_STREAK:
                    self._mark_failed(f"tx: {read_errors} consecutive utun read errors")
                    return
                # Back off (growing) so a persistently failing utun can't
                # turn this loop into a busy-spin.
                self._stop.wait(min(_UTUN_ERROR_BACKOFF_S * read_errors, _UTUN_ERROR_BACKOFF_MAX_S))
                continue
            read_errors = 0
            if not payload:
                continue
            version = payload[0] >> 4
            if version not in (4, 6):
                self._incr_stat("drops")
                _log.debug("dropping utun packet with IP version nibble %d", version)
                continue
            ethertype = ethernet.ETH_P_IP if version == 4 else ethernet.ETH_P_IPV6
            frame = ethernet.wrap(payload, ethertype, dst=self.learner.current(), src=self.our_mac)
            self._send_frame(frame, ip_len=len(payload), wait_for_slot=True)

    def _send_frame(self, frame: bytes, ip_len: int | None = None, wait_for_slot: bool = False) -> None:
        """Submit one Ethernet ``frame``; ``ip_len`` (the IP packet's length)
        is what tx stats count once it completes -- None for a frame that
        isn't counted. (ARP replies don't come through here: see _handle_arp.)

        ``wait_for_slot`` (tx thread only): if every OUT transfer is in
        flight, wait up to _TX_SLOT_WAIT_S for one to retire instead of
        dropping at once (backpressure; see _TX_SLOT_WAIT_S). Never on the
        EventLoop thread -- that's the thread that retires transfers, so
        waiting there could only time out.
        """
        msg = self._out_msg(frame)
        if msg is None:
            return
        if wait_for_slot:
            if self._submit_waiting(msg, ip_len):
                return
        elif self._tx_pool.submit_out(msg, metadata=ip_len):
            return
        if self._stop.is_set():
            return  # shutting down: not a stall, nothing to count
        # Every OUT transfer is (still) in flight: the modem's queue (or our
        # own pool) is full. Not fatal -- counted and rate-limited, same as a
        # sync bulk_write timeout (see bridge.py).
        self._incr_stat("tx_stalls")
        self._incr_stat("drops")
        self._warn_tx_stall_rate_limited()

    def perf_snapshot(self) -> dict:
        """Instrumentation for tuning the pools (see docs/macos-driver.md,
        "Performance and tuning"): OUT submit->completion latency histogram,
        max OUT transfers in flight, tx slot waits, and frames per RX URB.
        A cheap read of counters written by other threads: values can be a
        packet behind each other, which is fine for a log line.
        """
        stats = self.stats
        return {
            "out_latency": self._tx_pool.out_latency(),
            "tx_inflight_max": self._tx_pool.out_inflight_max,
            "tx_urbs": self.tx_urbs,
            "tx_slot_waits": stats.tx_slot_waits,
            "tx_slot_wait_ms": stats.tx_slot_wait_ns / 1e6,
            "rx_urbs": stats.rx_urbs,
            "rx_frames_per_urb_mean": (stats.rx_urb_frames / stats.rx_urbs) if stats.rx_urbs else 0.0,
            "rx_frames_per_urb_max": stats.rx_urb_frames_max,
        }

    def perf_summary(self) -> str:
        """``perf_snapshot()`` as one log-friendly ``key=value`` line body."""
        snap = self.perf_snapshot()
        lat = snap["out_latency"]
        labels = [f"<{us}us" for us in lat["bounds_us"]] + [f">={lat['bounds_us'][-1]}us"]
        hist = ",".join(f"{label}:{n}" for label, n in zip(labels, lat["buckets"]))
        return (
            f"out_latency[n={lat['count']} mean={lat['mean_us']:.0f}us max={lat['max_us']:.0f}us {hist}] "
            f"tx_inflight_max={snap['tx_inflight_max']}/{snap['tx_urbs']} "
            f"tx_slot_waits={snap['tx_slot_waits']} tx_slot_wait={snap['tx_slot_wait_ms']:.1f}ms "
            f"rx_urbs={snap['rx_urbs']} rx_frames_per_urb mean={snap['rx_frames_per_urb_mean']:.2f} "
            f"max={snap['rx_frames_per_urb_max']}"
        )

    def _out_msg(self, frame: bytes) -> bytes | None:
        """Wrap ``frame`` in a PACKET_MSG ready for the OUT pool, or count a
        drop and return None if it exceeds max_transfer_size.
        """
        msg = rndis.pack_packet(frame)
        if len(msg) > self.max_transfer_size:
            self._incr_stat("drops")
            _log.debug(
                "dropping oversized frame: PACKET_MSG %d bytes > max_transfer_size %d",
                len(msg), self.max_transfer_size,
            )
            return None
        if len(msg) % self.usb.bulk_out_max_packet == 0:
            msg = msg + b"\x00"  # see usb_transport.RndisUsb.bulk_write for why
        return msg

    def _submit_waiting(self, msg: bytes, ip_len: int | None) -> bool:
        """Submit ``msg``, waiting up to _TX_SLOT_WAIT_S for a free OUT slot
        (tx thread only, see _send_frame). False if none freed up in time or
        the bridge is stopping.
        """
        with self._tx_slot_freed:
            seen = self._tx_slots_freed
        if self._tx_pool.submit_out(msg, metadata=ip_len):
            return True
        wait_start_ns = time.perf_counter_ns()
        self.stats.tx_slot_waits += 1
        try:
            deadline = time.monotonic() + _TX_SLOT_WAIT_S
            while not self._stop.is_set() and time.monotonic() < deadline:
                with self._tx_slot_freed:
                    # Only wait if no slot was freed since our last attempt
                    # (submit_out itself never runs under this lock).
                    if self._tx_slots_freed == seen:
                        self._tx_slot_freed.wait(_TX_SLOT_POLL_S)
                    seen = self._tx_slots_freed
                if self._tx_pool.submit_out(msg, metadata=ip_len):
                    return True
            return False
        finally:
            self.stats.tx_slot_wait_ns += time.perf_counter_ns() - wait_start_ns

    def _on_tx_result(self, ok: bool, _actual_length: int, ip_len: int | None) -> None:
        """Called on the EventLoop thread when a submitted OUT transfer
        retires (COMPLETED, or failed: TIMED_OUT/STALL/ERROR -- see
        AsyncEndpoint). ``ip_len``
        is the IP packet length passed as ``submit_out()``'s metadata (None
        for an ARP reply, which isn't counted), since several transfers are
        in flight at once and can retire out of submission order.
        """
        with self._tx_slot_freed:
            self._tx_slots_freed += 1
            self._tx_slot_freed.notify()
        if ok:
            if ip_len is not None:
                self._incr_stat("tx_packets")
                self._incr_stat("tx_bytes", ip_len)
        else:
            # The modem NAKed this transfer (see module docstring in
            # bridge.py: no active data session -> a handful of PACKET_MSGs
            # accepted, then USB timeouts until the session recovers).
            self._incr_stat("tx_stalls")
            self._incr_stat("tx_timeouts")
            self._incr_stat("drops")
            self._warn_tx_stall_rate_limited()

    def _warn_tx_stall_rate_limited(self) -> None:
        self._tx_stall_warn_count_since += 1
        now = time.monotonic()
        if now - self._tx_stall_warn_last >= _TX_STALL_WARN_INTERVAL_S:
            _log.warning(
                "tx stalled (no active data session?): %d timeouts since last warning (total tx_stalls=%d)",
                self._tx_stall_warn_count_since, self.stats.tx_stalls,
            )
            self._tx_stall_warn_last = now
            self._tx_stall_warn_count_since = 0

    # --- control: a 1-deep async interrupt-IN transfer signals this thread,
    # which does a single GET_ENCAPSULATED_RESPONSE (never polls without a
    # notification -- see rndis_device.py) and sends our own keepalive -------

    def _on_control_notify(self, data: bytes) -> None:
        """AsyncEndpoint callback (runs on the EventLoop thread). Only a
        RESPONSE_AVAILABLE notification counts (the RNDIS 8-byte form the
        FM350 sends, or the CDC A1 01 form -- see
        usb_transport.is_response_available) -- each one is answered by
        exactly one GET_ENCAPSULATED_RESPONSE. Anything else (zero-length
        notifications, the 16-byte CONNECTION_SPEED_CHANGE) is logged and
        ignored: fetching on it would poll the firmware with nothing pending.
        """
        if not is_response_available(data):
            _log.debug("ignoring non-RESPONSE_AVAILABLE notification: %s", bytes(data).hex())
            return
        with self._notify_cond:
            if self._pending_responses < _MAX_PENDING_RESPONSES:
                self._pending_responses += 1
            self._notify_cond.notify()

    def _take_pending_response(self, timeout: float) -> bool:
        """Block up to ``timeout`` for a pending RESPONSE_AVAILABLE and
        consume one count. Truly blocks (a condition wait), so it can't spin.
        """
        with self._notify_cond:
            if self._pending_responses == 0 and not self._stop.is_set():
                self._notify_cond.wait(timeout)
            if self._pending_responses > 0:
                self._pending_responses -= 1
                return True
            return False

    def _control_loop(self) -> None:
        last_keepalive = 0.0
        while not self._stop.is_set():
            now = time.monotonic()
            silent_s = now - self._last_keepalive_ack
            if silent_s > _KEEPALIVE_MISSED_LIMIT * _KEEPALIVE_INTERVAL_S:
                self._mark_failed(
                    f"control: no successful KEEPALIVE_CMPLT for {silent_s:.0f}s "
                    f"({_KEEPALIVE_MISSED_LIMIT} keepalive intervals): device control path unresponsive"
                )
                return
            if now - last_keepalive >= _KEEPALIVE_INTERVAL_S:
                self._send_keepalive()
                last_keepalive = now

            if not self._take_pending_response(_CONTROL_NOTIFY_TIMEOUT_S):
                continue
            if self._stop.is_set():
                return

            try:
                msg = self.usb.get_encapsulated()
            except UsbError as exc:
                if _is_device_gone(exc):
                    self._mark_failed(f"control: get_encapsulated: device disconnected ({exc})", device_lost=True)
                    return
                if self._control_log.allow():
                    _log.exception("control: get_encapsulated failed (further failures rate-limited)")
                continue
            if rndis.is_empty_response(msg):
                # RNDIS spec: a 1-byte 0x00 reply means "no response available".
                _log.debug("control: nothing pending (%d-byte reply)", len(msg))
                continue
            try:
                self._handle_control_msg(msg)
            except UsbError as exc:
                if _is_device_gone(exc):
                    self._mark_failed(f"control: send_encapsulated: device disconnected ({exc})", device_lost=True)
                    return
                if self._control_log.allow():
                    _log.exception("control: failed to handle message (further failures rate-limited)")
            except (rndis.RndisError, ValueError):
                if self._control_log.allow():
                    _log.exception("control: failed to handle message (further failures rate-limited)")

    def _send_keepalive(self) -> None:
        self._keepalive_request_id += 1
        try:
            self.usb.send_encapsulated(rndis.pack_keepalive(self._keepalive_request_id))
        except UsbError as exc:
            if _is_device_gone(exc):
                self._mark_failed(f"control: keepalive send: device disconnected ({exc})", device_lost=True)
            elif self._control_log.allow():
                _log.exception("control: keepalive send failed (further failures rate-limited)")

    def _handle_control_msg(self, msg: bytes) -> None:
        msg_type = rndis.message_type(msg)
        if msg_type == rndis.KEEPALIVE:
            request_id = rndis.parse_keepalive(msg)
            self.usb.send_encapsulated(rndis.pack_keepalive_cmplt(request_id))
        elif msg_type == rndis.INDICATE_STATUS:
            status = rndis.parse_indicate_status(msg)
            _log.info("RNDIS INDICATE_STATUS status=%#x", status.status)
        elif msg_type == rndis.KEEPALIVE_CMPLT:
            cmplt = rndis.parse_keepalive_cmplt(msg)
            if cmplt.status != 0:
                # Not an ack: the keepalive watchdog fails the bridge only if
                # no successful one arrives within its limit, so a single
                # error status doesn't tear the session down.
                _log.warning(
                    "control: KEEPALIVE_CMPLT status=%#x: device reports an error; not counted as an ack",
                    cmplt.status,
                )
                return
            self._last_keepalive_ack = time.monotonic()
            _log.debug("our keepalive was acknowledged")
