"""The rx/tx data pump between the RNDIS bulk endpoints and a utun interface.

rx thread: bulk IN -> RNDIS PACKET_MSG decode -> Ethernet strip -> IPv4/IPv6
goes to utun, ARP requests for our IP get answered, everything else is
dropped. tx thread: utun read -> Ethernet wrap (dst = learned peer MAC,
src = device MAC) -> PACKET_MSG -> bulk OUT. A third, low-rate thread answers
device keepalives and sends our own every 5 s on the control channel.

Each thread is guarded: USB errors are retried with a short backoff, a
disconnected device (or too many consecutive errors) is treated as fatal and
recorded on ``failed``/``failure_reason``, and any unexpected exception in a
thread is logged and also marks the bridge failed rather than dying silently.

Live testing found that without an active data session (no PDP/bearer) the
FM350 accepts a handful of PACKET_MSGs on bulk OUT and then NAKs every
further write (a USB timeout) until the session recovers -- e.g. during a
mobile-network outage or reconnect. That's not a fatal USB error: it's
counted separately (``stats.tx_stalls``), logged at a rate limit instead of
per-packet, and the bulk-write timeout is shortened while stalled so a dead
link doesn't leave stale utun packets queued up for hundreds of ms each.
"""

from __future__ import annotations

import logging
import socket
import threading
import time
from dataclasses import dataclass

from . import ethernet, rndis
from .usb_async import UsbError, UsbNoDevice, UsbTimeout
from .usb_transport import RndisUsb
from .utun import Utun

_log = logging.getLogger(__name__)

_BULK_READ_SIZE = 0x4000
_BULK_TIMEOUT_MS = 500
_UTUN_TIMEOUT_S = 0.5
_KEEPALIVE_INTERVAL_S = 5.0
_CONTROL_POLL_TIMEOUT_MS = 500
_JOIN_TIMEOUT_S = 2.0
_ERROR_RETRY_DELAY_S = 0.1
_MAX_CONSECUTIVE_ERRORS = 50
_TX_STALL_TIMEOUT_MS = 50  # short write timeout while tx is stalled, so utun doesn't back up
_TX_STALL_WARN_INTERVAL_S = 10.0
# If a "wait for a notification" call returns in much less than the timeout
# it was given, several times in a row, treat it as not really blocking
# (e.g. the loopback transport) and sleep out the rest of
# the interval ourselves -- otherwise the control thread busy-spins and
# starves the rx/tx threads of the GIL between Python's thread-switch
# checks (measured: 18 ms median echo RTT instead of <1 ms in --loopback).
_FAST_RETURN_FRACTION = 0.1
_FAST_RETURN_STREAK_LIMIT = 3


def _is_device_gone(exc: UsbError) -> bool:
    """True if ``exc`` indicates the USB device was physically disconnected."""
    return isinstance(exc, UsbNoDevice)


@dataclass
class BridgeStats:
    """Running counters for the data pump."""

    rx_packets: int = 0
    rx_bytes: int = 0
    tx_packets: int = 0
    tx_bytes: int = 0
    drops: int = 0
    tx_stalls: int = 0


class Bridge:
    """Bridges RNDIS Ethernet frames to/from a utun interface."""

    def __init__(
        self,
        rndis_usb: RndisUsb,
        utun: Utun,
        our_mac: bytes,
        our_ip: bytes,
        max_transfer_size: int,
    ) -> None:
        self.usb = rndis_usb
        self.utun = utun
        self.our_mac = our_mac
        self._our_ip_lock = threading.Lock()
        self._our_ip = our_ip
        self.max_transfer_size = max_transfer_size
        self.learner = ethernet.PeerMacLearner()
        self.stats = BridgeStats()
        self.failed = threading.Event()
        self.failure_reason: str | None = None
        self.device_lost = False
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._keepalive_request_id = 0
        self._consecutive_errors = 0
        self._tx_stalled = False
        self._tx_stall_warn_last = 0.0
        self._tx_stall_warn_count_since = 0

    @property
    def our_ip(self) -> bytes:
        """The IP address the ARP responder answers for (thread-safe)."""
        with self._our_ip_lock:
            return self._our_ip

    def set_our_ip(self, ip: str) -> None:
        """Update the IP the ARP responder answers for (thread-safe).

        Called by the reconnect supervisor after the modem assigns a new IP.
        """
        with self._our_ip_lock:
            self._our_ip = socket.inet_aton(ip)

    def start(self) -> None:
        """Start the rx, tx and control threads."""
        self._stop.clear()
        self.failed.clear()
        self.failure_reason = None
        self.device_lost = False
        self._consecutive_errors = 0
        self.utun.settimeout(_UTUN_TIMEOUT_S)
        loops = (
            ("fm350mac-rx", self._rx_loop),
            ("fm350mac-tx", self._tx_loop),
            ("fm350mac-control", self._control_loop),
        )
        self._threads = [
            threading.Thread(target=self._run_guarded, args=(loop, name), name=name, daemon=True)
            for name, loop in loops
        ]
        for t in self._threads:
            t.start()

    def stop(self) -> bool:
        """Signal all threads to stop and wait for them to exit.

        Returns True if every thread exited within the join timeout, False
        if any is still alive -- in that case the caller must not reuse the
        USB handle (e.g. for RNDIS HALT) from another context, since it
        would race the still-running thread on the same device handle.
        """
        self._stop.set()
        all_stopped = True
        for t in self._threads:
            t.join(timeout=_JOIN_TIMEOUT_S)
            if t.is_alive():
                all_stopped = False
                _log.warning("thread %s did not exit within %.1fs", t.name, _JOIN_TIMEOUT_S)
        return all_stopped

    def _run_guarded(self, target, name: str) -> None:
        """Thread entry point: run ``target`` and turn any escaping exception
        into a failure instead of a silently-dead thread.
        """
        try:
            target()
        except Exception:
            _log.exception("%s crashed", name)
            self._mark_failed(f"{name}: unexpected exception")

    def _mark_failed(self, reason: str, device_lost: bool = False) -> None:
        """Record a fatal condition and signal every loop to stop."""
        if not self.failed.is_set():
            self.failure_reason = reason
            _log.error("bridge failed: %s", reason)
        if device_lost:
            self.device_lost = True
        self.failed.set()
        self._stop.set()

    def _note_usb_error(self, exc: UsbError, context: str) -> bool:
        """Handle a UsbError seen in any loop.

        Returns True if the caller's loop should stop now (device physically
        gone, or too many consecutive errors -- treated as fatal too).
        """
        if _is_device_gone(exc):
            self._mark_failed(f"{context}: device disconnected ({exc})", device_lost=True)
            return True
        self._consecutive_errors += 1
        _log.exception("%s failed (%d consecutive errors)", context, self._consecutive_errors)
        if self._consecutive_errors >= _MAX_CONSECUTIVE_ERRORS:
            self._mark_failed(f"{context}: {self._consecutive_errors} consecutive USB errors")
            return True
        time.sleep(_ERROR_RETRY_DELAY_S)
        return False

    def _note_usb_ok(self) -> None:
        self._consecutive_errors = 0

    # --- rx: RNDIS bulk IN -> utun ------------------------------------------

    def _rx_loop(self) -> None:
        while not self._stop.is_set():
            try:
                buf = self.usb.bulk_read(_BULK_READ_SIZE, timeout=_BULK_TIMEOUT_MS)
            except UsbTimeout:
                continue
            except UsbError as exc:
                if self._note_usb_error(exc, "rx: bulk_read"):
                    return
                continue
            self._note_usb_ok()
            for frame in rndis.unpack_packets(buf):
                self._handle_frame(frame)

    def _handle_frame(self, frame: bytes) -> None:
        try:
            ethertype, src_mac, payload = ethernet.strip(frame)
        except ValueError:
            self.stats.drops += 1
            return
        self.stats.rx_packets += 1
        self.stats.rx_bytes += len(frame)
        if ethertype in (ethernet.ETH_P_IP, ethernet.ETH_P_IPV6):
            self.learner.observe(ethertype, src_mac)
            try:
                self.utun.write(payload)
            except OSError:
                _log.exception("utun write failed")
        elif ethertype == ethernet.ETH_P_ARP:
            self._handle_arp(payload)
        else:
            self.stats.drops += 1

    def _handle_arp(self, payload: bytes) -> None:
        try:
            arp = ethernet.parse_arp(payload)
        except ValueError:
            self.stats.drops += 1
            return
        if arp.oper != ethernet.ARP_REQUEST or arp.tpa != self.our_ip:
            return
        reply_payload = ethernet.build_arp_reply(arp, self.our_mac, self.our_ip)
        frame = ethernet.wrap(reply_payload, ethernet.ETH_P_ARP, dst=arp.sha, src=self.our_mac)
        self._send_frame(frame)

    # --- tx: utun -> RNDIS bulk OUT ------------------------------------------

    def _tx_loop(self) -> None:
        while not self._stop.is_set():
            try:
                payload = self.utun.read()
            except (socket.timeout, TimeoutError):
                continue
            except (OSError, ValueError):
                if self._stop.is_set():
                    break
                _log.exception("utun read failed")
                continue
            if not payload:
                continue
            version = payload[0] >> 4
            ethertype = ethernet.ETH_P_IP if version == 4 else ethernet.ETH_P_IPV6
            frame = ethernet.wrap(payload, ethertype, dst=self.learner.current(), src=self.our_mac)
            self._send_frame(frame)

    def _send_frame(self, frame: bytes) -> None:
        msg = rndis.pack_packet(frame)
        if len(msg) > self.max_transfer_size:
            self.stats.drops += 1
            _log.debug(
                "dropping oversized frame: PACKET_MSG %d bytes > max_transfer_size %d",
                len(msg), self.max_transfer_size,
            )
            return
        timeout = _TX_STALL_TIMEOUT_MS if self._tx_stalled else _BULK_TIMEOUT_MS
        try:
            self.usb.bulk_write(msg, timeout=timeout)
        except UsbTimeout:
            # Not a fatal USB error: the modem NAKs bulk OUT like this whenever
            # there's no active data session (see module docstring). Counted
            # and rate-limited, not retried at the full 500 ms timeout.
            self.stats.tx_stalls += 1
            self.stats.drops += 1
            self._tx_stalled = True
            self._warn_tx_stall_rate_limited()
            return
        except UsbError as exc:
            if _is_device_gone(exc):
                self._mark_failed(f"tx: bulk_write: device disconnected ({exc})", device_lost=True)
            else:
                _log.exception("bulk_write failed")
            return
        self._tx_stalled = False
        self.stats.tx_packets += 1
        self.stats.tx_bytes += len(frame)

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

    # --- control: device keepalive reply + our own keepalive tick -----------

    def _control_loop(self) -> None:
        last_keepalive = 0.0
        fast_return_streak = 0
        while not self._stop.is_set():
            now = time.monotonic()
            if now - last_keepalive >= _KEEPALIVE_INTERVAL_S:
                self._send_keepalive()
                last_keepalive = now

            wait_start = time.monotonic()
            try:
                notify = self.usb.wait_notify(timeout=_CONTROL_POLL_TIMEOUT_MS)
            except UsbError as exc:
                if self._note_usb_error(exc, "control: wait_notify"):
                    return
                continue
            elapsed = time.monotonic() - wait_start

            # Defensive: a transport whose wait_notify doesn't actually block
            # (e.g. a misbehaving fake) would otherwise make this loop
            # busy-spin and starve the rx/tx threads of the GIL. If it keeps
            # returning much faster than the timeout we gave it, sleep out
            # the rest ourselves.
            if elapsed < _CONTROL_POLL_TIMEOUT_MS / 1000 * _FAST_RETURN_FRACTION:
                fast_return_streak += 1
                if fast_return_streak >= _FAST_RETURN_STREAK_LIMIT:
                    time.sleep(_CONTROL_POLL_TIMEOUT_MS / 1000 - elapsed)
            else:
                fast_return_streak = 0

            if notify is None:
                continue

            try:
                msg = self.usb.get_encapsulated()
            except UsbError as exc:
                if self._note_usb_error(exc, "control: get_encapsulated"):
                    return
                continue
            if not msg:
                continue

            try:
                self._handle_control_msg(msg)
            except (UsbError, rndis.RndisError, ValueError):
                _log.exception("control: failed to handle message")
                continue
            self._note_usb_ok()

    def _send_keepalive(self) -> None:
        self._keepalive_request_id += 1
        try:
            self.usb.send_encapsulated(rndis.pack_keepalive(self._keepalive_request_id))
        except UsbError as exc:
            self._note_usb_error(exc, "control: keepalive send")

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
