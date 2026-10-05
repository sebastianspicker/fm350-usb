"""RNDIS control state machine on top of usb_transport.RndisUsb.

Handles request/response matching by RequestID, answers device KEEPALIVE
messages while waiting for our own completions, and logs INDICATE_STATUS
notifications.
"""

from __future__ import annotations

import logging
import time

from . import rndis
from .usb_transport import RndisUsb

_log = logging.getLogger(__name__)

_MAX_FALLBACK_POLLS = 3
_FALLBACK_POLL_INTERVAL = 0.1


class RndisDevice:
    """High-level RNDIS control operations against an FM350-GL over USB."""

    def __init__(self, usb: RndisUsb) -> None:
        self.usb = usb
        self._request_id = 0

    def _next_request_id(self) -> int:
        self._request_id += 1
        return self._request_id

    def _get_response(self, request_id: int, expect_type: int, timeout: int = 2000) -> bytes:
        """Send-then-wait is done by the caller; this only fetches and demuxes
        whatever the device currently has queued, handling KEEPALIVE and
        INDICATE_STATUS messages that arrive out of band, and discarding any
        completion that doesn't match both ``expect_type`` and ``request_id``
        (e.g. a stale reply to an earlier, already-timed-out request), until
        a matching completion is seen or ``timeout`` (in ms) elapses.

        Live testing found that hammering GET_ENCAPSULATED_RESPONSE with
        nothing pending (thousands of calls with no interrupt notification in
        between) crashed the FM350's firmware: control transfers timed out,
        the device disappeared from USB, and it took 60-90s to re-enumerate.
        So ``get_encapsulated`` is only called right after a real interrupt
        notification, plus at most a few fallback polls (spaced out, never in
        a tight loop) for the case where the device skips the notification
        but the response can still be fetched directly.
        """
        deadline = time.monotonic() + timeout / 1000
        fallback_polls_left = _MAX_FALLBACK_POLLS
        # Each wait is bounded so the fallback polls can actually happen
        # before the deadline: the first one only after a full
        # _FALLBACK_POLL_INTERVAL without any notification, later ones at
        # least that far apart.
        last_fetch = time.monotonic()
        while True:
            now = time.monotonic()
            remaining = deadline - now
            if remaining <= 0:
                raise TimeoutError("no matching RNDIS response from device")
            wait_s = remaining
            if fallback_polls_left > 0:
                wait_s = min(remaining, max(0.001, last_fetch + _FALLBACK_POLL_INTERVAL - now))
            notified = self.usb.wait_notify(max(1, int(wait_s * 1000)))

            if notified is not None:
                msg = self.usb.get_encapsulated()
            elif fallback_polls_left > 0 and time.monotonic() - last_fetch >= _FALLBACK_POLL_INTERVAL - 0.005:
                fallback_polls_left -= 1
                msg = self.usb.get_encapsulated()
            else:
                continue  # keep waiting for a notification; no unsolicited polling
            last_fetch = time.monotonic()
            if rndis.is_empty_response(msg):
                # RNDIS spec: a 1-byte 0x00 reply means "no response available".
                _log.debug("GET_ENCAPSULATED_RESPONSE: nothing pending (%d-byte reply)", len(msg))
                continue

            msg_type = rndis.message_type(msg)
            if msg_type == rndis.KEEPALIVE:
                device_request_id = rndis.parse_keepalive(msg)
                _log.debug("device KEEPALIVE request_id=%d, replying", device_request_id)
                self.usb.send_encapsulated(rndis.pack_keepalive_cmplt(device_request_id))
                continue
            if msg_type == rndis.INDICATE_STATUS:
                status = rndis.parse_indicate_status(msg)
                _log.info("RNDIS INDICATE_STATUS status=%#x", status.status)
                continue
            if msg_type == expect_type:
                msg_request_id = rndis.peek_request_id(msg)
                if msg_request_id == request_id:
                    return msg
                _log.debug(
                    "discarding stale RNDIS completion type=%#x request_id=%d (expected %d)",
                    msg_type, msg_request_id, request_id,
                )
                continue
            _log.debug("ignoring unexpected RNDIS message type %#x while waiting for %#x", msg_type, expect_type)

    def initialize(self, max_transfer: int = 0x4000) -> rndis.InitCmplt:
        """Send REMOTE_NDIS_INITIALIZE_MSG and return the parsed completion."""
        request_id = self._next_request_id()
        self.usb.send_encapsulated(rndis.pack_init(request_id, max_transfer))
        msg = self._get_response(request_id, rndis.INIT_CMPLT)
        return rndis.parse_init_cmplt(msg)

    def query(self, oid: int) -> bytes:
        """Send REMOTE_NDIS_QUERY_MSG for ``oid`` and return the response buffer."""
        request_id = self._next_request_id()
        self.usb.send_encapsulated(rndis.pack_query(request_id, oid))
        msg = self._get_response(request_id, rndis.QUERY_CMPLT)
        return rndis.parse_query_cmplt(msg).buffer

    def set(self, oid: int, value: bytes) -> None:
        """Send REMOTE_NDIS_SET_MSG for ``oid`` with ``value`` and wait for the completion."""
        request_id = self._next_request_id()
        self.usb.send_encapsulated(rndis.pack_set(request_id, oid, value))
        msg = self._get_response(request_id, rndis.SET_CMPLT)
        rndis.parse_set_cmplt(msg)

    def mac(self) -> bytes:
        """Query OID_802_3_CURRENT_ADDRESS (6 bytes). Raises RndisError if
        the device answers with any other length.
        """
        value = self.query(rndis.OID_802_3_CURRENT_ADDRESS)
        if len(value) != 6:
            raise rndis.RndisError(rndis.STATUS_INVALID_DATA, f"MAC address is {len(value)} bytes, expected 6")
        return value

    def set_packet_filter(self, filter_value: int = rndis.PACKET_FILTER_DEFAULT) -> None:
        """Set OID_GEN_CURRENT_PACKET_FILTER (default: directed|multicast|broadcast)."""
        self.set(rndis.OID_GEN_CURRENT_PACKET_FILTER, filter_value.to_bytes(4, "little"))

    def halt(self) -> None:
        """Send REMOTE_NDIS_HALT_MSG. No completion is expected."""
        request_id = self._next_request_id()
        self.usb.send_encapsulated(rndis.pack_halt(request_id))
