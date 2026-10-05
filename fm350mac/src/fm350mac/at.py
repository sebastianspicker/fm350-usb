"""AT command channel to the FM350-GL's vendor-specific serial interface,
sent over raw USB bulk transfers (macOS has no serial driver for it).

Moved from ``tools/fm350_at.py``; see that script for the original standalone
CLI, now a thin wrapper around ``AtPort``.
"""

from __future__ import annotations

import ipaddress
import logging
import re
import threading
import time

from .redact import redact_pin
from .usb_async import UsbDevice, UsbTimeout, open_device

PIDS = {0x7127: 6, 0x7126: 4}  # USB product id -> AT interface number

# A final result code ends the response; matched as soon as it appears so
# command() doesn't keep draining (with 300 ms read timeouts) after the
# modem is already done -- a plain "AT" measured a 327 ms round trip before
# this, almost all of it spent waiting out one extra drain past "\r\nOK\r\n".
_FINAL_RESULT_RE = re.compile(r"(?m)^(?:OK|ERROR|NO CARRIER|\+CME ERROR:[^\r\n]*|\+CMS ERROR:[^\r\n]*)\r\n")
_DRAIN_TIMEOUT_MS = 50
_STALE_DRAIN_TIMEOUT_MS = 5  # near-non-blocking: just collect what's already queued

_log = logging.getLogger("fm350mac.at")

# Timeouts (seconds). Queries answer instantly; only network operations
# (CGACT) can legitimately take a while.
QUERY_TIMEOUT_S = 10.0
ACTIVATE_TIMEOUT_S = 60.0


class AtTimeoutError(TimeoutError):
    """No final result code arrived before the deadline; ``response`` holds what did.

    A SIM PIN in either (the command, or the modem's echo of it) is always
    masked: this text ends up in error messages and logs.
    """

    def __init__(self, message: str, response: str = "") -> None:
        super().__init__(redact_pin(message))
        self.response = redact_pin(response)


class AtCommandError(RuntimeError):
    """An AT command didn't answer OK; ``response`` holds the response text."""

    def __init__(self, message: str, response: str = "") -> None:
        super().__init__(message)
        self.response = response


class UnknownAtInterface(RuntimeError):
    """The device's USB product id has no known AT interface and none was given."""


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
            raise UnknownAtInterface(
                f"unknown AT interface for USB product id {self.usb_device.pid:#06x}"
            )
        self.iface_num = default_iface if iface_override is None else iface_override
        self.usb_device.claim_interface(self.iface_num)
        self.ep_out, _ = self.usb_device.find_endpoint(self.iface_num, "out")
        self.ep_in, self.ep_in_max_packet = self.usb_device.find_endpoint(self.iface_num, "in", "bulk")
        self._drain(200)  # discard unsolicited output left over from boot

    def _drain(
        self, timeout_ms: int = 200, deadline: float | None = None, stop_event: threading.Event | None = None
    ) -> str:
        """Read until a read times out, ``deadline`` (time.monotonic) passes,
        or ``stop_event`` is set.

        The deadline and stop event are checked between reads, so a stream of
        unsolicited output can't keep this running past them.
        """
        out = b""
        while True:
            if stop_event is not None and stop_event.is_set():
                break
            read_timeout_ms = timeout_ms
            if deadline is not None:
                remaining_ms = int((deadline - time.monotonic()) * 1000)
                if remaining_ms <= 0:
                    break
                read_timeout_ms = min(timeout_ms, remaining_ms)
            try:
                out += self.usb_device.bulk_in(self.ep_in, self.ep_in_max_packet * 8, timeout_ms=read_timeout_ms)
            except UsbTimeout:
                break
        return out.decode(errors="replace")

    def command(self, cmd: str, timeout: float = 240.0, stop_event: threading.Event | None = None) -> str:
        """Send an AT command and return the response text.

        Stale bytes (URCs queued since the last command) are drained first
        so they can't be mistaken for this command's result. Returns as soon
        as the buffer contains a final result code (rather than always
        draining for the full per-read timeout first), using short per-read
        timeouts so the common case is fast. Raises AtTimeoutError if no
        final result code arrives within ``timeout`` seconds.

        The response ends at the final result code: anything read after it
        in the same drain (a URC such as ``+CGEV: ...``) is not part of this
        command's response and is dropped, so ``is_ok()``/``check_ok()``
        still see the result code as the last line.

        ``stop_event`` (optional): checked before sending and between reads,
        so a shutdown never has to sit out a long (e.g. 60 s CGACT) timeout.
        Once it is set the command is abandoned with AtTimeoutError -- the
        modem's state is then as unknown as after a real timeout.
        """
        if stop_event is not None and stop_event.is_set():
            raise AtTimeoutError(f"{cmd!r} not sent: stop requested")
        stale = self._drain(_STALE_DRAIN_TIMEOUT_MS, deadline=time.monotonic() + 0.05)
        if stale.strip():
            _log.debug("discarded stale AT output before %r: %r", cmd, stale)
        self.usb_device.bulk_out(self.ep_out, (cmd + "\r").encode())
        buf = ""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            buf += self._drain(_DRAIN_TIMEOUT_MS, deadline=deadline, stop_event=stop_event)
            final = _FINAL_RESULT_RE.search(buf)
            if final:
                trailing = buf[final.end():]
                if trailing.strip():
                    _log.debug("discarded AT output after %r's final result code: %r", cmd, trailing)
                return buf[: final.end()].strip()
            if stop_event is not None and stop_event.is_set():
                raise AtTimeoutError(f"{cmd!r} abandoned: stop requested", buf.strip())
        raise AtTimeoutError(f"no final result code for {cmd!r} within {timeout:g}s", buf.strip())

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


def is_ok(response: str) -> bool:
    """True if the final non-empty line of ``response`` is ``OK``."""
    lines = [line.strip() for line in response.splitlines() if line.strip()]
    return bool(lines) and lines[-1] == "OK"


def check_ok(response: str, what: str) -> str:
    """Return ``response`` if it ended in OK, else raise AtCommandError naming ``what``."""
    if not is_ok(response):
        raise AtCommandError(f"{what} failed: {response!r}", response)
    return response


_IPV4_TOKEN_RE = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(?![\d.])")


def parse_cgpaddr(response: str) -> str | None:
    """Parse the IPv4 address from ``+CGPADDR: <cid>,"a.b.c.d"[,"<ipv6>"]`` (quotes optional).

    When the context has both an IPv4 and an IPv6 address the IPv4 one is
    returned; an IPv6-only result gives None (the data path is IPv4-only).
    """
    for line in response.splitlines():
        if "+CGPADDR:" not in line:
            continue
        match = _IPV4_TOKEN_RE.search(line.split(":", 1)[1])
        if match:
            return match.group(1)
    return None


# 3GPP TS 27.007's default IPv6 notation (AT+CGPIAF not set): 16 dotted
# decimal octets, e.g. "32.1.13.184.0.0.0.0.0.0.0.0.0.0.0.1".
_DOTTED_IPV6_RE = re.compile(r"(?<![\d.])\d{1,3}(?:\.\d{1,3}){15}(?![\d.])")


def cgpaddr_is_ipv6_only(response: str) -> bool:
    """True if ``+CGPADDR`` reports an IPv6 address (colon or 27.007
    dotted-decimal notation) but no IPv4 one.
    """
    if parse_cgpaddr(response) is not None:
        return False
    for line in response.splitlines():
        if "+CGPADDR:" not in line:
            continue
        rest = line.split(":", 1)[1]
        if ":" in rest or _DOTTED_IPV6_RE.search(rest):
            return True
    return False


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
    return parse_cpin_state(response) == "READY"


def parse_cpin_state(response: str) -> str | None:
    """Return the SIM state of an ``AT+CPIN?`` response.

    ``"READY"``, ``"SIM PIN"``, ``"SIM PUK"`` etc. from ``+CPIN: <state>``;
    ``"NOT INSERTED"`` for ``+CME ERROR: SIM not inserted``/``10``; other
    CME errors are returned as their error text. None if unrecognisable.
    """
    match = re.search(r"\+CPIN:\s*([^\r\n]*)", response)
    if match:
        return match.group(1).strip() or None
    match = re.search(r"\+CME ERROR:\s*([^\r\n]*)", response)
    if match:
        error = match.group(1).strip()
        if error.lower() == "sim not inserted" or error == "10":
            return "NOT INSERTED"
        return error or None
    return None


def parse_cgact(response: str) -> dict[int, bool]:
    """Parse ``+CGACT: <cid>,<state>`` lines into {cid: active}."""
    return {int(cid): state == "1" for cid, state in re.findall(r"\+CGACT:\s*(\d+)\s*,\s*(\d+)", response)}


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


def sim_state(port: AtPort) -> str | None:
    """Send AT+CPIN? and return the SIM state ("READY", "SIM PIN", "NOT INSERTED", ...)."""
    return parse_cpin_state(port.command("AT+CPIN?", timeout=QUERY_TIMEOUT_S))


def sim_ready(port: AtPort) -> bool:
    """Send AT+CPIN? and return whether the SIM reports READY."""
    return sim_state(port) == "READY"


def define_pdp(port: AtPort, cid: int, pdp_type: str, apn: str) -> str:
    """Send AT+CGDCONT to define a PDP context.

    Validates ``pdp_type`` and ``apn`` first (raises ValueError), so
    untrusted input can never be injected into the AT command string.
    """
    validate_pdp_type(pdp_type)
    validate_apn(apn)
    return port.command(f'AT+CGDCONT={cid},"{pdp_type}","{apn}"', timeout=QUERY_TIMEOUT_S)


def activate(port: AtPort, cid: int) -> str:
    """Send AT+CGACT=1,<cid> to activate a PDP context."""
    return port.command(f"AT+CGACT=1,{cid}", timeout=ACTIVATE_TIMEOUT_S)


def deactivate(
    port: AtPort, cid: int, timeout: float = ACTIVATE_TIMEOUT_S, stop_event: threading.Event | None = None
) -> str:
    """Send AT+CGACT=0,<cid> to deactivate a PDP context.

    ``timeout``/``stop_event`` let a shutdown bound the wait (see
    ``AtPort.command``); ``stop_event`` is only passed on when given.
    """
    if stop_event is None:
        return port.command(f"AT+CGACT=0,{cid}", timeout=timeout)
    return port.command(f"AT+CGACT=0,{cid}", timeout=timeout, stop_event=stop_event)


def is_active(port: AtPort, cid: int) -> bool:
    """Send AT+CGACT? and return whether context ``cid`` is active."""
    return parse_cgact(port.command("AT+CGACT?", timeout=QUERY_TIMEOUT_S)).get(cid, False)


def setup_pdp(port: AtPort, cid: int, pdp_type: str, apn: str) -> tuple[str, str]:
    """Define and activate a PDP context, returning the (CGDCONT, CGACT) responses.

    If ``cid`` is already active it is deactivated first -- the modem
    ignores a new APN/PDP type on an active context. Raises ValueError for
    an invalid APN/PDP type and AtCommandError if any step doesn't answer OK.
    """
    validate_pdp_type(pdp_type)
    validate_apn(apn)
    if is_active(port, cid):
        _log.info("PDP context %d is already active; deactivating it so the new APN/PDP type takes effect", cid)
        check_ok(deactivate(port, cid), f"AT+CGACT=0,{cid}")
    defined = check_ok(define_pdp(port, cid, pdp_type, apn), "AT+CGDCONT")
    activated = check_ok(activate(port, cid), f"AT+CGACT=1,{cid}")
    return defined, activated


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
    return valid_assigned_ipv4(parse_cgpaddr(port.command(f"AT+CGPADDR={cid}", timeout=QUERY_TIMEOUT_S)))


def dns(port: AtPort, cid: int) -> list[str]:
    """Send AT+GTDNS=<cid> and return the DNS server addresses, if any."""
    return parse_gtdns(port.command(f"AT+GTDNS={cid}", timeout=QUERY_TIMEOUT_S))


def parse_cgsn(response: str) -> str | None:
    """Parse the IMEI (a bare 14-16 digit string) from an ``AT+CGSN`` response.

    Longer digit runs are not truncated into a match.
    """
    match = re.search(r"(?<!\d)(\d{14,16})(?!\d)", response)
    return match.group(1) if match else None


def imei(port: AtPort) -> str | None:
    """Send AT+CGSN (read-only) and return the modem's IMEI, if parseable.

    Used to pin the modem's identity across a USB re-enumeration (see
    cli.py's ``up --supervise`` restart loop): a device that disappears and
    a *different* device that happens to enumerate at the same VID/PID
    afterwards must never be treated as "the same modem, just back".
    """
    return parse_cgsn(port.command("AT+CGSN", timeout=QUERY_TIMEOUT_S))
