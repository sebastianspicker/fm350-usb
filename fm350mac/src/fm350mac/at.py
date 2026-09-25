"""AT command channel to the FM350-GL's vendor-specific serial interface,
sent over raw USB bulk transfers (macOS has no serial driver for it).

Moved from ``tools/fm350_at.py``; see that script for the original standalone
CLI, now a thin wrapper around ``AtPort``.
"""

from __future__ import annotations

import ipaddress
import re
import time

from .usb_async import UsbDevice, UsbTimeout, open_device

PIDS = {0x7127: 6, 0x7126: 4}  # USB product id -> AT interface number

# A final result code ends the response; matched as soon as it appears so
# command() doesn't keep draining (with 300 ms read timeouts) after the
# modem is already done -- a plain "AT" measured a 327 ms round trip before
# this, almost all of it spent waiting out one extra drain past "\r\nOK\r\n".
_FINAL_RESULT_RE = re.compile(r"\r\nOK\r\n|\r\nERROR\r\n|\+CME ERROR:[^\r\n]*\r\n|\+CMS ERROR:[^\r\n]*\r\n")
_DRAIN_TIMEOUT_MS = 50


class AtPort:
    """An open AT command channel on the FM350's vendor serial interface.

    ``usb_device`` is the shared UsbDevice also used by the RNDIS interfaces
    (see usb_transport.py); if not given, one is opened here and owned by
    this AtPort -- ``close()`` then closes it too. When a shared device is
    passed in explicitly, ``close()`` only releases this port's interface,
    leaving the device open for whoever else is using it.
    """

    def __init__(self, usb_device: UsbDevice | None = None, iface_override: int | None = None) -> None:
        self._owns_device = usb_device is None
        self.usb_device = usb_device if usb_device is not None else open_device()
        default_iface = PIDS.get(self.usb_device.pid)
        if default_iface is None and iface_override is None:
            raise RuntimeError(f"unknown AT interface for pid {self.usb_device.pid:#06x}")
        self.iface_num = default_iface if iface_override is None else iface_override
        self.usb_device.claim_interface(self.iface_num)
        self.ep_out, _ = self.usb_device.find_endpoint(self.iface_num, "out")
        self.ep_in, self.ep_in_max_packet = self.usb_device.find_endpoint(self.iface_num, "in", "bulk")
        self._drain(200)  # discard unsolicited output left over from boot

    def _drain(self, timeout_ms: int = 200) -> str:
        out = b""
        while True:
            try:
                out += self.usb_device.bulk_in(self.ep_in, self.ep_in_max_packet * 8, timeout_ms=timeout_ms)
            except UsbTimeout:
                return out.decode(errors="replace")

    def command(self, cmd: str, timeout: float = 240.0) -> str:
        """Send an AT command and return the response text.

        Returns as soon as the buffer contains a final result code (rather
        than always draining for the full per-read timeout first), using
        short per-read timeouts so the common case is fast.
        """
        self.usb_device.bulk_out(self.ep_out, (cmd + "\r").encode())
        buf = ""
        deadline = time.time() + timeout
        while time.time() < deadline:
            buf += self._drain(_DRAIN_TIMEOUT_MS)
            if _FINAL_RESULT_RE.search(buf):
                break
        return buf.strip()

    def close(self) -> None:
        """Release the AT interface, and the shared UsbDevice if this AtPort opened it."""
        self.usb_device.release_interface(self.iface_num)
        if self._owns_device:
            self.usb_device.close()

    def __enter__(self) -> "AtPort":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()


# --- Pure response parsers (no I/O, unit-testable) -------------------------


def parse_cgpaddr(response: str) -> str | None:
    """Parse ``+CGPADDR: <cid>,"a.b.c.d"`` (quotes optional) from an AT response."""
    match = re.search(r"\+CGPADDR:\s*\d+\s*,\s*\"?(\d{1,3}(?:\.\d{1,3}){3})\"?", response)
    return match.group(1) if match else None


def parse_gtdns(response: str) -> list[str]:
    """Parse ``+GTDNS: <cid>,"primary","secondary"`` (quotes/spaces optional).

    Returns the DNS server addresses found, in order, skipping empty or
    unspecified (``0.0.0.0``) entries.
    """
    match = re.search(
        r"\+GTDNS:\s*\d+\s*,\s*\"?(\d{1,3}(?:\.\d{1,3}){3})?\"?"
        r"\s*(?:,\s*\"?(\d{1,3}(?:\.\d{1,3}){3})?\"?)?",
        response,
    )
    if not match:
        return []
    return [ip for ip in match.groups() if ip and ip != "0.0.0.0"]


def parse_cpin(response: str) -> bool:
    """Return True if an ``AT+CPIN?`` response reports ``READY``."""
    return "+CPIN: READY" in response


_REGISTERED_STATS = frozenset({1, 5})  # 1 = registered home, 5 = registered roaming


def parse_registration(response: str) -> int | None:
    """Parse the ``<stat>`` field from an ``AT+CEREG?``/``AT+C5GREG?`` response.

    Returns None both when the response doesn't contain a recognisable
    +CEREG/+C5GREG line at all, and when it does but ``<stat>`` is absent --
    which happens when the modem is in unsolicited-report-only mode (``<n>``
    reported alone, e.g. ``+C5GREG: 0``, seen live on this modem for
    C5GREG). Callers that need to tell "unsupported/no stat reported" apart
    from "unparseable" should always poll both CEREG and C5GREG rather than
    inspecting this return value alone (see supervisor._poll_registration).
    """
    match = re.search(r"\+C(?:E|5G)REG:\s*\d+\s*,\s*(\d+)", response)
    return int(match.group(1)) if match else None


def is_registered(stat: int | None) -> bool:
    """True if a CEREG/C5GREG ``<stat>`` value means registered (home or roaming)."""
    return stat in _REGISTERED_STATS


# --- Input validation (allow-list; the values end up inside a quoted AT
# command string, so anything that could break out of the quotes or inject
# a `\r`-terminated second command must be rejected before it gets there) ---

_APN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.\-_]{0,99}$")
VALID_PDP_TYPES = frozenset({"IP", "IPV6", "IPV4V6"})


def validate_apn(apn: str) -> str:
    """Validate an APN against a conservative allow-list. Raises ValueError otherwise."""
    if not _APN_RE.match(apn):
        raise ValueError(f"invalid APN: {apn!r}")
    return apn


def validate_pdp_type(pdp_type: str) -> str:
    """Validate a PDP type (IP, IPV6 or IPV4V6). Raises ValueError otherwise."""
    if pdp_type not in VALID_PDP_TYPES:
        raise ValueError(f"invalid PDP type: {pdp_type!r} (expected one of {sorted(VALID_PDP_TYPES)})")
    return pdp_type


# --- AT command helpers -----------------------------------------------------


def sim_ready(port: AtPort) -> bool:
    """Send AT+CPIN? and return whether the SIM reports READY."""
    return parse_cpin(port.command("AT+CPIN?"))


def define_pdp(port: AtPort, cid: int, pdp_type: str, apn: str) -> str:
    """Send AT+CGDCONT to define a PDP context.

    Validates ``pdp_type`` and ``apn`` first (raises ValueError), so
    untrusted input can never be injected into the AT command string.
    """
    validate_pdp_type(pdp_type)
    validate_apn(apn)
    return port.command(f'AT+CGDCONT={cid},"{pdp_type}","{apn}"')


def activate(port: AtPort, cid: int) -> str:
    """Send AT+CGACT=1,<cid> to activate a PDP context."""
    return port.command(f"AT+CGACT=1,{cid}")


def deactivate(port: AtPort, cid: int) -> str:
    """Send AT+CGACT=0,<cid> to deactivate a PDP context."""
    return port.command(f"AT+CGACT=0,{cid}")


def valid_assigned_ipv4(value: str | None) -> str | None:
    """Return ``value`` if it's a plausible IPv4 address assigned to us by
    the network, else None.

    ``parse_cgpaddr``'s regex only checks the shape of the field (1-3
    digits per octet), so e.g. a malformed/malicious ``+CGPADDR:
    1,"999.1.1.1"`` would otherwise pass through as a string. This is the
    single place that turns modem-controlled text into something safe to
    hand to ``ifconfig``/``route``/the RNDIS bridge's ARP responder:
    rejects anything that isn't a real unicast address (not unspecified,
    multicast, loopback or link-local).
    """
    if value is None:
        return None
    try:
        addr = ipaddress.IPv4Address(value)
    except ValueError:
        return None
    if addr.is_unspecified or addr.is_multicast or addr.is_loopback or addr.is_link_local:
        return None
    return value


def ip_address(port: AtPort, cid: int) -> str | None:
    """Send AT+CGPADDR=<cid> and return the assigned IPv4 address, if any.

    Returns None for anything that isn't a plausible assigned address (see
    ``valid_assigned_ipv4``) as well as for "no address" -- callers (e.g.
    the reconnect supervisor) already treat None as "no PDP context up".
    """
    return valid_assigned_ipv4(parse_cgpaddr(port.command(f"AT+CGPADDR={cid}")))


def dns(port: AtPort, cid: int) -> list[str]:
    """Send AT+GTDNS=<cid> and return the DNS server addresses, if any."""
    return parse_gtdns(port.command(f"AT+GTDNS={cid}"))


def parse_cgsn(response: str) -> str | None:
    """Parse the IMEI (a bare 14-16 digit string) from an ``AT+CGSN`` response."""
    match = re.search(r"(\d{14,16})", response)
    return match.group(1) if match else None


def imei(port: AtPort) -> str | None:
    """Send AT+CGSN (read-only) and return the modem's IMEI, if parseable.

    Used to pin the modem's identity across a USB re-enumeration (see
    cli.py's ``up --supervise`` restart loop): a device that disappears and
    a *different* device that happens to enumerate at the same VID/PID
    afterwards must never be treated as "the same modem, just back".
    """
    return parse_cgsn(port.command("AT+CGSN"))
