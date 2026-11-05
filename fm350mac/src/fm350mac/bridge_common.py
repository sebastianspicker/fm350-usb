"""Pieces shared by the sync ``Bridge`` and the ``AsyncBridge``.

Tunable constants stay in each bridge module (tests patch them there); only
``_TX_STALL_WARN_INTERVAL_S``, used solely by the shared stall warning, lives here.
"""

from __future__ import annotations

import socket
import time

from . import ethernet
from .usb_async import UsbError, UsbNoDevice

_TX_STALL_WARN_INTERVAL_S = 10.0


def _is_device_gone(exc: UsbError) -> bool:
    """True if ``exc`` indicates the USB device was physically disconnected."""
    return isinstance(exc, UsbNoDevice)


class BridgeCommon:
    """Mixin: our-IP accessors, ARP reply building and the tx-stall warning.

    The host class provides ``_our_ip``, ``_our_ip_lock``, ``our_mac``, ``stats``, the ``_tx_stall_warn_*``
    fields and a ``_log`` (so records keep the host module's logger name).
    """

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

    def _arp_reply_frame(self, payload: bytes) -> bytes | None:
        """The Ethernet frame answering an ARP request for our IP, else None. Raises ValueError if malformed."""
        arp = ethernet.parse_arp(payload)
        if arp.oper != ethernet.ARP_REQUEST or arp.tpa != self.our_ip:
            return None
        reply_payload = ethernet.build_arp_reply(arp, self.our_mac, self.our_ip)
        return ethernet.wrap(reply_payload, ethernet.ETH_P_ARP, dst=arp.sha, src=self.our_mac)

    def _warn_tx_stall_rate_limited(self) -> None:
        self._tx_stall_warn_count_since += 1
        now = time.monotonic()
        if now - self._tx_stall_warn_last >= _TX_STALL_WARN_INTERVAL_S:
            self._log.warning(
                "tx stalled (no active data session?): %d timeouts since last warning (total tx_stalls=%d)",
                self._tx_stall_warn_count_since, self.stats.tx_stalls,
            )
            self._tx_stall_warn_last = now
            self._tx_stall_warn_count_since = 0
