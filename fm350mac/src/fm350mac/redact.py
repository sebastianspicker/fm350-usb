"""Mask identifying data (phone numbers, IMSI/IMEI/ICCID, IP addresses,
TAC/cell ID) in AT response text and log lines, for pasting into bug reports.

Pure string functions, no I/O.
"""

from __future__ import annotations

import ipaddress
import re

REDACTED = "REDACTED"

# +GTCCINFO row fields are always ``<IsServiceCell>,<rat>,<mcc>,<mnc>,<tac>,
# <cellid>,...`` (see cellinfo.py's field tables), so TAC/cell ID are always
# the 5th/6th comma-separated values on any cell row.
_GTCCINFO_ROW_RE = re.compile(r"(?m)^(\d+,\d+,[^,\r\n]*,[^,\r\n]*,)[^,\r\n]*(,)[^,\r\n]*(,)")

_CNUM_RE = re.compile(r'(\+CNUM:\s*"[^"]*"\s*,\s*")[^"]*(")')
_ID_PREFIX_RE = re.compile(r"(\+(?:CIMI|CGSN|GSN|ICCID|CCID):[ \t]*)\S+")
# ICCID: "89" + 17-20 more digits (19-22 total, covering the common 20 and
# 22 digit variants), optional trailing F/hex pad nibble.
_ICCID_RE = re.compile(r"(?<![0-9A-Za-z])89\d{17,20}[0-9A-Fa-f]?(?![0-9A-Za-z])")
# A line consisting of nothing but a 14-16 digit number: IMEI/IMEISV/IMSI as
# printed by AT+CGSN / AT+CIMI without a +PREFIX.
_BARE_ID_LINE_RE = re.compile(r"(?m)^([ \t]*)\d{14,16}([ \t\r]*)$")
# A run of dotted-decimal numbers: a plain IPv4 address (4 parts), or 3GPP
# TS 27.007's dotted forms -- IPv4 address+mask (8 parts, +CGCONTRDP) and
# IPv6 (16 parts; 32 with a mask) when AT+CGPIAF isn't set. Other part counts
# (version strings) are left alone. A sentence-ending "." isn't part of it.
_DOTTED_RE = re.compile(r"(?<![\d.])\d{1,3}(?:\.\d{1,3}){3,31}(?!\.?\d)")
_DOTTED_ADDRESS_PARTS = frozenset({8, 16, 32})
# +CREG/+CGREG/+CEREG/+C5GREG with <n>=2/3 (or as a URC) carry the TAC/LAC
# and cell ID as quoted hex strings after <stat>.
_REG_LOCATION_RE = re.compile(r'(\+C(?:G|E|5G)?REG:[ \t]*\d+(?:[ \t]*,[ \t]*\d+)?[ \t]*,[ \t]*)"[0-9A-Fa-f]*"([ \t]*,[ \t]*)"[0-9A-Fa-f]*"')
_IPV6_RE = re.compile(r"(?<![0-9A-Fa-f:.])(?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f]{0,4}(?![0-9A-Fa-f:.])")


def redact_gtccinfo(text: str) -> str:
    """Mask the TAC and cell ID columns of raw +GTCCINFO rows."""
    return _GTCCINFO_ROW_RE.sub(rf"\1{REDACTED}\2{REDACTED}\3", text)


def _ip_replacement(match: re.Match, parser, private_hint: bool) -> str:
    try:
        addr = parser(match.group(0))
    except ValueError:
        return match.group(0)
    if addr.is_unspecified:
        return match.group(0)
    if private_hint and (addr.is_private or addr.is_link_local):
        return f"{REDACTED}(private)"
    return REDACTED


def _dotted_replacement(match: re.Match, private_hint: bool) -> str:
    parts = match.group(0).split(".")
    if len(parts) == 4:
        return _ip_replacement(match, ipaddress.IPv4Address, private_hint)
    if len(parts) not in _DOTTED_ADDRESS_PARTS or any(int(p) > 255 for p in parts):
        return match.group(0)
    if not any(int(p) for p in parts):
        return match.group(0)  # unspecified, like 0.0.0.0
    return REDACTED


def redact_text(text: str, *, private_hint: bool = False) -> str:
    """Return ``text`` with identifying values masked as ``REDACTED``.

    Masks +CNUM numbers, +CIMI/+CGSN/+GSN/+ICCID/+CCID values, ICCIDs
    anywhere, bare 14-16 digit IMEI/IMSI lines, TAC/cell ID in +GTCCINFO
    rows and +C(E|G|5G)REG location fields, and IPv4/IPv6 addresses
    (including 27.007's dotted IPv6 and address+mask forms; ``0.0.0.0``/``::``
    are kept). With ``private_hint`` an address in a private/link-local
    range is masked as ``REDACTED(private)`` instead.
    """
    text = _CNUM_RE.sub(rf"\1{REDACTED}\2", text)
    text = _ID_PREFIX_RE.sub(rf"\1{REDACTED}", text)
    text = _ICCID_RE.sub(REDACTED, text)
    text = _BARE_ID_LINE_RE.sub(rf"\1{REDACTED}\2", text)
    text = redact_gtccinfo(text)
    text = _REG_LOCATION_RE.sub(rf'\1"{REDACTED}"\2"{REDACTED}"', text)
    text = _DOTTED_RE.sub(lambda m: _dotted_replacement(m, private_hint), text)
    text = _IPV6_RE.sub(lambda m: _ip_replacement(m, ipaddress.IPv6Address, private_hint), text)
    return text
