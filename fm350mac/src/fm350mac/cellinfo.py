"""Pure parsers for cell/signal AT responses (+CESQ, +GTCCINFO, +COPS,
+GTSENRDTEMP) into typed dataclasses. No I/O -- at.py/cli.py send the AT
commands and hand the raw response text to these functions.

Field layouts and scaling come from two sources, cited on each parser:

- The Fibocom FM350 AT Commands User Manual V2.10 (line refs below point
  into the plain-text extraction used while writing this module).
- 3GPP TS 27.007 (the CESQ field definitions the manual itself points at)
  and TS 36.101 / TS 38.101-1 (EARFCN/NR-ARFCN -> band tables, which are
  *not* in the Fibocom manual at all).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_LTE_RAT = 4
_NR_RAT = 9


def _int_or_none(raw: str | None) -> int | None:
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _scale(value: int | None, unknown: int, offset: float, step: float) -> float | None:
    """Map a raw AT enum value to a physical unit, or None if it reports
    "unknown/not detectable" (``unknown``, conventionally 99 or 255).

    The manual (and 3GPP TS 27.007) define each value as a range, e.g. rsrp
    1 = "-140 dBm <= rsrp < -139 dBm". We report the lower bound of the
    range, so ``offset`` is the lower bound of value 0; openwrt/fm350-status.sh
    uses the same convention.
    """
    if value is None or value == unknown:
        return None
    return round(offset + step * value, 2)


def _rssnr_db(value: int | None) -> float | None:
    """RSSNR: -100..100 in 0.5 dB steps, 255 = invalid (manual V2.10 line 4702-4714)."""
    if value is None or value == 255:
        return None
    return round(value * 0.5, 2)


# --- +CESQ: Extended Signal Quality (3GPP TS 27.007 8.69; manual V2.10
# section 11.1.2, lines 3697-3822) ------------------------------------------


@dataclass
class SignalQuality:
    """Decoded AT+CESQ response.

    ``rxlev`` uses the GSM rssi scale (0-63, 99 = unknown) and is not
    converted to dBm here -- only LTE/NR fields are, since that's all this
    modem (LTE/NR only, no 2G/3G) ever reports as non-unknown.
    """

    rxlev: int | None
    rscp_dbm: float | None
    ecno_db: float | None
    rsrq_db: float | None
    rsrp_dbm: float | None
    ss_rsrq_db: float | None
    ss_rsrp_dbm: float | None
    ss_sinr_db: float | None


def parse_cesq(response: str) -> SignalQuality | None:
    """Parse ``+CESQ: <rxlev>,<ber>,<rscp>,<ecno>,<rsrq>,<rsrp>,<ss_rsrq>,<ss_rsrp>,<ss_sinr>``.

    ``<ber>`` is dropped: the manual says it "always returns 99" (line 3700).
    """
    match = re.search(
        r"\+CESQ:\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)",
        response,
    )
    if not match:
        return None
    rxlev, _ber, rscp, ecno, rsrq, rsrp, ss_rsrq, ss_rsrp, ss_sinr = (int(g) for g in match.groups())
    return SignalQuality(
        rxlev=None if rxlev == 99 else rxlev,
        rscp_dbm=_scale(rscp, 255, -121.0, 1.0),
        ecno_db=_scale(ecno, 255, -24.5, 0.5),
        rsrq_db=_scale(rsrq, 255, -20.0, 0.5),
        rsrp_dbm=_scale(rsrp, 255, -141.0, 1.0),
        ss_rsrq_db=_scale(ss_rsrq, 255, -43.5, 0.5),
        ss_rsrp_dbm=_scale(ss_rsrp, 255, -157.0, 1.0),
        ss_sinr_db=_scale(ss_sinr, 255, -23.5, 0.5),
    )


# --- +GTCCINFO: current cell information (manual V2.10 section 11.1.15,
# lines 4569-4764). Only LTE (<rat>=4) and NR (<rat>=9) rows are modelled:
# this modem is LTE/NR only, and those are the two row shapes the task this
# module was written for needs. WCDMA/GSM rows (<rat>=2) have a completely
# different field layout (lac/psc/ecno/rscp/... , manual lines 4580-4588)
# and are skipped rather than guessed at. ------------------------------------

_LTE_SERVING_FIELDS = (
    "is_serving", "rat", "mcc", "mnc", "tac", "cell_id", "earfcn", "pci",
    "band_raw", "bandwidth", "rssnr_raw", "rxlev_raw", "rsrp_raw", "rsrq_raw",
)
_LTE_NEIGHBOUR_FIELDS = (
    "is_serving", "rat", "mcc", "mnc", "tac", "cell_id", "earfcn", "pci",
    "bandwidth", "rxlev_raw", "rsrp_raw", "rsrq_raw",
)
_NR_SERVING_FIELDS = (
    "is_serving", "rat", "mcc", "mnc", "tac", "cell_id", "arfcn", "pci",
    "band_raw", "bandwidth", "ss_sinr_raw", "rxlev_raw", "ss_rsrp_raw", "ss_rsrq_raw",
)
_NR_NEIGHBOUR_FIELDS = (
    "is_serving", "rat", "mcc", "mnc", "tac", "cell_id", "arfcn", "pci",
    "ss_sinr_raw", "rxlev_raw", "ss_rsrp_raw", "ss_rsrq_raw",
)


@dataclass
class LteCell:
    """One LTE row from +GTCCINFO (serving or neighbour; manual lines 4589-4597).

    ``band`` is derived from ``earfcn`` (see ``lte_band_for_earfcn``), not
    read from the modem's own ``<band>`` field: on this modem that field is
    routinely blank even on the serving cell (see the real fixture this
    module's tests are built from), so it can't be relied on.

    ``rxlev_raw`` is kept unconverted on purpose: the manual's <rxlev>
    sub-table for LTE (line 4654-4659) is defined with exactly the same
    breakpoints as <rsrp> a few lines below it (line 4715-4716), which looks
    like a copy-paste of the CESQ <rxlev> section rather than a distinct
    field -- real samples confirm rxlev_raw always equals the rsrp raw value
    here. Rather than assume that holds in general, this module leaves it
    raw and lets callers ignore it.
    """

    is_serving: bool
    mcc: int | None
    mnc: int | None
    tac: str | None
    cell_id: str | None
    earfcn: int | None
    band: int | None
    pci: int | None
    bandwidth: int | None
    rssnr_db: float | None
    rxlev_raw: int | None
    rsrp_dbm: float | None
    rsrq_db: float | None


@dataclass
class NrCell:
    """One NR row from +GTCCINFO (serving or neighbour; manual lines 4599-4611).

    See ``LteCell`` for why ``band`` is derived from ``arfcn`` rather than
    the modem's own field, and why ``rxlev_raw`` is left unconverted.
    """

    is_serving: bool
    mcc: int | None
    mnc: int | None
    tac: str | None
    cell_id: str | None
    arfcn: int | None
    band: str | None
    pci: int | None
    bandwidth: int | None
    ss_sinr_db: float | None
    rxlev_raw: int | None
    ss_rsrp_dbm: float | None
    ss_rsrq_db: float | None


def _parse_lte_row(fields: list[str]) -> LteCell | None:
    if len(fields) == len(_LTE_SERVING_FIELDS):
        row = dict(zip(_LTE_SERVING_FIELDS, fields))
    elif len(fields) == len(_LTE_NEIGHBOUR_FIELDS):
        row = dict(zip(_LTE_NEIGHBOUR_FIELDS, fields))
    else:
        return None
    earfcn = _int_or_none(row.get("earfcn"))
    return LteCell(
        is_serving=row["is_serving"] == "1",
        mcc=_int_or_none(row.get("mcc")),
        mnc=_int_or_none(row.get("mnc")),
        tac=row.get("tac") or None,
        cell_id=row.get("cell_id") or None,
        earfcn=earfcn,
        band=lte_band_for_earfcn(earfcn),
        pci=_int_or_none(row.get("pci")),
        bandwidth=_int_or_none(row.get("bandwidth")),
        rssnr_db=_rssnr_db(_int_or_none(row.get("rssnr_raw"))),
        rxlev_raw=_int_or_none(row.get("rxlev_raw")),
        rsrp_dbm=_scale(_int_or_none(row.get("rsrp_raw")), 255, -141.0, 1.0),
        rsrq_db=_scale(_int_or_none(row.get("rsrq_raw")), 255, -20.0, 0.5),
    )


def _parse_nr_row(fields: list[str]) -> NrCell | None:
    if len(fields) == len(_NR_SERVING_FIELDS):
        row = dict(zip(_NR_SERVING_FIELDS, fields))
    elif len(fields) == len(_NR_NEIGHBOUR_FIELDS):
        row = dict(zip(_NR_NEIGHBOUR_FIELDS, fields))
    else:
        return None
    arfcn = _int_or_none(row.get("arfcn"))
    return NrCell(
        is_serving=row["is_serving"] == "1",
        mcc=_int_or_none(row.get("mcc")),
        mnc=_int_or_none(row.get("mnc")),
        tac=row.get("tac") or None,
        cell_id=row.get("cell_id") or None,
        arfcn=arfcn,
        band=nr_band_for_arfcn(arfcn),
        pci=_int_or_none(row.get("pci")),
        bandwidth=_int_or_none(row.get("bandwidth")),
        ss_sinr_db=_scale(_int_or_none(row.get("ss_sinr_raw")), 255, -23.5, 0.5),
        rxlev_raw=_int_or_none(row.get("rxlev_raw")),
        ss_rsrp_dbm=_scale(_int_or_none(row.get("ss_rsrp_raw")), 255, -157.0, 1.0),
        ss_rsrq_db=_scale(_int_or_none(row.get("ss_rsrq_raw")), 255, -43.5, 0.5),
    )


def parse_gtccinfo(response: str) -> list[LteCell | NrCell]:
    """Parse an AT+GTCCINFO? response into serving + neighbour cell rows.

    Each non-empty, non-echo, non-result-code line is one cell row; the
    real modem separates the serving-cell line and the block of neighbour
    lines with a blank line, which this just skips over. Rows for RATs
    other than LTE/NR, or with a field count that doesn't match either the
    serving or neighbour shape, are skipped rather than guessed at.
    """
    cells: list[LteCell | NrCell] = []
    for raw_line in response.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("+GTCCINFO", "AT+GTCCINFO")) or line in ("OK", "ERROR"):
            continue
        fields = [f.strip() for f in line.split(",")]
        if len(fields) < 2:
            continue
        rat = _int_or_none(fields[1])
        cell: LteCell | NrCell | None
        if rat == _LTE_RAT:
            cell = _parse_lte_row(fields)
        elif rat == _NR_RAT:
            cell = _parse_nr_row(fields)
        else:
            cell = None
        if cell is not None:
            cells.append(cell)
    return cells


def serving_cell(cells: list[LteCell | NrCell]) -> LteCell | NrCell | None:
    """The serving cell among ``parse_gtccinfo()``'s result, if any."""
    for cell in cells:
        if cell.is_serving:
            return cell
    return None


def neighbour_count_by_band(cells: list[LteCell | NrCell]) -> dict[str, int]:
    """Count neighbour (non-serving) cells per band label (e.g. "LTE B20", "NR n78")."""
    counts: dict[str, int] = {}
    for cell in cells:
        if cell.is_serving:
            continue
        if isinstance(cell, LteCell):
            label = f"LTE B{cell.band}" if cell.band is not None else "LTE (unknown band)"
        else:
            label = f"NR {cell.band}" if cell.band is not None else "NR (unknown band)"
        counts[label] = counts.get(label, 0) + 1
    return counts


def no_cells_measured(cesq: SignalQuality | None, cells: list[LteCell | NrCell]) -> bool:
    """True if nothing at all is being received: no serving/neighbour cell in
    GTCCINFO, and CESQ's LTE/NR fields are all "unknown" too.
    """
    if cells:
        return False
    if cesq is None:
        return True
    return all(
        v is None
        for v in (cesq.rsrp_dbm, cesq.rsrq_db, cesq.ss_rsrp_dbm, cesq.ss_rsrq_db, cesq.ss_sinr_db)
    )


# --- EARFCN -> LTE band / NR-ARFCN -> NR band -------------------------------
#
# Not in the Fibocom manual at all: these are the downlink channel-number
# ranges from 3GPP TS 36.101 Table 5.7.3-1 (LTE) and TS 38.101-1 Table
# 5.4.2.1-1 (NR global frequency raster), common bands only.

_LTE_BAND_RANGES: tuple[tuple[int, int, int], ...] = (
    (1, 0, 599),
    (2, 600, 1199),
    (3, 1200, 1949),
    (4, 1950, 2399),
    (5, 2400, 2649),
    (7, 2750, 3449),
    (8, 3450, 3799),
    (12, 5010, 5179),
    (13, 5180, 5279),
    (17, 5730, 5849),
    (20, 6150, 6449),
    (25, 8040, 8689),
    (26, 8690, 9039),
    (28, 9210, 9659),
    (30, 9770, 9869),
    (32, 9920, 10359),
    (38, 37750, 38249),
    (40, 38650, 39649),
    (41, 39650, 41589),
    (42, 41590, 43589),
    (43, 43590, 45589),
    (66, 66436, 67335),
)

# n77 (620000-680000) and n78 (620000-653333) overlap -- n78 is a subset of
# n77 -- and there's no way to tell them apart from the ARFCN alone. n78 is
# listed first so a channel in the shared range is reported as n78 (the far
# more common deployment); this is a heuristic, not a real disambiguation.
# n41 (499200-537999) overlaps n38 (514000-524000) and n7 (524000-538000):
# n41 comes after both, so a channel in those ranges is reported as n38/n7
# and only 499200-513999 as n41; same heuristic caveat applies.
_NR_BAND_RANGES: tuple[tuple[str, int, int], ...] = (
    ("n1", 422000, 434000),
    ("n3", 361000, 376000),
    ("n7", 524000, 538000),
    ("n8", 185000, 192000),
    ("n20", 158200, 164200),
    ("n28", 151600, 160600),
    ("n38", 514000, 524000),
    ("n40", 460000, 480000),
    ("n41", 499200, 537999),
    ("n78", 620000, 653333),
    ("n77", 620000, 680000),
    ("n79", 693334, 733333),
)


def lte_band_for_earfcn(earfcn: int | None) -> int | None:
    """Map a downlink EARFCN to an LTE band number, or None if unknown/unlisted."""
    if earfcn is None:
        return None
    for band, lo, hi in _LTE_BAND_RANGES:
        if lo <= earfcn <= hi:
            return band
    return None


def nr_band_for_arfcn(arfcn: int | None) -> str | None:
    """Map a downlink NR-ARFCN to an NR band label (e.g. "n78"), or None if unknown/unlisted."""
    if arfcn is None:
        return None
    for band, lo, hi in _NR_BAND_RANGES:
        if lo <= arfcn <= hi:
            return band
    return None


# --- +COPS: operator selection (manual V2.10 section 11.1.6, lines 4082-4162) ---

_COPS_ACT_NAMES: dict[int, str] = {
    # 3GPP TS 27.007 <AcT>. The Fibocom manual's table (lines 4152-4162)
    # lists 8-12 as CDMA/EVDO/eMTC/NB-IoT and stops at 12, but the MediaTek
    # firmware follows 27.007: it reports 13 while camped on LTE with NR
    # measurements in +CESQ, i.e. EN-DC (5G non-standalone).
    0: "GSM",
    1: "GSM Compact",
    2: "UTRAN",
    3: "GSM w/EGPRS",
    4: "UTRAN w/HSDPA",
    5: "UTRAN w/HSUPA",
    6: "UTRAN w/HSDPA+HSUPA",
    7: "E-UTRAN (LTE)",
    8: "EC-GSM-IoT",
    9: "E-UTRAN (NB-S1 mode)",
    10: "E-UTRA connected to 5GCN",
    11: "NR connected to 5GCN (5G SA)",
    12: "NG-RAN",
    13: "LTE + NR dual connectivity (EN-DC, 5G NSA)",
}


@dataclass
class Operator:
    """Decoded AT+COPS? response."""

    mode: int
    name: str | None
    act: int | None
    act_name: str | None


def parse_cops(response: str) -> Operator | None:
    """Parse ``+COPS: <mode>[,<format>,"<oper>"[,<AcT>]]``."""
    match = re.search(r'\+COPS:\s*(\d+)(?:\s*,\s*\d+\s*,\s*"([^"]*)"(?:\s*,\s*(\d+))?)?', response)
    if not match:
        return None
    mode = int(match.group(1))
    name = match.group(2) or None
    act = int(match.group(3)) if match.group(3) is not None else None
    return Operator(mode=mode, name=name, act=act, act_name=_COPS_ACT_NAMES.get(act) if act is not None else None)


# --- +GTSENRDTEMP: thermal sensor (manual V2.10 section 18.3, lines 7889-7953) ---


def parse_gtsenrdtemp(response: str) -> int | None:
    """Parse the first ``+GTSENRDTEMP: <sensor_id>,<temperature>`` reading.

    ``<temperature>`` is in the modem's own units, thousandths of a degree
    Celsius (docs/bench-log.md's idle reading of 25-27.6 C matches raw
    values in the 25000-27600 range) -- use ``millidegrees_to_celsius()``.
    """
    match = re.search(r"\+GTSENRDTEMP:\s*\d+\s*,\s*(-?\d+)", response)
    return int(match.group(1)) if match else None


def millidegrees_to_celsius(value: int | None) -> float | None:
    """Convert a raw +GTSENRDTEMP reading (m°C) to degrees Celsius."""
    return None if value is None else round(value / 1000.0, 1)


# --- +ERAT: RAT mode and GPRS/EDGE status (manual V2.10 section 11.1.11,
# lines 4318-4368) -- used by `doctor`, not `status`. ------------------------

_ERAT_ACT_NAMES: dict[int, str] = {
    2: "UTRAN",
    4: "UTRAN w/HSDPA",
    5: "UTRAN w/HSUPA",
    6: "UTRAN w/HSDPA+HSUPA",
    7: "E-UTRAN",
    9: "E-UTRAN (NB-S1 mode)",
    10: "E-UTRA connected to a 5GCN",
    11: "NR connected to a 5GCN",
    12: "NR connected to an EPS core",
    13: "NG-RAN",
    14: "E-UTRA-NR dual connectivity",
    255: "unknown",
}
_ERAT_MODE_NAMES: dict[int, str] = {
    1: "UMTS only",
    3: "LTE only",
    5: "UMTS+LTE",
    15: "NR only",
    17: "UMTS+NR",
    19: "LTE+NR",
    21: "UMTS+LTE+NR",
}


@dataclass
class RatStatus:
    """Decoded AT+ERAT? response."""

    act: int | None
    act_name: str | None
    rat_mode: int | None
    rat_mode_name: str | None


def parse_erat(response: str) -> RatStatus | None:
    """Parse ``+ERAT: <Act>,<GPRSstatus>,<RATmode>,<prefer_rat>``.

    The manual documents exactly 4 fields (lines 4322-4329), but this
    modem's live response carries a trailing 5th value (``+ERAT:
    13,0,21,0,0``) that we couldn't find documented anywhere -- it's
    ignored here rather than guessed at.
    """
    match = re.search(r"\+ERAT:\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)", response)
    if not match:
        return None
    act = int(match.group(1))
    rat_mode = int(match.group(3))
    return RatStatus(
        act=act,
        act_name=_ERAT_ACT_NAMES.get(act, f"unknown ({act})"),
        rat_mode=rat_mode,
        rat_mode_name=_ERAT_MODE_NAMES.get(rat_mode, f"unknown ({rat_mode})"),
    )
