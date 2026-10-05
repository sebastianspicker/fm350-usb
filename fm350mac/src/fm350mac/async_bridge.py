"""AsyncBridge: the async-I/O sibling of bridge.Bridge, keeping several
bulk transfers in flight per direction via usb_async's libusb async
transfer pools instead of one synchronous transfer per packet.

Same public API as Bridge (start/stop/failed/failure_reason/stats/
set_our_ip/our_ip/device_lost), so cli.py's ``up --io async|sync`` can use
either. See docs/macos-driver.md, "New data path AsyncBridge", for the design.
"""

from __future__ import annotations

import logging
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
        # stats are touched from both the tx thread
        # (_send_frame's pool-exhausted path) and the EventLoop thread
        # (_on_rx_complete/_handle_arp, _on_tx_result): a bare += is not
        # atomic (LOAD/ADD/STORE), so concurrent increments from two threads
        # can lose an update without this lock.
        self._stats_lock = threading.Lock()

        device = rndis_usb.usb_device
        self._event_loop = EventLoop(device.libusb, device.ctx, usb_device=device)
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
        self.utun.settimeout(_UTUN_TIMEOUT_S)
        with self._notify_cond:
            self._pending_responses = 0
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
        self._send_frame(frame)

    # --- tx: utun -> async OUT pool ------------------------------------------

    def _tx_loop(self) -> None:
        read_errors = 0
        while not self._stop.is_set():
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
        is what tx stats count once it completes -- None for our ARP replies.

        ``wait_for_slot`` (tx thread only): if every OUT transfer is in
        flight, wait up to _TX_SLOT_WAIT_S for one to retire instead of
        dropping at once (backpressure; see _TX_SLOT_WAIT_S). Never on the
        EventLoop thread -- that's the thread that retires transfers, so
        waiting there could only time out.
        """
        msg = rndis.pack_packet(frame)
        if len(msg) > self.max_transfer_size:
            self._incr_stat("drops")
            _log.debug(
                "dropping oversized frame: PACKET_MSG %d bytes > max_transfer_size %d",
                len(msg), self.max_transfer_size,
            )
            return
        if len(msg) % self.usb.bulk_out_max_packet == 0:
            msg = msg + b"\x00"  # see usb_transport.RndisUsb.bulk_write for why
        with self._tx_slot_freed:
            seen = self._tx_slots_freed
        if self._tx_pool.submit_out(msg, metadata=ip_len):
            return
        if wait_for_slot:
            deadline = time.monotonic() + _TX_SLOT_WAIT_S
            while not self._stop.is_set() and time.monotonic() < deadline:
                with self._tx_slot_freed:
                    # Only wait if no slot was freed since our last attempt
                    # (submit_out itself never runs under this lock).
                    if self._tx_slots_freed == seen:
                        self._tx_slot_freed.wait(_TX_SLOT_POLL_S)
                    seen = self._tx_slots_freed
                if self._tx_pool.submit_out(msg, metadata=ip_len):
                    return
        # Every OUT transfer is (still) in flight: the modem's queue (or our
        # own pool) is full. Not fatal -- counted and rate-limited, same as a
        # sync bulk_write timeout (see bridge.py).
        self._incr_stat("tx_stalls")
        self._incr_stat("drops")
        self._warn_tx_stall_rate_limited()

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
                _log.exception("control: get_encapsulated failed")
                continue
            if rndis.is_empty_response(msg):
                # RNDIS spec: a 1-byte 0x00 reply means "no response available".
                _log.debug("control: nothing pending (%d-byte reply)", len(msg))
                continue
            try:
                self._handle_control_msg(msg)
            except (UsbError, rndis.RndisError, ValueError):
                _log.exception("control: failed to handle message")

    def _send_keepalive(self) -> None:
        self._keepalive_request_id += 1
        try:
            self.usb.send_encapsulated(rndis.pack_keepalive(self._keepalive_request_id))
        except UsbError as exc:
            if _is_device_gone(exc):
                self._mark_failed(f"control: keepalive send: device disconnected ({exc})", device_lost=True)
            else:
                _log.exception("control: keepalive send failed")

    def _handle_control_msg(self, msg: bytes) -> None:
        msg_type = rndis.message_type(msg)
        if msg_type == rndis.KEEPALIVE:
            request_id = rndis.parse_keepalive(msg)
            self.usb.send_encapsulated(rndis.pack_keepalive_cmplt(request_id))
        elif msg_type == rndis.INDICATE_STATUS:
            status = rndis.parse_indicate_status(msg)
            _log.info("RNDIS INDICATE_STATUS status=%#x", status.status)
        elif msg_type == rndis.KEEPALIVE_CMPLT:
            _log.debug("our keepalive was acknowledged")
