"""fm350mac command-line interface.

Subcommands: probe | at | status | connect | disconnect | up.
See docs/macos-driver.md in the repo root for the architecture and session flow.

Most command functions take keyword-only factories for their external
dependencies (AtPort, device-finding, RndisUsb, Utun, NetConfig, ...),
defaulting to the real implementations. This is dependency injection for
tests only: the CLI surface (argv -> argparse -> args.func(args)) is
unchanged, since real usage never passes the extra keywords.
"""

from __future__ import annotations

import argparse
import dataclasses
import ipaddress
import json
import logging
import os
import re
import signal
import socket
import struct
import sys
import threading
import time

from . import at as at_mod
from . import cellinfo, ethernet, helper_admin, loopback, rndis
from .async_bridge import DEFAULT_RX_URBS, DEFAULT_TX_URBS, AsyncBridge
from .bridge import Bridge
from .helper_client import HelperNetConfig
from .helper_client import probe as probe_helper
from .netconfig import MAX_HOST_ROUTES, SCUTIL, NetConfig, valid_unicast_ipv4, validate_route_host
from .redact import redact_text
from .rndis_device import RndisDevice
from .supervisor import Supervisor
from .usb_async import AsyncEndpoint, EventLoop, close_cached
from .usb_transport import RndisUsb, find_device
from .utun import Utun

_log = logging.getLogger("fm350mac")

DEFAULT_CID = 1
DEFAULT_PDP_TYPE = "IP"
DEFAULT_REENUM_TIMEOUT_S = 180.0
_REENUM_POLL_INTERVAL_S = 2.0
_REENUM_SETTLE_S = 15.0

_PROBE_OIDS = [
    ("GEN_MAXIMUM_FRAME_SIZE", rndis.OID_GEN_MAXIMUM_FRAME_SIZE),
    ("GEN_LINK_SPEED", rndis.OID_GEN_LINK_SPEED),
    ("GEN_CURRENT_PACKET_FILTER", rndis.OID_GEN_CURRENT_PACKET_FILTER),
    ("GEN_MAXIMUM_TOTAL_SIZE", rndis.OID_GEN_MAXIMUM_TOTAL_SIZE),
    ("GEN_MEDIA_CONNECT_STATUS", rndis.OID_GEN_MEDIA_CONNECT_STATUS),
    ("802_3_PERMANENT_ADDRESS", rndis.OID_802_3_PERMANENT_ADDRESS),
    ("802_3_CURRENT_ADDRESS", rndis.OID_802_3_CURRENT_ADDRESS),
]


class _RedactingFormatter(logging.Formatter):
    """``--redact``: mask identifiers (see redact.py) in every log line,
    tracebacks included -- not just the lines cli.py itself builds (e.g. the
    supervisor's IP-change warning, netconfig's dry-run command log).
    """

    def format(self, record: logging.LogRecord) -> str:
        return redact_text(super().format(record))


def _setup_logging(verbose: bool, redact: bool = False) -> None:
    handler = logging.StreamHandler()
    formatter_cls = _RedactingFormatter if redact else logging.Formatter
    handler.setFormatter(formatter_cls("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, handlers=[handler])


def _apn_arg(value: str) -> str:
    """argparse ``type=`` for ``--apn``: reject anything outside the allow-list early."""
    try:
        return at_mod.validate_apn(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _pdp_type_arg(value: str) -> str:
    """argparse ``type=`` for ``--pdp``: reject anything but IP/IPV6/IPV4V6 early."""
    try:
        return at_mod.validate_pdp_type(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _int_range_arg(lo: int, hi: int):
    """argparse ``type=`` factory: an int in the inclusive range lo..hi."""

    def parse(value: str) -> int:
        try:
            number = int(value)
        except ValueError:
            raise argparse.ArgumentTypeError(f"invalid int value: {value!r}") from None
        if not lo <= number <= hi:
            raise argparse.ArgumentTypeError(f"{number} is out of range ({lo}..{hi})")
        return number

    return parse


def _positive_float_arg(minimum: float = 0.0, inclusive: bool = False):
    """argparse ``type=`` factory: a finite float > minimum (>= if inclusive)."""

    def parse(value: str) -> float:
        try:
            number = float(value)
        except ValueError:
            raise argparse.ArgumentTypeError(f"invalid float value: {value!r}") from None
        if not (number >= minimum if inclusive else number > minimum) or number == float("inf"):
            raise argparse.ArgumentTypeError(f"{value} must be {'>=' if inclusive else '>'} {minimum:g}")
        return number

    return parse


def _maybe_redact(args: argparse.Namespace, text: str) -> str:
    return redact_text(text) if getattr(args, "redact", False) else text


def _validate_ipv4(value: str, context: str) -> str:
    """Validate ``value`` as an IPv4 address before it reaches netconfig. Raises ValueError."""
    try:
        ipaddress.IPv4Address(value)
    except ValueError as exc:
        raise ValueError(f"invalid {context}: {value!r}") from exc
    return value


def _route_host_arg(value: str) -> str:
    """argparse ``type=`` for ``--route-host``: a unicast IPv4 address."""
    try:
        return validate_route_host(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _filter_valid_ips(servers: list[str]) -> list[str]:
    """Drop anything that isn't a usable unicast IPv4 address (the only kind
    netconfig/the helper accept) before it reaches netconfig.
    """
    valid = []
    for server in servers:
        normalized = valid_unicast_ipv4(server)
        if normalized is None:
            _log.warning("ignoring invalid DNS server address from modem: %r", server)
            continue
        valid.append(normalized)
    return valid


def _format_oid_value(name: str, value: bytes) -> str:
    if "ADDRESS" in name:
        return value.hex(":")
    if len(value) >= 4:
        return str(struct.unpack_from("<I", value)[0])
    return value.hex()


def cmd_probe(
    _args: argparse.Namespace,
    *,
    find_device_factory=find_device,
    rndis_usb_factory=RndisUsb,
) -> int:
    """RNDIS initialize + query the OIDs of interest, then halt. No root needed."""
    dev = find_device_factory()
    try:
        with rndis_usb_factory(dev) as usb_dev:
            device = RndisDevice(usb_dev)
            init = device.initialize()
            print(
                f"INIT_CMPLT: v{init.major}.{init.minor} device_flags={init.device_flags:#x} "
                f"medium={init.medium} max_packets_per_transfer={init.max_packets_per_transfer} "
                f"max_transfer_size={init.max_transfer_size} "
                f"packet_alignment_factor={init.packet_alignment_factor}"
            )
            for name, oid in _PROBE_OIDS:
                value = device.query(oid)
                print(f"{name:26} {_format_oid_value(name, value)}")
            device.halt()
    finally:
        # RndisUsb only releases its own interfaces (0/1): it shares the
        # underlying UsbDevice handle with an AT port elsewhere in this
        # process (see usb_async.open_device()), so nothing else closes it.
        dev.close()
    return 0


def cmd_at(args: argparse.Namespace, *, at_port_factory=None) -> int:
    """Send raw AT commands to the vendor serial interface. No root needed."""
    port = at_port_factory() if at_port_factory else at_mod.AtPort(iface_override=args.iface)
    try:
        for cmd in args.commands:
            print(f">>> {cmd}")
            try:
                print(_maybe_redact(args, port.command(cmd)))
            except at_mod.AtTimeoutError as exc:
                if exc.response:
                    print(_maybe_redact(args, exc.response))
                print(f"{cmd}: timed out waiting for a final result code", file=sys.stderr)
                return 1
    finally:
        port.close()
    return 0


DEFAULT_WATCH_INTERVAL_S = 2.0

# All status/doctor queries are short, read-only AT commands that answer in
# well under a second on real hardware; capping the per-command timeout well
# below AtPort.command()'s 240s default bounds how long one stuck query can
# block --watch's refresh loop (which, unlike `up`, has no cooperative stop
# flag -- see supervisor.py's _run_signal_guarded -- so a stuck command is
# only recoverable by Ctrl-C actually landing between commands).
_QUERY_TIMEOUT_S = 10.0


def _query_all(port, commands: tuple[tuple[str, str], ...]) -> dict[str, str]:
    return {label: port.command(cmd, timeout=_QUERY_TIMEOUT_S) for label, cmd in commands}


_NO_CELLS_HINT = "no cells measured: check the antenna pigtails and connectors first (see docs/dell-dw5931e-usb.md)"

_REG_STAT_NAMES = {
    0: "not registered",
    1: "home",
    2: "searching",
    3: "registration denied",
    4: "unknown",
    5: "roaming",
}

# The AT queries status/doctor share, sent once per snapshot -- label ->
# command, in the order they're sent and (for --raw) printed.
_STATUS_COMMANDS = (
    ("cpin", "AT+CPIN?"),
    ("cops", "AT+COPS?"),
    ("cereg", "AT+CEREG?"),
    ("c5greg", "AT+C5GREG?"),
    ("cesq", "AT+CESQ"),
    ("gtccinfo", "AT+GTCCINFO?"),
    ("gtsenrdtemp", "AT+GTSENRDTEMP=0"),
)

def _collect_status(port) -> dict[str, str]:
    """Send the read-only AT queries status/doctor share, and return the raw responses."""
    return _query_all(port, _STATUS_COMMANDS)


def _build_status_report(responses: dict[str, str]) -> dict:
    cereg_stat = at_mod.parse_registration(responses["cereg"])
    c5greg_stat = at_mod.parse_registration(responses["c5greg"])
    cesq = cellinfo.parse_cesq(responses["cesq"])
    cells = cellinfo.parse_gtccinfo(responses["gtccinfo"])
    return {
        "sim_ready": at_mod.parse_cpin(responses["cpin"]),
        "sim_state": at_mod.parse_cpin_state(responses["cpin"]),
        "cereg_stat": cereg_stat,
        "c5greg_stat": c5greg_stat,
        "lte_registered": at_mod.is_registered(cereg_stat),
        "nr_registered": at_mod.is_registered(c5greg_stat),
        "operator": cellinfo.parse_cops(responses["cops"]),
        "cesq": cesq,
        "cells": cells,
        "serving_cell": cellinfo.serving_cell(cells),
        "neighbour_counts": cellinfo.neighbour_count_by_band(cells),
        "temperature_c": cellinfo.millidegrees_to_celsius(cellinfo.parse_gtsenrdtemp(responses["gtsenrdtemp"])),
        "no_cells": cellinfo.no_cells_measured(cesq, cells),
    }


def _serving_signal(cell: cellinfo.LteCell | cellinfo.NrCell | None) -> tuple[float | None, float | None, str]:
    """(power_dbm, quality_db, quality_label) for the serving cell, if any."""
    if cell is None:
        return None, None, "SINR"
    if isinstance(cell, cellinfo.LteCell):
        return cell.rsrp_dbm, cell.rssnr_db, "RSSNR"
    return cell.ss_rsrp_dbm, cell.ss_sinr_db, "SINR"


def _format_serving_cell(cell: cellinfo.LteCell | cellinfo.NrCell, redact: bool) -> str:
    cell_id = "REDACTED" if redact else cell.cell_id
    tac = "REDACTED" if redact else cell.tac
    if isinstance(cell, cellinfo.LteCell):
        band = f"B{cell.band}" if cell.band is not None else "unknown"
        return (
            f"Serving cell: LTE {band} EARFCN={cell.earfcn} PCI={cell.pci} TAC={tac} cell_id={cell_id} "
            f"RSRP={cell.rsrp_dbm} dBm RSRQ={cell.rsrq_db} dB"
        )
    band = cell.band or "unknown"
    return (
        f"Serving cell: NR {band} ARFCN={cell.arfcn} PCI={cell.pci} TAC={tac} cell_id={cell_id} "
        f"SS-RSRP={cell.ss_rsrp_dbm} dBm SS-SINR={cell.ss_sinr_db} dB"
    )


def _sim_text(ready: bool, state: str | None) -> str:
    if ready:
        return "ready"
    return f"not ready ({state})" if state else "not ready"


def _format_status(report: dict, redact: bool) -> str:
    lines = [
        f"SIM: {_sim_text(report['sim_ready'], report['sim_state'])}",
        "Registration: LTE {} ({}), NR {} ({})".format(
            "yes" if report["lte_registered"] else "no",
            _REG_STAT_NAMES.get(report["cereg_stat"], f"stat={report['cereg_stat']}"),
            "yes" if report["nr_registered"] else "no",
            "not reported" if report["c5greg_stat"] is None else _REG_STAT_NAMES.get(
                report["c5greg_stat"], f"stat={report['c5greg_stat']}"
            ),
        ),
    ]
    operator = report["operator"]
    if operator is not None and operator.name:
        lines.append(f"Operator: {operator.name} ({operator.act_name or operator.act})")
    else:
        lines.append("Operator: none")
    serving = report["serving_cell"]
    lines.append(_format_serving_cell(serving, redact) if serving is not None else "Serving cell: none")
    cesq = report["cesq"]
    if cesq is not None and cesq.ss_rsrp_dbm is not None:
        # +CESQ carries NR measurements while +GTCCINFO lists only the LTE
        # anchor, e.g. on an EN-DC (5G NSA) cell.
        lines.append(f"NR signal: SS-RSRP={cesq.ss_rsrp_dbm} dBm SS-RSRQ={cesq.ss_rsrq_db} dB SS-SINR={cesq.ss_sinr_db} dB")
    if report["neighbour_counts"]:
        counts = ", ".join(f"{n} {band}" for band, n in sorted(report["neighbour_counts"].items()))
        lines.append(f"Neighbour cells: {counts}")
    else:
        lines.append("Neighbour cells: none")
    if report["temperature_c"] is not None:
        lines.append(f"Temperature: {report['temperature_c']:.1f} C")
    if report["no_cells"]:
        lines.append(_NO_CELLS_HINT)
    return "\n".join(lines)


def _serialize_cell(cell: cellinfo.LteCell | cellinfo.NrCell, redact: bool) -> dict:
    d = dataclasses.asdict(cell)
    if redact:
        if d.get("tac") is not None:
            d["tac"] = "REDACTED"
        if d.get("cell_id") is not None:
            d["cell_id"] = "REDACTED"
    return d


def _serialize_report(report: dict, redact: bool) -> dict:
    operator = report["operator"]
    serving = report["serving_cell"]
    return {
        "sim_ready": report["sim_ready"],
        "sim_state": report["sim_state"],
        "cereg_stat": report["cereg_stat"],
        "c5greg_stat": report["c5greg_stat"],
        "lte_registered": report["lte_registered"],
        "nr_registered": report["nr_registered"],
        "operator": dataclasses.asdict(operator) if operator is not None else None,
        "cesq": dataclasses.asdict(report["cesq"]) if report["cesq"] is not None else None,
        "serving_cell": _serialize_cell(serving, redact) if serving is not None else None,
        "neighbour_counts": report["neighbour_counts"],
        "cell_count": len(report["cells"]),
        "temperature_c": report["temperature_c"],
        "no_cells_measured": report["no_cells"],
    }


def _signal_bar(dbm: float | None, lo: float = -120.0, hi: float = -60.0, width: int = 20) -> str:
    """A crude text bar for antenna aiming: more '#' is a stronger signal."""
    if dbm is None:
        return "[" + "-" * width + "] no signal"
    frac = max(0.0, min(1.0, (dbm - lo) / (hi - lo)))
    filled = round(frac * width)
    return "[" + "#" * filled + "-" * (width - filled) + f"] {dbm:.0f} dBm"


def _print_status_once(port, args: argparse.Namespace) -> dict:
    responses = _collect_status(port)
    report = _build_status_report(responses)
    if args.json:
        print(json.dumps(_serialize_report(report, args.redact), indent=2))
        return report
    if args.raw:
        for label, cmd in _STATUS_COMMANDS:
            print(f">>> {cmd}")
            text = responses[label]
            if args.redact:
                text = redact_text(text)
            print(text)
        print()
    print(_format_status(report, args.redact))
    return report


def _watch_status(port, args: argparse.Namespace, *, sleep=time.sleep) -> int:
    try:
        while True:
            sys.stdout.write("\x1b[2J\x1b[H")  # clear screen, cursor home
            try:
                report = _print_status_once(port, args)
            except at_mod.AtTimeoutError as exc:
                print(f"status: {exc}; retrying", file=sys.stderr)
                sys.stdout.flush()
                sleep(args.watch)
                continue
            power_dbm, quality_db, quality_label = _serving_signal(report["serving_cell"])
            print(f"RSRP {_signal_bar(power_dbm)}")
            if quality_db is not None:
                print(f"{quality_label}: {quality_db} dB")
            sys.stdout.flush()
            sleep(args.watch)
    except KeyboardInterrupt:
        print()
    return 0


def cmd_status(args: argparse.Namespace, *, at_port_factory=at_mod.AtPort, sleep=time.sleep) -> int:
    """Print SIM, registration, cell and thermal status. No root needed.

    Human-readable by default; ``--json`` for machine-readable output,
    ``--raw`` to also print the underlying AT responses, ``--redact`` to
    mask cell ID/TAC (and, in ``--raw`` output, every identifier), and ``--watch [SECONDS]`` to keep refreshing (for
    antenna aiming) instead of printing once.
    """
    if args.json and args.watch is not None:
        # --watch redraws the screen and appends a signal bar every round,
        # which would corrupt the machine-readable output.
        print("status: --json can't be combined with --watch", file=sys.stderr)
        return 2
    port = at_port_factory()
    try:
        if args.watch is not None:
            return _watch_status(port, args, sleep=sleep)
        try:
            _print_status_once(port, args)
        except at_mod.AtTimeoutError as exc:
            print(f"status: {exc}", file=sys.stderr)
            return 1
        return 0
    finally:
        port.close()


# --- doctor: read-only diagnostic checks ------------------------------------

# Commands doctor sends. All are queries (``?``) except the ones listed in
# _DOCTOR_ALLOWED_EQUALS_COMMANDS below -- see test_cli_e2e.py's assertion
# that doctor never sends a write/set command.
_DOCTOR_COMMANDS = (
    ("pkgver", "AT+GTPKGVER?"),
    ("dipcmode", "AT+GTDIPCMODE?"),
    ("fcceffstatus", "AT+GTFCCEFFSTATUS?"),
    ("fmode", "AT+GTFMODE?"),
    ("usbmode", "AT+GTUSBMODE?"),
    ("erat", "AT+ERAT?"),
    ("anttuningen", "AT+GTANTTUNINGEN?"),
    ("cfun", "AT+CFUN?"),
    ("cpin", "AT+CPIN?"),
    ("cesq", "AT+CESQ"),
    ("gtccinfo", "AT+GTCCINFO?"),
)

# Read-type commands that happen to use "=" (e.g. AT+GTSENRDTEMP=<sensor_id>
# selects which sensor to read, it doesn't change any setting) are the only
# ones allowed to contain "="; everything else must be a plain query.
_DOCTOR_ALLOWED_EQUALS_COMMANDS = frozenset({"AT+GTSENRDTEMP=0"})


@dataclasses.dataclass
class DoctorCheck:
    """One doctor result line."""

    level: str  # "OK" | "WARN" | "INFO"
    message: str


def _parse_int_tuple(response: str) -> tuple[int, ...] | None:
    """Pull the comma-separated integers out of a ``+FOO: 1,2,3`` response.

    Error results (``+CME ERROR: 3``, ``+CMS ERROR: ...``) return None rather
    than being read as data.
    """
    match = re.search(r"^\+(?!CME ERROR|CMS ERROR)[A-Z0-9]+:\s*([\d,\-]+)", response, re.MULTILINE)
    if not match:
        return None
    try:
        return tuple(int(v) for v in match.group(1).split(","))
    except ValueError:
        return None


_OEM_IMAGE_NAMES = {"5025": "Dell DW5931e"}


def _doctor_checks(responses: dict[str, str]) -> list[DoctorCheck]:
    checks: list[DoctorCheck] = []

    pkgver_match = re.search(r'"([^"]*)"', responses["pkgver"])
    pkgver = pkgver_match.group(1) if pkgver_match else responses["pkgver"].strip()
    oem_match = re.search(r"_(\d{4})\.", pkgver)
    if oem_match:
        oem_name = _OEM_IMAGE_NAMES.get(oem_match.group(1), f"OEM image {oem_match.group(1)}")
        checks.append(DoctorCheck("INFO", f"firmware package: {pkgver} ({oem_name})"))
    else:
        checks.append(DoctorCheck("INFO", f"firmware package: {pkgver}"))

    dipc = _parse_int_tuple(responses["dipcmode"])
    if dipc is None:
        checks.append(DoctorCheck("WARN", f"GTDIPCMODE?: unparseable response {redact_text(responses['dipcmode'])!r}"))
    elif dipc[0] == 1:
        checks.append(DoctorCheck("INFO", f"DIPC mode {dipc[0]} (PCIe Advance: USB only works without a PCIe link)"))
    elif dipc[0] == 3:
        checks.append(DoctorCheck("OK", f"DIPC mode {dipc[0]} (dual: USB always on)"))
    else:
        checks.append(DoctorCheck("WARN", f"DIPC mode {dipc[0]}: USB is disabled in this mode"))

    fcc = _parse_int_tuple(responses["fcceffstatus"])
    if fcc is None or len(fcc) < 2:
        checks.append(DoctorCheck("WARN", f"GTFCCEFFSTATUS?: unparseable response {redact_text(responses['fcceffstatus'])!r}"))
    elif fcc[1] == 1:
        checks.append(DoctorCheck("OK", f"FCC lock: unlocked (mode={fcc[0]}, status={fcc[1]})"))
    else:
        checks.append(DoctorCheck("WARN", f"FCC lock: locked (mode={fcc[0]}, status={fcc[1]})"))

    fmode = _parse_int_tuple(responses["fmode"])
    if fmode is None or len(fmode) < 2:
        checks.append(DoctorCheck("WARN", f"GTFMODE?: unparseable response {redact_text(responses['fmode'])!r}"))
    else:
        n, m = fmode[0], fmode[1]
        checks.append(DoctorCheck(
            "INFO",
            f"GTFMODE {n},{m} (radio hw-pin control {'enabled' if n else 'disabled'}, "
            f"GNSS hw-pin control {'enabled' if m else 'disabled'})",
        ))

    usbmode = _parse_int_tuple(responses["usbmode"])
    if usbmode is None:
        checks.append(DoctorCheck("WARN", f"GTUSBMODE?: unparseable response {redact_text(responses['usbmode'])!r}"))
    else:
        note = " (RNDIS + serial + ADB)" if usbmode[0] == 41 else ""
        checks.append(DoctorCheck("INFO", f"USB mode {usbmode[0]}{note}"))

    erat = cellinfo.parse_erat(responses["erat"])
    if erat is None:
        checks.append(DoctorCheck("WARN", f"ERAT?: unparseable response {redact_text(responses['erat'])!r}"))
    else:
        # ERAT's <Act> uses MediaTek numbering that doesn't match what +COPS
        # reports on this firmware, so only the configured mode is shown here;
        # `status` shows the current access technology from +COPS.
        checks.append(DoctorCheck("INFO", f"RAT mode: {erat.rat_mode_name} (ERAT mode {erat.rat_mode})"))

    anttuningen = _parse_int_tuple(responses["anttuningen"])
    if anttuningen is None:
        checks.append(DoctorCheck("WARN", f"GTANTTUNINGEN?: unparseable response {redact_text(responses['anttuningen'])!r}"))
    elif anttuningen[0] == 0:
        checks.append(DoctorCheck("WARN", "antenna tuner disabled (GTANTTUNINGEN=0; should be 1)"))
    else:
        checks.append(DoctorCheck("OK", f"antenna tuner enabled (GTANTTUNINGEN={anttuningen[0]})"))

    cfun = _parse_int_tuple(responses["cfun"])
    if cfun is None:
        checks.append(DoctorCheck("WARN", f"CFUN?: unparseable response {redact_text(responses['cfun'])!r}"))
    elif cfun[0] == 1:
        checks.append(DoctorCheck("OK", f"radio functionality: on (CFUN={cfun[0]})"))
    else:
        checks.append(DoctorCheck("WARN", f"radio functionality: CFUN={cfun[0]} (not full functionality)"))

    sim_ready = at_mod.parse_cpin(responses["cpin"])
    sim_text = _sim_text(sim_ready, at_mod.parse_cpin_state(responses["cpin"]))
    checks.append(DoctorCheck("OK" if sim_ready else "WARN", f"SIM: {sim_text}"))

    cesq = cellinfo.parse_cesq(responses["cesq"])
    cells = cellinfo.parse_gtccinfo(responses["gtccinfo"])
    if cellinfo.no_cells_measured(cesq, cells):
        checks.append(DoctorCheck("WARN", _NO_CELLS_HINT))
    else:
        serving = cellinfo.serving_cell(cells)
        neighbours = len(cells) - (1 if serving is not None else 0)
        if serving is not None:
            rat = "LTE" if isinstance(serving, cellinfo.LteCell) else "NR"
            band = f"B{serving.band}" if isinstance(serving, cellinfo.LteCell) else (serving.band or "unknown")
            checks.append(DoctorCheck("OK", f"serving cell: {rat} {band}, {neighbours} neighbour(s)"))
        else:
            checks.append(DoctorCheck("OK", f"{len(cells)} cell(s) measured (no serving cell reported)"))

    return checks


def cmd_doctor(_args: argparse.Namespace, *, at_port_factory=at_mod.AtPort) -> int:
    """Read-only diagnostic checks: firmware/OEM image, DIPC/FCC/antenna-tuner
    state, RAT config and whether any cell is being measured. No root
    needed, never sends a write/set AT command. Exit code 0 if every check
    is OK/INFO, 1 if any is a WARN.
    """
    port = at_port_factory()
    try:
        try:
            responses = _query_all(port, _DOCTOR_COMMANDS)
        except at_mod.AtTimeoutError as exc:
            print(f"doctor: {exc}", file=sys.stderr)
            return 1
        checks = _doctor_checks(responses)
        for check in checks:
            print(f"[{check.level}] {check.message}")
        return 1 if any(check.level == "WARN" for check in checks) else 0
    finally:
        port.close()


def cmd_connect(args: argparse.Namespace, *, at_port_factory=at_mod.AtPort) -> int:
    """Define and activate a PDP context, print the assigned IP and DNS. No root needed."""
    port = at_port_factory()
    try:
        state = at_mod.sim_state(port)
        if state != "READY":
            print(f"SIM not ready ({state})" if state else "SIM not ready", file=sys.stderr)
            return 1
        if args.pdp != DEFAULT_PDP_TYPE:
            print(
                f"warning: PDP type {args.pdp} requested, but the macOS data path only uses IPv4 "
                "(the IPv6 side of this context is not routed)",
                file=sys.stderr,
            )
        try:
            defined, activated = at_mod.setup_pdp(port, args.cid, args.pdp, args.apn)
        except (at_mod.AtCommandError, at_mod.AtTimeoutError) as exc:
            print(_maybe_redact(args, str(exc)), file=sys.stderr)
            return 1
        print(defined)
        print(activated)
        cgpaddr = port.command(f"AT+CGPADDR={args.cid}", timeout=at_mod.QUERY_TIMEOUT_S)
        ip = at_mod.valid_assigned_ipv4(at_mod.parse_cgpaddr(cgpaddr))
        if ip is None:
            if at_mod.cgpaddr_is_ipv6_only(cgpaddr):
                print("no IPv4 address assigned (IPv6-only context; the macOS data path needs IPv4)", file=sys.stderr)
            else:
                print("no IP address assigned", file=sys.stderr)
            return 1
        print(_maybe_redact(args, f"IP: {ip}"))
        servers = at_mod.dns(port, args.cid)
        print(_maybe_redact(args, f"DNS: {servers}"))
    except at_mod.AtTimeoutError as exc:
        print(f"connect: {_maybe_redact(args, str(exc))}", file=sys.stderr)
        return 1
    finally:
        port.close()
    return 0


def cmd_disconnect(args: argparse.Namespace, *, at_port_factory=at_mod.AtPort) -> int:
    """Deactivate the PDP context. No root needed."""
    port = at_port_factory()
    try:
        try:
            response = at_mod.deactivate(port, args.cid)
        except at_mod.AtTimeoutError as exc:
            print(f"disconnect: {exc}", file=sys.stderr)
            return 1
        print(response)
        if not at_mod.is_ok(response):
            return 1
    finally:
        port.close()
    return 0


def _log_bridge_stats(bridge: Bridge | AsyncBridge) -> None:
    _log.info(
        "stats: rx=%d/%dB tx=%d/%dB drops=%d tx_stalls=%d",
        bridge.stats.rx_packets,
        bridge.stats.rx_bytes,
        bridge.stats.tx_packets,
        bridge.stats.tx_bytes,
        bridge.stats.drops,
        bridge.stats.tx_stalls,
    )


_SHUTDOWN_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)


def _run_signal_guarded(stop) -> None:
    """Install SIGINT/SIGTERM/SIGHUP handlers that call ``stop()``, for the
    duration of the caller's blocking wait. Handlers are process-global in
    Python, so this is only safe to call once per invocation (used by
    ``up --loopback``; ``up`` itself uses ``_ShutdownGuard``).
    """

    def _on_signal(_signum, _frame):
        stop()

    for sig in _SHUTDOWN_SIGNALS:
        signal.signal(sig, _on_signal)


class _ShutdownGuard:
    """SIGINT/SIGTERM/SIGHUP handling for ``up``, installed before the first
    network change so a signal at any point still runs the full cleanup
    (without it, SIGTERM/SIGHUP would just kill the process and leave routes
    and DNS behind on the direct/root path).

    From the pump's start until cleanup begins ``stop`` is set to its stop
    callable and a signal just calls it; at any other time (bring-up,
    waiting for a re-enumeration) the signal raises ``KeyboardInterrupt`` so
    the blocking call unwinds into cmd_up's ``finally`` blocks. Once cleanup
    has started (``cleaning``, set before ``stop`` is cleared) further
    signals are only recorded, never raised, so they can't interrupt a
    teardown half-way.
    """

    def __init__(self) -> None:
        self.event = threading.Event()
        self.stop = None
        self.cleaning = False
        self._previous: dict[int, object] = {}

    def install(self) -> None:
        try:
            for sig in _SHUTDOWN_SIGNALS:
                self._previous[sig] = signal.signal(sig, self._on_signal)
        except ValueError:  # not the main thread: signals can't be installed (e.g. some test runners)
            self.restore()

    def restore(self) -> None:
        for sig, handler in self._previous.items():
            signal.signal(sig, handler)
        self._previous.clear()

    def _on_signal(self, _signum, _frame) -> None:
        self.event.set()
        if self.cleaning:
            return
        if self.stop is not None:
            self.stop()
            return
        raise KeyboardInterrupt


def _quiesce_net(net) -> None:
    """Remove DNS, the default route and host routes (in that order) but keep
    the interface itself, best-effort. Used while the modem is away: they'd
    otherwise keep pointing into a utun with nothing behind it.
    """
    for name in ("clear_dns", "remove_default_route", "remove_host_routes"):
        try:
            getattr(net, name)()
        except Exception:
            _log.exception("net.%s() failed", name)


_DRY_RUN_PLACEHOLDER_IP = "192.0.2.2"  # TEST-NET-1: stands in for the address a real activation would assign


def _dry_run_pdp_state(at_port, args: argparse.Namespace) -> str:
    """``up --dry-run``'s only talking to the modem: read-only queries (the
    PDP context's activation state and assigned address; the caller also
    reads the DNS servers). Returns the address to plan the system commands
    with -- the real one if the context is already active, else a placeholder.
    """
    print("dry-run: only read-only AT queries are sent (CPIN?, CGSN, CGACT?, CGPADDR, GTDNS); nothing is written to the modem")
    _log.info("%s", at_port.command("AT+CGACT?", timeout=at_mod.QUERY_TIMEOUT_S))
    ip = at_mod.ip_address(at_port, args.cid)
    if ip is None:
        print(
            f"dry-run: PDP context {args.cid} is not active (no address from AT+CGPADDR); "
            f"planning with the placeholder {_DRY_RUN_PLACEHOLDER_IP}"
        )
        return _DRY_RUN_PLACEHOLDER_IP
    return ip


def _print_dry_run_plan(args: argparse.Namespace, net) -> None:
    """Print what ``up`` would run for real: AT writes, RNDIS steps and (from
    what the recording NetConfig captured) the system commands, in order,
    including teardown.
    """
    print("dry-run: AT commands that WOULD be sent (none were):")
    print(f'  AT+CGDCONT={args.cid},"{args.pdp}","{args.apn}"')
    print(f"  AT+CGACT=1,{args.cid}")
    print(f"  AT+CGACT=0,{args.cid}   (on shutdown)")
    print("dry-run: RNDIS steps that WOULD run (none did): initialize, query MAC, set packet filter; halt on shutdown")
    print("dry-run: system commands that WOULD run, in order, including teardown (none were run):")
    for line in _dry_run_system_commands(net):
        print(f"  {_maybe_redact(args, line)}")
    print("dry-run: skipping utun/bridge pump")


def _dry_run_system_commands(net) -> list[str]:
    lines = []
    for argv in getattr(net, "commands", []):
        if argv and argv[0] == SCUTIL:
            lines.append(f"{SCUTIL} <<< {'; '.join(argv[1:])}")
        else:
            lines.append(" ".join(argv))
    return lines


def _wait_for_reenumeration(find_device_factory, timeout_s: float, sleep=time.sleep, time_source=time.monotonic) -> bool:
    """Poll ``find_device_factory`` every 2s until it succeeds or ``timeout_s`` elapses."""
    deadline = time_source() + timeout_s
    while time_source() < deadline:
        try:
            find_device_factory()
        except Exception:
            sleep(_REENUM_POLL_INTERVAL_S)
            continue
        # Only a probe: drop the cached handle so the rebuild opens a fresh
        # one instead of reusing a handle a second re-enumeration made stale.
        close_cached()
        return True
    return False


def _helper_or_root_error(action: str) -> int:
    print(
        f"{action} needs either the root helper or root. Install the helper once with "
        "'sudo fm350mac helper install' (see 'fm350mac helper status'), or pass --no-helper "
        "to fall back to running this with sudo.",
        file=sys.stderr,
    )
    return 1


def _resolve_helper(
    args: argparse.Namespace, *, geteuid, helper_probe_factory
) -> tuple[bool, HelperNetConfig | None]:
    """Decide whether this invocation uses the helper or the direct/root
    path, for both ``up`` and ``up --loopback``.

    Returns ``(ok, helper_net)``: ``ok`` is False if neither path is usable
    (caller should print an error and return 1); ``helper_net`` is a
    ``HelperNetConfig`` (also usable as the NetConfig for this session) if
    the helper is in use, else None (direct/root path, or --dry-run).
    """
    if args.dry_run:
        return True, None
    if args.no_helper:
        if geteuid() != 0:
            return False, None
        return True, None
    client = helper_probe_factory()
    if client is None:
        return False, None
    return True, HelperNetConfig(client)


def cmd_up(
    args: argparse.Namespace,
    *,
    at_port_factory=at_mod.AtPort,
    find_device_factory=find_device,
    rndis_usb_factory=RndisUsb,
    utun_factory=Utun.open,
    net_config_factory=NetConfig,
    supervisor_factory=Supervisor,
    geteuid=os.geteuid,
    reenum_sleep=time.sleep,
    reenum_time_source=time.monotonic,
    helper_probe_factory=probe_helper,
) -> int:
    """Run the full session: AT PDP context, RNDIS init, utun, routes/DNS, pump.

    Blocks until Ctrl-C (or a fatal bridge failure), then tears down cleanly.
    By default, uses the root helper (see helper_client.py) if it's
    installed and reachable, so ``up`` itself doesn't need root; otherwise
    it prints how to install the helper, or fall back with ``--no-helper``
    (the direct/root path, same as before privilege separation). Skipped
    entirely with ``--dry-run``, which never needs root or the helper and has
    no side effects: only read-only AT queries (no PDP define/activate, no
    RNDIS init/halt), and the AT/system commands it would run are printed. With
    ``--loopback``, skips AT/USB entirely and uses an in-process fake modem
    instead (see loopback.py).

    With ``--supervise`` (default on), a Supervisor polls registration/PDP
    state and reconnects on loss instead of just sleeping. If the bridge
    fails because the USB device disappeared, the session is torn down
    (utun/routes stay up) and rebuilt once the modem re-enumerates -- FM350
    firmware crashes and re-enumerates under real network conditions (see
    rndis_device.py), so this isn't just a theoretical case.
    """
    if args.loopback:
        return _cmd_up_loopback(
            args,
            net_config_factory=net_config_factory,
            utun_factory=utun_factory,
            geteuid=geteuid,
            helper_probe_factory=helper_probe_factory,
        )

    route_hosts = list(dict.fromkeys(getattr(args, "route_host", None) or []))
    if len(route_hosts) > MAX_HOST_ROUTES:
        print(f"--route-host: at most {MAX_HOST_ROUTES} hosts are supported (got {len(route_hosts)})", file=sys.stderr)
        return 1
    if args.dns and not args.default_route:
        print(
            "--dns requires --default-route: without it the carrier's resolver would be queried over the "
            "normal uplink, not the tunnel. Add --default-route, or drop --dns (use --route-host to test "
            "individual hosts through the tunnel).",
            file=sys.stderr,
        )
        return 1

    ok, helper_net = _resolve_helper(args, geteuid=geteuid, helper_probe_factory=helper_probe_factory)
    if not ok:
        return _helper_or_root_error("fm350mac up")
    if helper_net is not None:
        utun_factory = helper_net.open_utun
        net_config_factory = lambda dry_run: helper_net  # noqa: E731 -- dry_run is always False here

    net = net_config_factory(dry_run=args.dry_run)
    utun: Utun | None = None
    utun_name = "utun-dry-run"
    restart_count = 0
    previous_ip: str | None = None
    pinned_imei: str | None = None
    pinned_port_path: tuple[int, tuple[int, ...]] | None = None
    shutdown = _ShutdownGuard()

    try:
        # Before the first network change: a signal from here on runs the
        # full cleanup (see _ShutdownGuard).
        shutdown.install()
        while True:
            at_port = at_port_factory()
            usb_ctx: RndisUsb | None = None
            bridge: Bridge | None = None
            pdp_active = False
            bridge_stopped_cleanly = True
            device_lost = False
            try:
                # Pin the modem's identity across a USB re-enumeration: a
                # different physical device that happens to enumerate at the
                # same VID/PID afterwards must never be silently treated as
                # "the same modem, just back" (see docs/macos-driver.md).
                # AT+CGSN is read-only, so this is safe on every bring-up,
                # not just after a restart.
                current_imei = at_mod.imei(at_port)
                if pinned_imei is None:
                    pinned_imei = current_imei
                    if pinned_imei is None:
                        _log.warning("could not read the modem's IMEI (AT+CGSN); identity pinning across re-enumeration is disabled")
                elif current_imei is not None and current_imei != pinned_imei:
                    print(
                        f"modem identity changed: expected IMEI {_maybe_redact(args, pinned_imei)}, "
                        f"now AT+CGSN reports {_maybe_redact(args, current_imei)!r} "
                        "-- refusing to rebuild the session on what may be a different device",
                        file=sys.stderr,
                    )
                    return 4

                state = at_mod.sim_state(at_port)
                if state != "READY":
                    print(f"SIM not ready ({state})" if state else "SIM not ready", file=sys.stderr)
                    return 1
                if args.dry_run:
                    ip = _dry_run_pdp_state(at_port, args)
                else:
                    try:
                        defined, activated = at_mod.setup_pdp(at_port, args.cid, args.pdp, args.apn)
                    except (at_mod.AtCommandError, at_mod.AtTimeoutError) as exc:
                        # No final result code: the context's state is
                        # unknown (the modem may still finish activating
                        # it), so deactivate it best-effort on cleanup.
                        pdp_active = isinstance(exc, at_mod.AtTimeoutError)
                        print(_maybe_redact(args, str(exc)), file=sys.stderr)
                        return 1
                    _log.info("%s", defined)
                    _log.info("%s", activated)
                    pdp_active = True
                    ip = at_mod.ip_address(at_port, args.cid)
                    if ip is None:
                        print("no IP address assigned", file=sys.stderr)
                        return 1
                    try:
                        ip = _validate_ipv4(ip, "IP address from AT+CGPADDR")
                    except ValueError as exc:
                        print(exc, file=sys.stderr)
                        return 1
                # Always query DNS (read-only) so the log shows what the
                # carrier handed out; it is only *applied* with --dns.
                try:
                    dns_servers = _filter_valid_ips(at_mod.dns(at_port, args.cid))
                except at_mod.AtTimeoutError as exc:
                    _log.warning("AT+GTDNS timed out (%s); treating as no DNS returned", exc)
                    dns_servers = []
                _log.info("%s", _maybe_redact(args, f"IP={ip} DNS={dns_servers if dns_servers else '<none returned>'}"))
                if args.dns and not dns_servers:
                    _log.warning("--dns was requested but the modem returned no usable DNS servers; DNS is left unchanged")

                if not args.dry_run:
                    dev = find_device_factory()
                    try:
                        current_port_path = dev.port_path()
                    except Exception:
                        current_port_path = None  # e.g. a fake device in tests; nothing to compare
                    if pinned_port_path is None:
                        pinned_port_path = current_port_path
                    elif current_port_path is not None and current_port_path != pinned_port_path:
                        _log.warning(
                            "modem re-enumerated on a different USB port (bus/ports %s -> %s); "
                            "continuing anyway (unlike an IMEI mismatch, replugging into another port is not an error)",
                            pinned_port_path, current_port_path,
                        )
                        pinned_port_path = current_port_path
                    usb_ctx = rndis_usb_factory(dev)
                    device = RndisDevice(usb_ctx)
                    init = device.initialize()
                    _log.info("RNDIS initialized: v%d.%d max_transfer_size=%d", init.major, init.minor, init.max_transfer_size)
                    our_mac = device.mac()
                    device.set_packet_filter()
                    _log.info("device MAC: %s", our_mac.hex(":"))

                if args.dry_run:
                    utun_name = "utun-dry-run"
                elif utun is None:
                    utun = utun_factory()
                    utun_name = utun.name
                _log.info("utun interface: %s", utun_name)

                if restart_count == 0 or previous_ip is None:
                    net.configure_interface(utun_name, ip, mtu=1500)
                elif ip != previous_ip:
                    # A restart (device_lost -> re-enumerated) that came back
                    # with a different IP: replace the address in place
                    # rather than stacking a second alias (see
                    # NetConfig.reconfigure_address).
                    net.reconfigure_address(utun_name, previous_ip, ip)
                # else: same IP as before the restart -- nothing to do, the
                # interface is already configured correctly.
                previous_ip = ip

                # Re-added on every (re)build: they're removed while the modem
                # is away (see the cleanup below), and adding is idempotent.
                for host in route_hosts:
                    net.add_host_route(utun_name, host)
                if args.default_route:
                    # Idempotent: a no-op if our route for this interface is
                    # already installed (true on every restart), so it never
                    # re-captures "the previous default" from what is by now
                    # our own route (see NetConfig.add_default_route).
                    net.add_default_route(utun_name)
                if args.dns and dns_servers:
                    net.set_dns(dns_servers)

                if not args.dry_run and utun is not None:
                    our_ip = socket.inet_aton(ip)
                    # Alignment 0: Linux rndis_host ignores it on RX, and 0 is what was verified on hardware.
                    if args.io == "async":
                        bridge = AsyncBridge(
                            usb_ctx, utun, our_mac, our_ip, max_transfer_size=init.max_transfer_size,
                            rx_urbs=args.rx_urbs, tx_urbs=args.tx_urbs,
                            packet_alignment_factor=0,
                        )
                    else:
                        bridge = Bridge(
                            usb_ctx, utun, our_mac, our_ip, max_transfer_size=init.max_transfer_size,
                            packet_alignment_factor=0,
                        )
                    bridge.start()
                    _log.info("bridge running (--io %s); Ctrl-C to stop", args.io)

                    if args.supervise:
                        sup_kwargs = {}
                        if args.dns:
                            def _refresh_dns(_new_ip, _port=at_port):
                                servers = _filter_valid_ips(at_mod.dns(_port, args.cid))
                                if servers:
                                    net.set_dns(servers)
                            sup_kwargs["refresh_dns"] = _refresh_dns
                        sup = supervisor_factory(at_port, bridge, net, utun_name, args.cid, initial_ip=ip, **sup_kwargs)
                        # Stays set until the cleanup below has started, so a
                        # signal in between never raises KeyboardInterrupt
                        # into it (see _ShutdownGuard).
                        shutdown.stop = sup.stop
                        sup.run()
                        # A reconnect may have moved the utun to a new
                        # address; a rebuild must replace that one.
                        if sup.current_ip is not None:
                            previous_ip = sup.current_ip
                        if sup.failure_reason:
                            if bridge.failed.is_set() and bridge.device_lost:
                                device_lost = True
                            else:
                                print(f"bridge failed: {sup.failure_reason}", file=sys.stderr)
                                return 2
                    else:
                        shutdown.stop = shutdown.event.set
                        while not shutdown.event.is_set() and not bridge.failed.is_set():
                            time.sleep(0.5)
                        if bridge.failed.is_set():
                            print(f"bridge failed: {bridge.failure_reason}", file=sys.stderr)
                            return 2
                else:
                    # Nothing above touched the system (commands were only
                    # recorded): record the teardown too, then show the plan.
                    net.teardown()
                    _print_dry_run_plan(args, net)
                    return 0

                if not device_lost:
                    return 0
            except at_mod.AtTimeoutError as exc:
                # A query with no final result code: the modem is wedged or
                # gone. Fail cleanly (the finally below still tears down).
                print(f"modem did not answer: {_maybe_redact(args, str(exc))}", file=sys.stderr)
                return 1
            finally:
                # Further signals must not interrupt cleanup half-way.
                shutdown.cleaning = True
                shutdown.stop = None
                try:
                    # Order matters: stop routing traffic into the tunnel
                    # FIRST (routes/DNS), then stop the bridge, then
                    # deactivate the PDP context, and only then halt RNDIS.
                    # If the modem is merely away (device_lost), keep the
                    # interface and only drop what points into it.
                    if device_lost:
                        _quiesce_net(net)
                    else:
                        try:
                            net.teardown()
                        except Exception:
                            _log.exception("net.teardown() failed")
                    if bridge is not None:
                        try:
                            bridge_stopped_cleanly = bridge.stop()
                        except Exception:
                            _log.exception("bridge.stop() failed")
                            bridge_stopped_cleanly = False
                        _log_bridge_stats(bridge)
                    if pdp_active:
                        try:
                            at_mod.deactivate(at_port, args.cid)
                        except Exception:
                            _log.exception("PDP deactivate failed")
                    if usb_ctx is not None:
                        if bridge_stopped_cleanly:
                            try:
                                RndisDevice(usb_ctx).halt()
                            except Exception:
                                _log.exception("RNDIS halt failed")
                            try:
                                usb_ctx.close()
                            except Exception:
                                _log.exception("usb_ctx.close() failed")
                        else:
                            _log.warning("skipping RNDIS halt/usb close: a bridge thread is still running")
                    try:
                        at_port.close()
                    except Exception:
                        _log.exception("at_port.close() failed")
                finally:
                    shutdown.cleaning = False

            # Reached only when device_lost: the USB device disappeared but
            # --supervise is on. DNS, the default route and host routes were
            # removed above (so the Mac isn't left routing into a dead utun)
            # and are re-added after the rebuild; the utun itself stays. Wait
            # for the modem to re-enumerate, then rebuild AT/RNDIS/bridge.
            if shutdown.event.is_set():
                return 0  # a signal arrived during cleanup: don't wait for the modem
            restart_count += 1
            _log.warning(
                "modem disconnected (restart #%d); waiting up to %.0fs for it to re-enumerate...",
                restart_count, args.reenum_timeout,
            )
            if not _wait_for_reenumeration(find_device_factory, args.reenum_timeout, reenum_sleep, reenum_time_source):
                print(f"modem did not re-enumerate within {args.reenum_timeout:.0f}s; giving up", file=sys.stderr)
                return 3
            _log.info("modem re-enumerated; waiting %.0fs for its firmware to settle", _REENUM_SETTLE_S)
            reenum_sleep(_REENUM_SETTLE_S)
            # loop back around and rebuild the session
    except KeyboardInterrupt:
        # SIGINT/SIGTERM/SIGHUP during bring-up or the re-enumeration wait
        # (see _ShutdownGuard); the finally below runs the cleanup.
        print("interrupted; shutting down", file=sys.stderr)
        return 130
    finally:
        shutdown.cleaning = True
        try:
            net.teardown()
        except Exception:
            _log.exception("net.teardown() failed")
        if utun is not None:
            try:
                utun.close()
            except Exception:
                _log.exception("utun.close() failed")
        if helper_net is not None:
            helper_net.close()
        shutdown.restore()


def _cmd_up_loopback(
    args: argparse.Namespace,
    *,
    net_config_factory=NetConfig,
    utun_factory=Utun.open,
    geteuid=os.geteuid,
    helper_probe_factory=probe_helper,
) -> int:
    """``up --loopback``: exercise utun + netconfig + bridge with an
    in-process fake modem (loopback.LoopbackRndis) instead of real USB/AT.
    No SIM, no modem, needed. TEST-NET addressing; never touches the default
    route even if --default-route was also passed. Uses the root helper by
    default, same as plain ``up`` -- see cmd_up()'s docstring.
    """
    ok, helper_net = _resolve_helper(args, geteuid=geteuid, helper_probe_factory=helper_probe_factory)
    if not ok:
        return _helper_or_root_error("fm350mac up --loopback")
    if helper_net is not None:
        utun_factory = helper_net.open_utun
        net_config_factory = lambda dry_run: helper_net  # noqa: E731 -- dry_run is always False here

    if args.default_route or getattr(args, "route_host", None):
        print("--loopback ignores --default-route/--route-host: only a host route to 198.51.100.1 is added", file=sys.stderr)

    net = net_config_factory(dry_run=args.dry_run)
    utun: Utun | None = None
    bridge: Bridge | None = None

    try:
        if args.dry_run:
            utun_name = "utun-dry-run"
        else:
            utun = utun_factory()
            utun_name = utun.name
        _log.info("utun interface: %s (loopback)", utun_name)

        net.configure_interface(utun_name, loopback.OUR_IP, mtu=1500)
        net.add_host_route(utun_name, loopback.PEER_IP)

        if not args.dry_run and utun is not None:
            modem = loopback.LoopbackRndis()
            our_ip = socket.inet_aton(loopback.OUR_IP)
            bridge = Bridge(modem, utun, loopback.OUR_MAC, our_ip, max_transfer_size=loopback.MAX_TRANSFER_SIZE)
            bridge.start()
            _log.info("loopback bridge running; ping %s to test, Ctrl-C to stop", loopback.PEER_IP)
            stop_flag = threading.Event()
            _run_signal_guarded(stop_flag.set)
            while not stop_flag.is_set() and not bridge.failed.is_set():
                time.sleep(0.5)
            if bridge.failed.is_set():
                print(f"bridge failed: {bridge.failure_reason}", file=sys.stderr)
                return 2
        else:
            print("dry-run: skipping utun/bridge pump")
        return 0
    finally:
        if bridge is not None:
            try:
                bridge.stop()
            except Exception:
                _log.exception("bridge.stop() failed")
            _log_bridge_stats(bridge)
        try:
            net.teardown()
        except Exception:
            _log.exception("net.teardown() failed")
        if utun is not None:
            try:
                utun.close()
            except Exception:
                _log.exception("utun.close() failed")
        if helper_net is not None:
            helper_net.close()


_SELFTEST_RX_URBS = 8
_SELFTEST_RX_PENDING_S = 3.0
_SELFTEST_DRAIN_TIMEOUT_S = 2.0
_SELFTEST_OUT_FRAMES = 5
_SELFTEST_OUT_EXPECT_COMPLETED = 3
_SELFTEST_OUT_EXPECT_TIMED_OUT = 2
_SELFTEST_OUT_WAIT_S = 3.0


def _selftest_arp_probe_frame(our_mac: bytes) -> bytes:
    """A harmless broadcast ARP request, just to give the OUT pool something
    to submit -- the modem doesn't need to actually answer it.
    """
    payload = ethernet.pack_arp(
        ethernet.ARP_REQUEST, sha=our_mac, spa=bytes([192, 0, 2, 1]), tha=b"\x00" * 6, tpa=bytes([192, 0, 2, 2])
    )
    return ethernet.wrap(payload, ethernet.ETH_P_ARP, dst=ethernet.BROADCAST_MAC, src=our_mac)


def cmd_async_selftest(
    _args: argparse.Namespace,
    *,
    find_device_factory=find_device,
    rndis_usb_factory=RndisUsb,
    reenum_sleep=time.sleep,
    reenum_time_source=time.monotonic,
) -> int:
    """Live, read-only(ish) check of the async transfer pools against the
    real modem, with no SIM needed (see docs/macos-driver.md, "Verification
    without a SIM"):

      1. 8 RX transfers pending for 3s (nothing is sent, so 0 frames are
         expected), then cancelled -- all 8 must retire CANCELLED.
      2. 5 ARP frames submitted on an async OUT pool. Without a SIM/bearer
         the modem is known to accept exactly 3 bulk-OUT PACKET_MSGs and
         then NAK every further one (a USB timeout) until a USB reset --
         expect 3 COMPLETED, 2 TIMED_OUT.
      3. RNDIS halt, release interfaces.

    Step 2 leaves the modem's OUT queue jammed on purpose, so this always
    ends with ``UsbDevice.reset()`` and a fresh RNDIS INIT to confirm the
    modem is healthy afterwards -- don't run this more than a couple of
    times in a row.
    """
    overall_ok = True
    dev = find_device_factory()
    try:
        with rndis_usb_factory(dev) as usb_dev:
            device = RndisDevice(usb_dev)
            init = device.initialize()
            print(f"RNDIS initialized: v{init.major}.{init.minor} max_transfer_size={init.max_transfer_size}")
            our_mac = device.mac()

            # --- step 1: RX transfers pending, then cancelled -------------
            rx_frames: list[bytes] = []
            rx_pool = AsyncEndpoint(
                dev.libusb, dev.handle, usb_dev.ep_bulk_in, "in", "bulk",
                count=_SELFTEST_RX_URBS, buffer_size=0x4000, timeout_ms=0,
                on_complete=rx_frames.append,
            )
            loop = EventLoop(dev.libusb, dev.ctx, usb_device=dev)
            loop.register(rx_pool)
            loop.start()
            rx_pool.start()
            print(f"step 1: {_SELFTEST_RX_URBS} RX transfers pending for {_SELFTEST_RX_PENDING_S:.0f}s...")
            time.sleep(_SELFTEST_RX_PENDING_S)
            rx_pool.cancel_all()
            deadline = time.monotonic() + _SELFTEST_DRAIN_TIMEOUT_S
            while time.monotonic() < deadline and not rx_pool.all_retired():
                time.sleep(0.05)
            step1_ok = rx_pool.all_retired() and len(rx_frames) == 0
            overall_ok = overall_ok and step1_ok
            print(
                f"[{'PASS' if step1_ok else 'FAIL'}] step 1: rx cancel -- "
                f"{len(rx_frames)} frames received, all_retired={rx_pool.all_retired()}"
            )
            if rx_pool.all_retired():
                rx_pool.free_all()

            # --- step 2: jam the OUT queue --------------------------------
            print(
                "step 2 submits 5 ARP frames on the OUT pool with no SIM/bearer active: "
                "the modem is expected to accept 3 and then NAK the rest until a USB "
                "reset -- this leaves the modem's TX queue full on purpose; "
                "a reset follows at the end of this selftest."
            )
            out_results: list[bool] = []
            tx_pool = AsyncEndpoint(
                dev.libusb, dev.handle, usb_dev.ep_bulk_out, "out", "bulk",
                count=_SELFTEST_OUT_FRAMES, buffer_size=256, timeout_ms=1000,
                on_out_result=lambda ok, _actual, _meta: out_results.append(ok),
            )
            loop.register(tx_pool)
            msg = rndis.pack_packet(_selftest_arp_probe_frame(our_mac))
            for _ in range(_SELFTEST_OUT_FRAMES):
                if not tx_pool.submit_out(msg):
                    out_results.append(False)
            deadline = time.monotonic() + _SELFTEST_OUT_WAIT_S
            while time.monotonic() < deadline and len(out_results) < _SELFTEST_OUT_FRAMES:
                time.sleep(0.05)
            completed = sum(1 for ok in out_results if ok)
            timed_out = len(out_results) - completed
            step2_ok = completed == _SELFTEST_OUT_EXPECT_COMPLETED and timed_out == _SELFTEST_OUT_EXPECT_TIMED_OUT
            overall_ok = overall_ok and step2_ok
            print(
                f"[{'PASS' if step2_ok else 'FAIL'}] step 2: OUT queue -- {completed} COMPLETED, "
                f"{timed_out} TIMED_OUT (expected {_SELFTEST_OUT_EXPECT_COMPLETED}/{_SELFTEST_OUT_EXPECT_TIMED_OUT})"
            )
            deadline = time.monotonic() + _SELFTEST_DRAIN_TIMEOUT_S
            while time.monotonic() < deadline and not tx_pool.all_retired():
                time.sleep(0.05)
            if tx_pool.all_retired():
                tx_pool.free_all()

            loop.stop()

            # --- step 3: halt + release ------------------------------------
            try:
                device.halt()
                step3_ok = True
            except Exception:
                _log.exception("RNDIS halt failed")
                step3_ok = False
            overall_ok = overall_ok and step3_ok
            print(f"[{'PASS' if step3_ok else 'FAIL'}] step 3: RNDIS halt")
    finally:
        try:
            dev.reset()
            print("USB reset issued (the modem's TX queue was left full by step 2)")
        except Exception:
            _log.exception("USB reset failed")
            overall_ok = False
        dev.close()

    print(f"waiting up to {DEFAULT_REENUM_TIMEOUT_S:.0f}s for the modem to re-enumerate...")
    if not _wait_for_reenumeration(find_device_factory, DEFAULT_REENUM_TIMEOUT_S, reenum_sleep, reenum_time_source):
        print("[FAIL] post-reset: modem did not re-enumerate")
        return 1
    reenum_sleep(_REENUM_SETTLE_S)

    try:
        dev2 = find_device_factory()
        try:
            with rndis_usb_factory(dev2) as usb_dev2:
                RndisDevice(usb_dev2).initialize()
                RndisDevice(usb_dev2).halt()
        finally:
            dev2.close()
        print("[PASS] post-reset probe: modem responds to RNDIS INIT")
    except Exception:
        _log.exception("post-reset probe failed")
        print("[FAIL] post-reset probe: modem did not respond")
        overall_ok = False

    return 0 if overall_ok else 1


def build_parser() -> argparse.ArgumentParser:
    """Build the fm350mac argparse CLI."""
    parser = argparse.ArgumentParser(prog="fm350mac", description="User-space macOS data path for the FM350-GL")
    parser.add_argument("--verbose", action="store_true", help="enable debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    p_probe = sub.add_parser("probe", help="RNDIS init + query OIDs, then halt (no root)")
    p_probe.set_defaults(func=cmd_probe)

    p_at = sub.add_parser("at", help="send raw AT commands (no root)")
    p_at.add_argument("--iface", type=_int_range_arg(0, 31), help="override AT interface number (0..31)")
    p_at.add_argument("--redact", action="store_true", help="mask phone numbers, IMSI/IMEI/ICCID, IP addresses and TAC/cell ID in the output")
    p_at.add_argument("commands", nargs="+")
    p_at.set_defaults(func=cmd_at)

    p_status = sub.add_parser("status", help="SIM/registration/cell/thermal status (no root)")
    p_status.add_argument("--raw", action="store_true", help="also print the underlying raw AT responses")
    p_status.add_argument("--json", action="store_true", help="machine-readable JSON output instead of a summary")
    p_status.add_argument("--redact", action="store_true", help="mask cell ID/TAC in the summary/JSON; with --raw also masks "
        "phone numbers, IMSI/IMEI/ICCID, IP addresses and TAC/cell ID in the raw AT text")
    p_status.add_argument(
        "--watch", nargs="?", type=_positive_float_arg(1.0, inclusive=True), const=DEFAULT_WATCH_INTERVAL_S, default=None, metavar="SECONDS",
        help=f"keep refreshing every SECONDS (default {DEFAULT_WATCH_INTERVAL_S:.0f}) instead of printing once; "
        "for antenna aiming, Ctrl-C to stop",
    )
    p_status.set_defaults(func=cmd_status)

    p_doctor = sub.add_parser("doctor", help="read-only diagnostic checks: OK/WARN/INFO per item (no root)")
    p_doctor.set_defaults(func=cmd_doctor)

    p_connect = sub.add_parser("connect", help="define + activate a PDP context (no root)")
    p_connect.add_argument("--apn", required=True, type=_apn_arg)
    p_connect.add_argument(
        "--pdp", default=DEFAULT_PDP_TYPE, type=_pdp_type_arg,
        help="PDP type (default: IP; IPV6/IPV4V6 are accepted but the macOS data path only uses IPv4)",
    )
    p_connect.add_argument("--cid", type=_int_range_arg(1, 15), default=DEFAULT_CID)
    p_connect.add_argument("--redact", action="store_true", help="mask the assigned IP/DNS in the output")
    p_connect.set_defaults(func=cmd_connect)

    p_disconnect = sub.add_parser("disconnect", help="deactivate the PDP context (no root)")
    p_disconnect.add_argument("--cid", type=_int_range_arg(1, 15), default=DEFAULT_CID)
    p_disconnect.set_defaults(func=cmd_disconnect)

    p_up = sub.add_parser("up", help="run the full session: AT + RNDIS + utun + routes + pump")
    p_up.add_argument("--apn", required=True, type=_apn_arg)
    p_up.add_argument(
        "--pdp", default=DEFAULT_PDP_TYPE, type=_pdp_type_arg, choices=[DEFAULT_PDP_TYPE],
        help="PDP type (only IP: the data path is IPv4-only)",
    )
    p_up.add_argument("--cid", type=_int_range_arg(1, 15), default=DEFAULT_CID)
    p_up.add_argument("--redact", action="store_true", help="mask the assigned IP/DNS and IMEI in logs")
    p_up.add_argument("--default-route", action="store_true", help="route default traffic through the tunnel")
    p_up.add_argument(
        "--route-host", action="append", default=None, type=_route_host_arg, metavar="IP",
        help=f"route just this IPv4 host through the tunnel (host route IP -> utunN); repeatable, max {MAX_HOST_ROUTES}. "
        "The safe way to test on a metered SIM without --default-route",
    )
    p_up.add_argument(
        "--dns", action="store_true",
        help="publish the modem's DNS servers via scutil (requires --default-route; the DNS servers are always "
        "queried and logged either way)",
    )
    p_up.add_argument(
        "--dry-run", action="store_true",
        help="no side effects at all: only read-only AT queries (no CGDCONT/CGACT, no RNDIS init), nothing "
        "run on the system; prints the AT and system commands it WOULD run; no root needed",
    )
    p_up.add_argument(
        "--no-helper", action="store_true",
        help="don't use the root helper: run the direct/root path instead (needs sudo). "
        "By default, 'up' uses the helper (see 'fm350mac helper install'/'status') and needs no root at all.",
    )
    p_up.add_argument(
        "--loopback",
        action="store_true",
        help="smoke-test the utun/route/bridge path with an in-process fake modem; no SIM, no real USB",
    )
    p_up.add_argument(
        "--supervise", dest="supervise", action="store_true", default=True,
        help="poll registration/PDP state and reconnect on loss (default: on)",
    )
    p_up.add_argument("--no-supervise", dest="supervise", action="store_false", help="just sleep until Ctrl-C/failure")
    p_up.add_argument(
        "--reenum-timeout", type=_positive_float_arg(0.0), default=DEFAULT_REENUM_TIMEOUT_S,
        help=f"seconds to wait for the modem to re-enumerate after a USB disconnect (default: {DEFAULT_REENUM_TIMEOUT_S:.0f})",
    )
    p_up.add_argument(
        "--io", choices=("sync", "async"), default="async",
        help="data-path implementation: async keeps several USB transfers in flight per "
        "direction (default), sync is the one-transfer-per-packet fallback",
    )
    p_up.add_argument(
        "--rx-urbs", type=_int_range_arg(1, 64), default=DEFAULT_RX_URBS,
        help=f"number of bulk-IN transfers to keep in flight with --io async (default: {DEFAULT_RX_URBS})",
    )
    p_up.add_argument(
        "--tx-urbs", type=_int_range_arg(1, 64), default=DEFAULT_TX_URBS,
        help=f"number of bulk-OUT transfers to keep in flight with --io async (default: {DEFAULT_TX_URBS})",
    )
    p_up.set_defaults(func=cmd_up)

    p_selftest = sub.add_parser(
        "async-selftest",
        help="live, read-only check of the async USB transfer pools (no SIM needed; ends with a USB reset)",
    )
    p_selftest.set_defaults(func=cmd_async_selftest)

    p_helper = sub.add_parser("helper", help="manage the root helper LaunchDaemon (see docs/macos-driver.md)")
    helper_sub = p_helper.add_subparsers(dest="helper_command", required=True)

    p_helper_install = helper_sub.add_parser("install", help="install and bootstrap the helper LaunchDaemon (needs sudo)")
    p_helper_install.add_argument("--dry-run", action="store_true", help="print the plan without changing anything; no sudo needed")
    p_helper_install.add_argument(
        "--allowed-uid", type=int, default=None,
        help="uid allowed to use the helper (default: $SUDO_UID, i.e. the user who ran sudo)",
    )
    p_helper_install.set_defaults(func=helper_admin.cmd_helper_install)

    p_helper_uninstall = helper_sub.add_parser("uninstall", help="stop and remove the helper LaunchDaemon (needs sudo)")
    p_helper_uninstall.add_argument("--dry-run", action="store_true", help="print the plan without changing anything; no sudo needed")
    p_helper_uninstall.set_defaults(func=helper_admin.cmd_helper_uninstall)

    p_helper_status = helper_sub.add_parser("status", help="report whether the helper is installed and reachable (no sudo needed)")
    p_helper_status.set_defaults(func=helper_admin.cmd_helper_status)

    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point (console script: fm350mac)."""
    parser = build_parser()
    args = parser.parse_args(argv)
    _setup_logging(args.verbose, redact=getattr(args, "redact", False))
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
