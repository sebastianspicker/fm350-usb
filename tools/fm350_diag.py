#!/usr/bin/env python3
"""Staged, safety-gated diagnostics for a Fibocom FM350-GL (incl. Dell
DW5931e) in a Waveshare USB-to-M.2 adapter.

Every AT/adb command this tool can ever send is on a static allowlist with
a safety level (0 read-only .. 3 destructive ADB writes); a subcommand only
runs at the level it declares, and Session.send()/Adb.run() refuse anything
above it before sending a single byte. See ``plan LEVEL`` to print exactly
what a level may do without touching the modem.

Stages (subcommands):
  read         level 0, read-only snapshot + a short sampling window, then
               diagnosis rules print [OK]/[INFO]/[WARN]/[FAIL] and a
               suggested next step.
  volatile     level <=1, transient probes that revert on their own on
               reset/power-cycle (CEREG reject cause, operator scan, a CFUN
               cycle).
  experiment   level <=2, temporarily changes one persistent setting,
               measures, then restores it (writing restore.txt with the
               manual recovery commands before touching anything).
  backup       read-only ADB NV backup (IMEI + RF calibration -- never
               share it, never restore it onto another unit).
  dipc         switch/revert the on-module DIPC config file (level 3, ADB
               root shell; the only stage that can disable USB permanently
               if misused -- see the validator in this file).
  plan LEVEL   print every command a level may send; touches nothing.

See docs/diagnostics.md for the full field guide this automates, and
docs/dell-dw5931e-usb.md / docs/bench-log.md for the hardware history
(including the antenna-pigtail failure the `read` cell-check is built
around). Run with: python3 tools/fm350_diag.py <command> --help
"""

from __future__ import annotations

import argparse
import dataclasses
import glob
import hashlib
import json
import os
import re
import select
import shutil
import subprocess
import sys
import tempfile
import termios
import time
import tty as tty_mod
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Protocol

# Make the sibling fm350mac/src tree importable without installing the
# package (see tools/fm350_at.py, the same pattern).
_FM350MAC_SRC = Path(__file__).resolve().parent.parent / "fm350mac" / "src"
if _FM350MAC_SRC.is_dir():
    sys.path.insert(0, str(_FM350MAC_SRC))

try:
    from fm350mac import at as at_mod
    from fm350mac import cellinfo
    from fm350mac.cli import _GTCCINFO_ROW_RE, _parse_int_tuple  # reuse, don't reinvent
    from fm350mac.usb_async import LIBUSB_ERROR_ACCESS, LIBUSB_ERROR_BUSY, UsbError
except ImportError:
    print("fm350mac package not importable -- run from within the fm350-usb repo", file=sys.stderr)
    raise


class DiagError(Exception):
    """A user-facing fatal error (bad setup, refused precondition)."""


# --- Transports --------------------------------------------------------------
#
# Both wrap the same read-cmd-until-final-result-code protocol AtPort uses,
# behind a small Protocol so tests can inject a fake and nothing here ever
# touches real hardware in a test.


class Transport(Protocol):
    def command(self, cmd: str, timeout: float = 240.0) -> str: ...
    def close(self) -> None: ...
    def usb_reset(self) -> bool: ...


class LibusbTransport:
    """Raw USB bulk transfers via fm350mac.at.AtPort (default on macOS,
    which has no kernel driver for the FM350's vendor serial interfaces).
    """

    def __init__(self, iface_override: int | None = None) -> None:
        self._port = at_mod.AtPort(iface_override=iface_override)

    def command(self, cmd: str, timeout: float = 240.0) -> str:
        return self._port.command(cmd, timeout=timeout)

    def close(self) -> None:
        self._port.close()

    def usb_reset(self) -> bool:
        self._port.usb_device.reset()
        return True


class TtyTransport:
    """A kernel-exposed ttyUSB node for the FM350's AT interface (Linux
    only: the 'option' driver claims interface 6/4 and creates a tty).

    Same protocol as AtPort (write ``cmd + "\\r"``, read until a final
    result code) over raw termios I/O -- deliberately reuses AtPort's own
    final-result regex (``at_mod._FINAL_RESULT_RE``) so the two transports
    can never drift on what counts as "done".
    """

    _BAUD = termios.B115200

    def __init__(self, path: str) -> None:
        self.path = path
        self.fd = os.open(path, os.O_RDWR | os.O_NOCTTY)
        tty_mod.setraw(self.fd)
        attrs = termios.tcgetattr(self.fd)
        termios.cfsetispeed(attrs, self._BAUD)
        termios.cfsetospeed(attrs, self._BAUD)
        termios.tcsetattr(self.fd, termios.TCSANOW, attrs)
        self._drain(0.2)  # discard unsolicited output left over from boot

    def _drain(self, timeout_s: float) -> str:
        out = b""
        while True:
            ready, _, _ = select.select([self.fd], [], [], timeout_s)
            if not ready:
                return out.decode(errors="replace")
            out += os.read(self.fd, 4096)

    def command(self, cmd: str, timeout: float = 240.0) -> str:
        os.write(self.fd, (cmd + "\r").encode())
        buf = ""
        deadline = time.time() + timeout
        while time.time() < deadline:
            buf += self._drain(0.05)
            if at_mod._FINAL_RESULT_RE.search(buf):
                break
        return buf.strip()

    def close(self) -> None:
        # Idempotent: after a failed re-enumeration the session still holds this
        # transport, and the caller's finally closes it a second time.
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1

    def usb_reset(self) -> bool:
        print("usb-reset is not supported over a tty transport (use libusb, i.e. omit --tty)")
        return False


def _detect_linux_tty() -> str | None:
    """Glob for the ttyUSB node the kernel 'option' driver creates on the AT
    interface (6 on most FM350 PIDs, 4 on 0e8d:7126 -- see at.PIDS).
    """
    for suffix in ("*:1.6", "*:1.4"):
        matches = sorted(glob.glob(f"/sys/bus/usb/devices/{suffix}/ttyUSB*"))
        if matches:
            return "/dev/" + os.path.basename(matches[0])
    return None


def open_transport(*, tty: str | None = None, iface: int | None = None) -> Transport:
    """Open the AT transport: an explicit --tty, an auto-detected Linux
    ttyUSB node, or libusb (default on macOS, and the Linux fallback).
    """
    if tty:
        return TtyTransport(tty)
    if sys.platform.startswith("linux"):
        auto = _detect_linux_tty()
        if auto:
            return TtyTransport(auto)
    try:
        return LibusbTransport(iface_override=iface)
    except UsbError as exc:
        if sys.platform.startswith("linux") and exc.code in (LIBUSB_ERROR_ACCESS, LIBUSB_ERROR_BUSY):
            raise DiagError(
                f"cannot open the FM350 AT interface via libusb ({exc}); the kernel's 'option' "
                "driver likely holds it -- try --tty /dev/ttyUSBn instead"
            ) from exc
        raise


# --- The safety guard ---------------------------------------------------------
#
# Every AT command this tool may ever send is here, with the level it needs.
# Nothing outside this table (or AT_COMMAND_PATTERNS below) can be sent --
# Session.send() looks commands up here, never trusts a caller's say-so.


class SafetyError(Exception):
    """Raised by Session.send()/Adb.run() for an unknown command, or one
    above the session's granted level. Nothing is sent when this is raised.
    """


AT_COMMANDS: dict[str, int] = {
    # Level 0: read-only queries, safe at any time.
    "AT": 0,
    "ATI": 0,
    "AT+CGMR": 0,
    "AT+GTPKGVER?": 0,
    "AT+CGSN": 0,
    "AT+GTUSBMODE?": 0,
    "AT+GTDIPCMODE?": 0,
    "AT+GTCURCAR?": 0,
    "AT+GTLOCKCAR?": 0,
    "AT+GTFCCEFFSTATUS?": 0,
    "AT+GTFCCLOCKMODE?": 0,
    "AT+GTFMODE?": 0,
    "AT+CFUN?": 0,
    "AT+GTANTTUNINGEN?": 0,
    "AT+BODYSAREN?": 0,
    "AT+GTRXPATHEN?": 0,
    "AT+ECAL?": 0,
    "AT+CPIN?": 0,
    "AT+SIMTYPE?": 0,
    "AT+GTDUALSIM?": 0,
    "AT+MSMPD?": 0,
    "AT+CIMI": 0,
    "AT+ICCID": 0,
    "AT+GTACT?": 0,
    "AT+ERAT?": 0,
    "AT+E5GOPT?": 0,
    "AT+COPS?": 0,
    "AT+CEREG?": 0,
    "AT+C5GREG?": 0,
    "AT+CESQ": 0,
    "AT+GTCCINFO?": 0,
    "AT+CEER": 0,
    "AT+CGDCONT?": 0,
    "AT+GTSENRDTEMP=0": 0,  # reads sensor 0, changes nothing
    # Level 1: volatile -- reverts on its own at the next reset/power cycle.
    "AT+CMEE=2": 1,
    "AT+COPS=?": 1,
    "AT+CFUN=4": 1,
    "AT+CFUN=1": 1,
    "AT+CFUN=15": 1,
    # Level 2: persistent, but every caller in this tool restores it (see
    # the `experiment` template).
    "AT+GTFCCLOCKGEN": 2,
}

# Commands with an integer argument this tool constructs itself (never raw
# from argv) are matched by exact full-match regex instead of an infinite
# table. Full-match ($ anchored) so e.g. "AT+GTFMODE=0,0;AT+CFUN=15" or a
# non-integer argument is rejected, not just the valid prefix.
AT_COMMAND_PATTERNS: tuple[tuple[re.Pattern[str], int], ...] = (
    (re.compile(r"^AT\+CEREG=[0-5]$"), 1),  # 3GPP TS 27.007 <n> 0-5
    (re.compile(r"^AT\+GTFMODE=[01],[01]$"), 2),
    (re.compile(r"^AT\+GTANTTUNINGEN=[01]$"), 2),
    (re.compile(r"^AT\+ERAT=\d+$"), 2),
    (re.compile(r"^AT\+GTFCCLOCKVER=\d+$"), 2),
)


def command_level(cmd: str) -> int | None:
    """The safety level required to send ``cmd``, or None if it's on no allowlist."""
    if cmd in AT_COMMANDS:
        return AT_COMMANDS[cmd]
    for pattern, level in AT_COMMAND_PATTERNS:
        if pattern.match(cmd):
            return level
    return None


# --- Redaction -----------------------------------------------------------------
#
# On by default (--no-redact disables it); applied to everything printed or
# written (transcript, report, journal).

_BARE_ID_RE = re.compile(r"\b\d{14,20}\b")  # IMEI/IMSI/ICCID: bare 14-20 digit runs
_CEREG_TAC_CI_RE = re.compile(r'(\+C(?:E|5G)REG:\s*\d+\s*,\s*\d+\s*,\s*)"[0-9A-Fa-f]+"(\s*,\s*)"[0-9A-Fa-f]+"')


def redact_gtccinfo(text: str) -> str:
    """Mask the TAC and cell-id fields of any +GTCCINFO row (reuses cli.py's
    row regex -- same field layout, just a different replacement token).
    """
    return "\n".join(_GTCCINFO_ROW_RE.sub(r"\1<redacted>\2<redacted>\3", line) for line in text.splitlines())


def redact_text(text: str) -> str:
    """Mask IMEI/IMSI/ICCID, GTCCINFO TAC/cell-id, and CEREG/C5GREG quoted tac/ci."""
    text = redact_gtccinfo(text)
    text = _CEREG_TAC_CI_RE.sub(r'\1"<redacted>"\2"<redacted>"', text)
    text = _BARE_ID_RE.sub("<redacted>", text)
    return text


# --- Session: the safety-gated AT channel --------------------------------------


class _JournalWriter:
    def __init__(self, path: Path) -> None:
        self.path = path

    def append(self, record: dict) -> None:
        with self.path.open("a") as f:
            f.write(json.dumps(record) + "\n")


class Session:
    """A safety-gated AT command channel: send() refuses anything above
    ``level`` (see command_level()) before sending a byte, and journals
    every level>=1 command to journal.jsonl in ``out_dir`` -- an "intent"
    line before it's sent, a "result" line after.
    """

    def __init__(
        self,
        transport: Transport,
        level: int,
        out_dir: Path,
        *,
        redact: bool = True,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.transport = transport
        self.level = level
        self.out_dir = out_dir
        self.redact = redact
        self._clock = clock
        self.transcript: list[tuple[str, str]] = []
        self.sent_levels: dict[int, int] = {}
        self._journal: _JournalWriter | None = None

    def _journal_writer(self) -> _JournalWriter:
        if self._journal is None:
            self._journal = _JournalWriter(self.out_dir / "journal.jsonl")
        return self._journal

    def rebind(self, transport: Transport) -> None:
        """Swap in a new transport after a reset/re-enumeration; level,
        journal and transcript continue as before.
        """
        self.transport = transport

    def send(self, cmd: str, timeout: float = 240.0) -> str:
        level = command_level(cmd)
        if level is None:
            raise SafetyError(f"unknown command, refusing to send: {cmd!r}")
        if level > self.level:
            raise SafetyError(f"{cmd!r} requires level {level}, this session is level {self.level}")
        if level >= 1:
            self._journal_writer().append({"ts": self._clock(), "level": level, "cmd": cmd, "phase": "intent"})
        response = self.transport.command(cmd, timeout=timeout)
        self.sent_levels[level] = self.sent_levels.get(level, 0) + 1
        display = redact_text(response) if self.redact else response
        self.transcript.append((cmd, display))
        if level >= 1:
            self._journal_writer().append(
                {"ts": self._clock(), "level": level, "cmd": cmd, "phase": "result", "response": display}
            )
        return response

    def write_transcript(self) -> None:
        lines = []
        for cmd, response in self.transcript:
            lines.append(f">>> {cmd}")
            lines.append(response)
            lines.append("")
        (self.out_dir / "transcript.txt").write_text("\n".join(lines))


# --- ADB: the same allowlist pattern, for the on-module root shell -------------

_ADB_TIMEOUT_S = 15.0
_ADB_BACKUP_TIMEOUT_S = 900.0  # a raw mtdblock can be >100 MB over USB
_ADB_BACKUP_DIRS = ("nvram", "nvdata", "nvcfg", "protect_f", "protect_s", "mdota", "mdota2", "mdota3")

_ADB_LEVEL0_EXACT: frozenset[str] = frozenset(
    {
        "id",
        "cat /etc/vendor_info",
        "cat /mnt/vendor/nvdata/md_cmn/dipc_config",
        "cat /mnt/vendor/nvdata/md_cmn/dipc_config.orig",
        "cat /mnt/vendor/nvdata/md_cmn/dipc_config.orig-dell",
        "ls -l /mnt/vendor/nvdata/md_cmn/",
        "cat /proc/mtd",
        "logread | grep -E 'MIPC_NW_RADIO_STATE|NW_REGISTER_STATE|IMEI CHECK' | tail -n 30",
    }
    | {f"tar -C /mnt/vendor -cf - {name}" for name in _ADB_BACKUP_DIRS}
)
_ADB_LEVEL0_PATTERNS: tuple[re.Pattern[str], ...] = (re.compile(r"^cat /dev/mtdblock\d+$"),)

_DIPC_DIR = "/mnt/vendor/nvdata/md_cmn"
_DIPC_PATH = f"{_DIPC_DIR}/dipc_config"
_ADB_LEVEL3_PUSH_DEST = f"{_DIPC_DIR}/dipc_config.new"
# Every write is guarded by a shell test so it short-circuits before the
# redirect: `cat missing > dipc_config` would truncate the file to empty,
# and an empty/invalid dipc_config is the one outcome that can cut USB off.
# `cat src > dst` rather than cp/mv keeps dipc_config's owner and mode.
_DIPC_CP_ORIG = f"cd {_DIPC_DIR} && [ ! -e dipc_config.orig ] && cp -p dipc_config dipc_config.orig"
_DIPC_INSTALL_NEW = f"cd {_DIPC_DIR} && [ -s dipc_config.new ] && cat dipc_config.new > dipc_config && sync"
_DIPC_RM_NEW = f"cd {_DIPC_DIR} && rm -f dipc_config.new"


def _dipc_restore_cmd(orig_name: str) -> str:
    return f"cd {_DIPC_DIR} && [ -s {orig_name} ] && cat {orig_name} > dipc_config && sync"


_DIPC_ORIG_NAMES = ("dipc_config.orig", "dipc_config.orig-dell")
_ADB_LEVEL3_EXACT: frozenset[str] = frozenset(
    {
        _DIPC_CP_ORIG,
        f"cat {_DIPC_DIR}/dipc_config.new",
        _DIPC_INSTALL_NEW,
        _DIPC_RM_NEW,
        *(_dipc_restore_cmd(name) for name in _DIPC_ORIG_NAMES),
    }
)


class AdbError(Exception):
    """Raised for an allowlist violation, or adb itself failing/missing."""


class Adb:
    """A safety-gated adb wrapper: run() only accepts exact allowlisted
    shell strings for the requested level -- never shell=True, and never a
    string built from untrusted input (the backup dir list and mtdblock
    numbers are the tool's own constants/parsed-from-modem-output, not argv).
    """

    def __init__(self, binary: str | None = None) -> None:
        self.binary = binary if binary is not None else shutil.which("adb")

    def available(self) -> bool:
        return self.binary is not None

    def devices(self) -> str:
        if not self.available():
            raise AdbError("adb not found on PATH")
        result = subprocess.run([self.binary, "devices"], capture_output=True, text=True, timeout=_ADB_TIMEOUT_S)
        return result.stdout

    def run(
        self, cmd: str, level: int = 0, *, binary_output: bool = False, timeout: float = _ADB_TIMEOUT_S
    ) -> bytes | str:
        if level == 0:
            allowed = cmd in _ADB_LEVEL0_EXACT or any(p.match(cmd) for p in _ADB_LEVEL0_PATTERNS)
        elif level == 3:
            allowed = cmd in _ADB_LEVEL3_EXACT
        else:
            allowed = False
        if not allowed:
            raise SafetyError(f"adb command not allowlisted at level {level}: {cmd!r}")
        if not self.available():
            raise AdbError("adb not found on PATH")
        try:
            result = subprocess.run([self.binary, "exec-out", cmd], capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise AdbError(f"adb timed out after {timeout:.0f}s: {cmd!r}") from exc
        if result.returncode != 0 and not result.stdout:
            raise AdbError(f"adb failed (exit {result.returncode}): {cmd!r}: {result.stderr!r}")
        return result.stdout if binary_output else result.stdout.decode(errors="replace")

    def push(self, local: Path, remote: str) -> None:
        if remote != _ADB_LEVEL3_PUSH_DEST:
            raise SafetyError(f"adb push destination not allowlisted: {remote!r}")
        if not self.available():
            raise AdbError("adb not found on PATH")
        try:
            subprocess.run([self.binary, "push", str(local), remote], capture_output=True, timeout=_ADB_TIMEOUT_S, check=True)
        except (subprocess.TimeoutExpired, subprocess.CalledProcessError) as exc:
            raise AdbError(f"adb push failed: {exc}") from exc


# --- Shared data shapes ---------------------------------------------------------


@dataclasses.dataclass
class Check:
    """One diagnosis result line."""

    level: str  # "OK" | "INFO" | "WARN" | "FAIL"
    message: str


@dataclasses.dataclass
class Sample:
    """One CEREG/C5GREG/CESQ/GTCCINFO sampling round."""

    cereg_stat: int | None
    c5greg_stat: int | None
    cesq: "cellinfo.SignalQuality | None"
    cells: "list[cellinfo.LteCell | cellinfo.NrCell]"
    no_cells: bool


_REG_STAT_NAMES = {
    0: "not registered",
    1: "home",
    2: "searching",
    3: "registration denied",
    4: "unknown",
    5: "roaming",
}


# --- Diagnosis rules (pure functions over collected data) -----------------------

_NO_CELLS_HINT = (
    "No cell measured on any sample. Check antenna pigtails and MHF4 connectors FIRST -- on our unit "
    "this exact symptom was defective pigtails, after a day of ruling out firmware causes "
    "(see docs/diagnostics.md)"
)


def check_oem_image(pkgver_response: str) -> Check:
    match = re.search(r'"([^"]*)"', pkgver_response)
    pkgver = match.group(1) if match else pkgver_response.strip()
    oem_match = re.search(r"_(\d{4})\.", pkgver)
    if oem_match and oem_match.group(1) == "5025":
        return Check("INFO", f"firmware package: {pkgver} (Dell DW5931e)")
    if oem_match:
        return Check("INFO", f"firmware package: {pkgver} (OEM image {oem_match.group(1)})")
    return Check("INFO", f"firmware package: {pkgver}")


def check_dipc(dipcmode_response: str) -> Check:
    dipc = _parse_int_tuple(dipcmode_response)
    if dipc is None:
        return Check("WARN", f"GTDIPCMODE?: unparseable response {dipcmode_response!r}")
    if dipc[0] == 1:
        return Check("INFO", f"DIPC mode {dipc[0]} (PCIe Advance: USB works fine without a PCIe link)")
    if dipc[0] == 3:
        return Check("OK", f"DIPC mode {dipc[0]} (dual: USB always on)")
    return Check("WARN", f"DIPC mode {dipc[0]}: USB may be disabled in this mode")


def check_fcc_lock(fcceffstatus_response: str, oem_image_is_dell: bool) -> Check:
    fcc = _parse_int_tuple(fcceffstatus_response)
    if fcc is None or len(fcc) < 2:
        return Check("WARN", f"GTFCCEFFSTATUS?: unparseable response {fcceffstatus_response!r}")
    if fcc[1] == 1:
        return Check("OK", f"FCC lock: unlocked (mode={fcc[0]}, status={fcc[1]})")
    suggestion = (
        "run `experiment fcc-unlock-dell`"
        if oem_image_is_dell
        else "use mrhaav's fm350_fcc_unlock.sh "
        "(https://github.com/mrhaav/openwrt/blob/master/atc/fib-fm350_gl/fm350_fcc_unlock.sh)"
    )
    return Check("FAIL", f"FCC locked (mode={fcc[0]}, status={fcc[1]}); {suggestion}")


def check_cfun(cfun_response: str) -> Check:
    cfun = _parse_int_tuple(cfun_response)
    if cfun is None:
        return Check("WARN", f"CFUN?: unparseable response {cfun_response!r}")
    if cfun[0] == 1:
        return Check("OK", f"radio functionality: on (CFUN={cfun[0]})")
    return Check("WARN", f"radio off (CFUN={cfun[0]}); `volatile` cycles CFUN back to 1")


def check_sim(cpin_response: str, dualsim_response: str | None = None) -> list[Check]:
    checks: list[Check] = []
    if "+CPIN: READY" in cpin_response:
        checks.append(Check("OK", "SIM: ready"))
    elif "not inserted" in cpin_response.lower():
        checks.append(
            Check(
                "FAIL",
                "SIM not inserted: reseat the SIM, power-cycle, and check the Waveshare wires slot 1 only",
            )
        )
    else:
        checks.append(Check("WARN", f"SIM: not ready ({cpin_response.strip()!r})"))
    if dualsim_response is not None:
        dualsim = _parse_int_tuple(dualsim_response)
        if dualsim is not None and dualsim[0] != 0:
            checks.append(Check("WARN", f"GTDUALSIM slot {dualsim[0]} selected (expected slot 0)"))
    return checks


def check_anttuner(anttuningen_response: str) -> Check:
    anttuningen = _parse_int_tuple(anttuningen_response)
    if anttuningen is None:
        return Check("WARN", f"GTANTTUNINGEN?: unparseable response {anttuningen_response!r}")
    if anttuningen[0] == 0:
        return Check(
            "WARN",
            "antenna tuner disabled (GTANTTUNINGEN=0; should be 1) -- likely left over from an experiment; "
            "restore with `AT+GTANTTUNINGEN=1` (or re-run `experiment anttuner`, which restores the value it read)",
        )
    return Check("OK", f"antenna tuner enabled (GTANTTUNINGEN={anttuningen[0]})")


def check_rat_mode(erat_response: str) -> Check:
    erat = cellinfo.parse_erat(erat_response)
    if erat is None:
        return Check("WARN", f"ERAT?: unparseable response {erat_response!r}")
    if erat.rat_mode != 21:
        return Check(
            "INFO",
            f"non-default RAT mode: {erat.rat_mode_name} (ERAT persists across CFUN=15 on this firmware)",
        )
    return Check("OK", f"RAT mode: {erat.rat_mode_name}")


def check_temperature(temperature_c: float | None) -> Check | None:
    if temperature_c is None:
        return None
    if temperature_c > 70:
        return Check("WARN", f"temperature {temperature_c:.1f} C (> 70 C)")
    return Check("OK", f"temperature {temperature_c:.1f} C")


def check_cells(samples: list[Sample]) -> Check:
    if not samples:
        return Check("WARN", "no samples collected")
    no_cell_count = sum(1 for s in samples if s.no_cells)
    if no_cell_count == len(samples):
        return Check("FAIL", _NO_CELLS_HINT)
    if no_cell_count:
        return Check("WARN", f"cells measured in only {len(samples) - no_cell_count}/{len(samples)} samples")
    return Check("OK", f"cells measured in {len(samples)}/{len(samples)} samples")


def _serving_rsrp(cell: "cellinfo.LteCell | cellinfo.NrCell") -> float | None:
    return cell.rsrp_dbm if isinstance(cell, cellinfo.LteCell) else cell.ss_rsrp_dbm


def check_registration(samples: list[Sample], ceer_response: str | None = None) -> Check:
    for sample in samples:
        if at_mod.is_registered(sample.cereg_stat) or at_mod.is_registered(sample.c5greg_stat):
            serving = cellinfo.serving_cell(sample.cells)
            if serving is None:
                return Check("OK", f"registered ({len(sample.cells)} cell(s), no serving cell reported)")
            neighbours = len(sample.cells) - 1
            rat = "LTE" if isinstance(serving, cellinfo.LteCell) else "NR"
            band = f"B{serving.band}" if isinstance(serving, cellinfo.LteCell) else (serving.band or "unknown")
            rsrp = _serving_rsrp(serving)
            msg = f"registered: {rat} {band}, RSRP {rsrp} dBm, {neighbours} neighbour(s)"
            if rsrp is not None and rsrp < -110:
                return Check("WARN", f"{msg} (weak signal, RSRP < -110 dBm)")
            return Check("OK", msg)
    if any(not s.no_cells for s in samples):
        stat_name = _REG_STAT_NAMES.get(samples[-1].cereg_stat, f"stat={samples[-1].cereg_stat}")
        msg = f"cells seen but never registered (last CEREG stat: {stat_name})"
        if ceer_response:
            msg += f"; AT+CEER: {ceer_response.strip()}"
        msg += "; try `volatile` (reject cause via CEREG=3, operator scan)"
        return Check("WARN", msg)
    return Check("INFO", "no registration attempted (no cells seen)")


def check_cgdcont(cgdcont_response: str) -> Check | None:
    if "+CGDCONT:" in cgdcont_response:
        return None
    return Check("INFO", "no APN defined yet")


def check_adb_device_state(devices_output: str) -> Check:
    if "\tdevice" in devices_output:
        return Check("OK", "ADB device state: device")
    if "\toffline" in devices_output:
        return Check("WARN", "ADB device state: offline (a USB reset fixes this: `volatile --usb-reset`)")
    return Check("INFO", "ADB device: not found (skipping ADB checks)")


def check_radio_state(logread_text: str) -> Check:
    if re.search(r"hw=1,\s*sw=1", logread_text):
        return Check("OK", "radio state: hw=1, sw=1 (on)")
    return Check("WARN", f"radio state: could not confirm hw=1/sw=1 in log ({logread_text.strip()!r})")


# --- USB presence check ----------------------------------------------------------


def usb_present() -> bool:
    if sys.platform == "darwin":
        try:
            out = subprocess.run(
                ["ioreg", "-p", "IOUSB", "-w0"], capture_output=True, text=True, timeout=5
            ).stdout
        except OSError:
            return False
        return "FM350" in out or "0e8d" in out.lower()
    for vendor_path in glob.glob("/sys/bus/usb/devices/*/idVendor"):
        try:
            vendor = Path(vendor_path).read_text().strip()
            product = Path(vendor_path).parent.joinpath("idProduct").read_text().strip()
        except OSError:
            continue
        if vendor == "0e8d" and product in ("7127", "7126"):
            return True
    return False


# --- Report writing ----------------------------------------------------------

_SAFETY_BANNER = (
    "This changes modem state. Tested on one Dell DW5931e (FW 29.20.22, OEM 5025) on 2026-09-25 "
    "without damage; no warranty (MIT, as is). See docs/diagnostics.md."
)


def _git_commit() -> str | None:
    git = shutil.which("git")
    if git is None:
        return None
    try:
        result = subprocess.run(
            [git, "rev-parse", "--short", "HEAD"],
            cwd=Path(__file__).resolve().parent.parent,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except OSError:
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def write_report(out_dir: Path, session: Session, title: str, checks: list[Check]) -> None:
    total = sum(session.sent_levels.values())
    levels_used = ", ".join(f"L{level}:{count}" for level, count in sorted(session.sent_levels.items()))
    commit = _git_commit()
    lines = [
        f"# {title}",
        "",
        f"- session level: {session.level}",
        f"- commands sent: {total} (levels used: {levels_used or 'none'})",
    ]
    if commit:
        lines.append(f"- tool commit: {commit}")
    lines += ["", "## Checks", ""]
    lines += [f"- [{c.level}] {c.message}" for c in checks]
    (out_dir / "report.md").write_text("\n".join(lines) + "\n")
    (out_dir / "report.json").write_text(
        json.dumps(
            {
                "title": title,
                "level": session.level,
                "commands_sent": total,
                "levels_used": session.sent_levels,
                "tool_commit": commit,
                "checks": [dataclasses.asdict(c) for c in checks],
            },
            indent=2,
        )
    )
    session.write_transcript()


def _print_suggestions(checks: list[Check]) -> None:
    worst = [c for c in checks if c.level == "FAIL"] or [c for c in checks if c.level == "WARN"]
    if not worst:
        return
    print("\nSuggested next step:")
    for c in worst:
        print(f"- {c.message}")


# --- read ----------------------------------------------------------------------

DEFAULT_READ_DURATION_S = 60.0
DEFAULT_READ_INTERVAL_S = 10.0

# Every level-0 query `read` sends once, up front. Must exactly match the
# level-0 entries of AT_COMMANDS (checked in tests) -- this is the allowlist
# stated as a plain command list rather than derived from it, so a change to
# one is a visible diff against the other.
_LEVEL0_SNAPSHOT_COMMANDS: tuple[tuple[str, str], ...] = (
    ("at", "AT"),
    ("ati", "ATI"),
    ("cgmr", "AT+CGMR"),
    ("gtpkgver", "AT+GTPKGVER?"),
    ("cgsn", "AT+CGSN"),
    ("gtusbmode", "AT+GTUSBMODE?"),
    ("gtdipcmode", "AT+GTDIPCMODE?"),
    ("gtcurcar", "AT+GTCURCAR?"),
    ("gtlockcar", "AT+GTLOCKCAR?"),
    ("gtfcceffstatus", "AT+GTFCCEFFSTATUS?"),
    ("gtfcclockmode", "AT+GTFCCLOCKMODE?"),
    ("gtfmode", "AT+GTFMODE?"),
    ("cfun", "AT+CFUN?"),
    ("gtanttuningen", "AT+GTANTTUNINGEN?"),
    ("bodysaren", "AT+BODYSAREN?"),
    ("gtrxpathen", "AT+GTRXPATHEN?"),
    ("ecal", "AT+ECAL?"),
    ("cpin", "AT+CPIN?"),
    ("simtype", "AT+SIMTYPE?"),
    ("gtdualsim", "AT+GTDUALSIM?"),
    ("msmpd", "AT+MSMPD?"),
    ("cimi", "AT+CIMI"),
    ("iccid", "AT+ICCID"),
    ("gtact", "AT+GTACT?"),
    ("erat", "AT+ERAT?"),
    ("e5gopt", "AT+E5GOPT?"),
    ("cops", "AT+COPS?"),
    ("cereg", "AT+CEREG?"),
    ("c5greg", "AT+C5GREG?"),
    ("cesq", "AT+CESQ"),
    ("gtccinfo", "AT+GTCCINFO?"),
    ("ceer", "AT+CEER"),
    ("cgdcont", "AT+CGDCONT?"),
    ("gtsenrdtemp", "AT+GTSENRDTEMP=0"),
)


def _collect_snapshot(session: Session) -> dict[str, str]:
    return {label: session.send(cmd) for label, cmd in _LEVEL0_SNAPSHOT_COMMANDS}


def _collect_one_sample(session: Session) -> Sample:
    cereg = session.send("AT+CEREG?")
    c5greg = session.send("AT+C5GREG?")
    session.send("AT+COPS?")
    cesq_resp = session.send("AT+CESQ")
    gtcc_resp = session.send("AT+GTCCINFO?")
    cesq = cellinfo.parse_cesq(cesq_resp)
    cells = cellinfo.parse_gtccinfo(gtcc_resp)
    return Sample(
        cereg_stat=at_mod.parse_registration(cereg),
        c5greg_stat=at_mod.parse_registration(c5greg),
        cesq=cesq,
        cells=cells,
        no_cells=cellinfo.no_cells_measured(cesq, cells),
    )


def _collect_samples(
    session: Session,
    duration: float,
    interval: float,
    sleep: Callable[[float], None],
    monotonic: Callable[[], float],
) -> list[Sample]:
    samples = [_collect_one_sample(session)]
    start = monotonic()
    while monotonic() - start < duration:
        sleep(interval)
        samples.append(_collect_one_sample(session))
    return samples


def _diagnose(snapshot: dict[str, str], samples: list[Sample], adb_checks: list[Check]) -> list[Check]:
    checks: list[Check] = []
    oem_check = check_oem_image(snapshot["gtpkgver"])
    checks.append(oem_check)
    is_dell = "Dell DW5931e" in oem_check.message
    checks.append(check_dipc(snapshot["gtdipcmode"]))
    checks.append(check_fcc_lock(snapshot["gtfcceffstatus"], is_dell))
    checks.append(check_cfun(snapshot["cfun"]))
    checks.extend(check_sim(snapshot["cpin"], snapshot.get("gtdualsim")))
    checks.append(check_anttuner(snapshot["gtanttuningen"]))
    checks.append(check_rat_mode(snapshot["erat"]))
    temp_c = cellinfo.millidegrees_to_celsius(cellinfo.parse_gtsenrdtemp(snapshot["gtsenrdtemp"]))
    temp_check = check_temperature(temp_c)
    if temp_check is not None:
        checks.append(temp_check)
    checks.append(check_cells(samples))
    checks.append(check_registration(samples, snapshot.get("ceer")))
    cgdcont_check = check_cgdcont(snapshot["cgdcont"])
    if cgdcont_check is not None:
        checks.append(cgdcont_check)
    checks.extend(adb_checks)
    return checks


def _run_adb_read_checks(adb: Adb, out_dir: Path, redact: bool = True) -> list[Check]:
    if not adb.available():
        return [Check("INFO", "adb not found on PATH; skipping ADB checks")]
    checks: list[Check] = []
    devices_output = adb.devices()
    checks.append(check_adb_device_state(devices_output))
    if "\tdevice" not in devices_output:
        return checks
    def clean(text: str) -> str:
        return redact_text(text) if redact else text

    vendor_info = clean(adb.run("cat /etc/vendor_info"))
    checks.append(Check("INFO", f"vendor_info: {vendor_info.strip()}"))
    dipc_text = adb.run("cat /mnt/vendor/nvdata/md_cmn/dipc_config")
    dipc_config = parse_dipc_config(dipc_text)
    if dipc_config is not None:
        checks.append(Check("INFO", f"dipc_config: {dipc_config}"))
    logread = clean(adb.run("logread | grep -E 'MIPC_NW_RADIO_STATE|NW_REGISTER_STATE|IMEI CHECK' | tail -n 30"))
    checks.append(check_radio_state(logread))
    return checks


def cmd_read(
    args: argparse.Namespace,
    *,
    session_factory: Callable[[], Session],
    usb_present_fn: Callable[[], bool] = usb_present,
    adb_factory: Callable[[], Adb] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    out_dir: Path,
) -> int:
    if not usb_present_fn():
        print("FM350 not found (0e8d:7126/7127).")
        print("Wait 10-60 s after power-on; use the adapter's power plug; try another USB port.")
        return 2
    session = session_factory()
    try:
        snapshot = _collect_snapshot(session)
        samples = _collect_samples(session, args.duration, args.interval, sleep, monotonic)
        adb_checks: list[Check] = []
        if args.adb:
            adb = adb_factory() if adb_factory else Adb()
            adb_checks = _run_adb_read_checks(adb, out_dir, redact=session.redact)
        checks = _diagnose(snapshot, samples, adb_checks)
        for check in checks:
            print(f"[{check.level}] {check.message}")
        _print_suggestions(checks)
        write_report(out_dir, session, "fm350_diag read", checks)
        return 1 if any(c.level in ("WARN", "FAIL") for c in checks) else 0
    finally:
        session.transport.close()


# --- volatile --------------------------------------------------------------------

DEFAULT_VOLATILE_DURATION_S = 30.0
_VOLATILE_SAMPLE_INTERVAL_S = 10.0
_COPS_SCAN_TIMEOUT_S = 180.0


def _wait_for_reenumeration(
    transport_factory: Callable[[], Transport],
    timeout_s: float,
    *,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> Transport | None:
    """Poll ``transport_factory`` every 3s until it opens or ``timeout_s`` elapses."""
    deadline = monotonic() + timeout_s
    while monotonic() < deadline:
        try:
            return transport_factory()
        except Exception:
            sleep(3.0)
    return None


def cmd_volatile(
    args: argparse.Namespace,
    *,
    session_factory: Callable[[], Session],
    transport_factory: Callable[[], Transport] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    out_dir: Path,
    confirm: Callable[[str], str] = input,
) -> int:
    print(_SAFETY_BANNER)
    if not args.yes and confirm("Type 'yes' to continue: ").strip() != "yes":
        print("aborted")
        return 1
    session = session_factory()
    try:
        snapshot = _collect_snapshot(session)
        before_samples = _collect_samples(session, args.duration, args.interval, sleep, monotonic)

        session.send("AT+CMEE=2")

        orig_cereg = _parse_int_tuple(snapshot["cereg"])
        orig_cereg_n = orig_cereg[0] if orig_cereg else 0
        try:
            session.send("AT+CEREG=3")
            reject_samples = _collect_samples(session, 60.0, _VOLATILE_SAMPLE_INTERVAL_S, sleep, monotonic)
        finally:
            session.send(f"AT+CEREG={orig_cereg_n}")

        checks = [check_cells(before_samples), check_registration(before_samples, snapshot.get("ceer"))]
        checks.append(Check("INFO", f"CEREG=3 reject-cause sampling: {len(reject_samples)} sample(s) collected"))

        if not args.skip_scan:
            scan_response = session.send("AT+COPS=?", timeout=_COPS_SCAN_TIMEOUT_S)
            operator_count = len(re.findall(r"\(\d+,", scan_response))
            checks.append(Check("INFO", f"AT+COPS=? found {operator_count} operator(s)"))

        if not args.skip_cfun:
            orig_cfun = _parse_int_tuple(snapshot["cfun"])
            orig_cfun_n = orig_cfun[0] if orig_cfun else None
            if orig_cfun_n not in (1, 4):
                # Only cycle what we can put back: AT+CFUN=1/4 are the allowlisted restores.
                checks.append(
                    Check("WARN", f"CFUN cycle skipped: original CFUN is {orig_cfun_n}, which this tool can't restore")
                )
            else:
                try:
                    session.send("AT+CFUN=4")
                    sleep(5.0)
                    session.send("AT+CFUN=1")
                    cfun_samples = _collect_samples(session, 60.0, _VOLATILE_SAMPLE_INTERVAL_S, sleep, monotonic)
                    checks.append(check_registration(cfun_samples))
                finally:
                    session.send(f"AT+CFUN={orig_cfun_n}")

        if args.reset:
            session.send("AT+CFUN=15")
            session.transport.close()
            new_transport = _wait_for_reenumeration(transport_factory, 120.0, sleep=sleep, monotonic=monotonic)
            if new_transport is None:
                checks.append(Check("WARN", "modem did not re-enumerate within 120s after AT+CFUN=15"))
            else:
                session.rebind(new_transport)
                session.send("AT")
                session.send("AT+CPIN?")
                checks.append(Check("OK", "modem re-enumerated and answered AT/AT+CPIN? after reset"))

        if args.usb_reset:
            if session.transport.usb_reset():
                session.transport.close()
                if transport_factory is not None:
                    session.rebind(transport_factory())
                checks.append(Check("OK", "issued a USB device reset"))

        for c in checks:
            print(f"[{c.level}] {c.message}")
        write_report(out_dir, session, "fm350_diag volatile", checks)
        return 1 if any(c.level in ("WARN", "FAIL") for c in checks) else 0
    finally:
        session.transport.close()


# --- experiment ------------------------------------------------------------------

_EXPERIMENT_NAMES = ("fmode", "anttuner", "rat-lte", "fcc-unlock-dell")
DEFAULT_EXPERIMENT_MEASURE_S = 90.0
_EXPERIMENT_SAMPLE_INTERVAL_S = 15.0
_EXPERIMENT_REENUM_TIMEOUT_S = 120.0


class RestoreFailed(Exception):
    """The experiment template couldn't restore or verify the original value."""


def _print_restore_failed(restore_path: Path) -> None:
    print("\n" + "!" * 70)
    print("RESTORE FAILED -- run these commands manually:")
    print(restore_path.read_text())
    print("!" * 70 + "\n")


def run_experiment(
    session: Session,
    out_dir: Path,
    *,
    name: str,
    read_cmd: str,
    parse_orig: Callable[[str], object],
    apply_cmds: Callable[[object], list[str]],
    restore_cmds: Callable[[object], list[str]],
    reenumerate_after_apply: bool,
    reenumerate_after_restore: bool,
    measure: Callable[[], list[Sample]],
    reenumerate: Callable[[], None],
    verify_cmd: str,
    parse_verify: Callable[[str], object],
) -> tuple[object, list[Sample], list[Sample]]:
    """Read the original value, write restore.txt, apply, measure, then
    ALWAYS restore (even on KeyboardInterrupt/an apply failure) and verify.

    Raises RestoreFailed (after printing a loud manual-recovery block) if
    the restore doesn't verify or itself fails; otherwise re-raises
    whatever exception interrupted apply/measure, after the restore.
    """
    orig_response = session.send(read_cmd)
    orig = parse_orig(orig_response)
    if orig is None:
        # Nothing changed yet; restoring a guessed default could itself be the damage.
        raise DiagError(f"{name}: could not parse the original value from {orig_response.strip()!r}; nothing changed")

    restore_path = out_dir / "restore.txt"
    restore_path.write_text(
        "\n".join(
            [
                f"# manual restore for `experiment {name}` (tools/fm350_diag.py)",
                f"# original {read_cmd} response: {orig_response.strip()!r}",
                *restore_cmds(orig),
            ]
        )
        + "\n"
    )
    print(f"restore instructions written to {restore_path} before changing anything")

    before_samples = measure()
    after_samples: list[Sample] = []
    try:
        for cmd in apply_cmds(orig):
            session.send(cmd)
        if reenumerate_after_apply:
            reenumerate()
        after_samples = measure()
    finally:
        try:
            for cmd in restore_cmds(orig):
                session.send(cmd)
            if reenumerate_after_restore:
                reenumerate()
            verify_value = parse_verify(session.send(verify_cmd))
            if verify_value != orig:
                raise RestoreFailed(f"{name}: expected {orig!r} after restore, read back {verify_value!r}")
        except BaseException as restore_exc:  # incl. a second Ctrl-C mid-restore: never claim "restored"
            _print_restore_failed(restore_path)
            if isinstance(restore_exc, RestoreFailed):
                raise
            raise RestoreFailed(f"{name}: restore step failed: {restore_exc}") from restore_exc

    return orig, before_samples, after_samples


def _run_fmode(session: Session, out_dir: Path, measure, reenumerate):
    def parse_orig(resp: str):
        t = _parse_int_tuple(resp)
        return (t[0], t[1]) if t and len(t) >= 2 else None

    def parse_verify(resp: str):
        t = _parse_int_tuple(resp)
        return (t[0], t[1]) if t and len(t) >= 2 else None

    return run_experiment(
        session,
        out_dir,
        name="fmode",
        read_cmd="AT+GTFMODE?",
        parse_orig=parse_orig,
        apply_cmds=lambda orig: ["AT+GTFMODE=0,0"],
        restore_cmds=lambda orig: [f"AT+GTFMODE={orig[0]},{orig[1]}"],
        reenumerate_after_apply=True,
        reenumerate_after_restore=True,
        measure=measure,
        reenumerate=reenumerate,
        verify_cmd="AT+GTFMODE?",
        parse_verify=parse_verify,
    )


def _run_anttuner(session: Session, out_dir: Path, measure, reenumerate):
    def parse_orig(resp: str):
        t = _parse_int_tuple(resp)
        return t[0] if t else None

    def parse_verify(resp: str):
        t = _parse_int_tuple(resp)
        return t[0] if t else None

    return run_experiment(
        session,
        out_dir,
        name="anttuner",
        read_cmd="AT+GTANTTUNINGEN?",
        parse_orig=parse_orig,
        apply_cmds=lambda orig: ["AT+GTANTTUNINGEN=0"],
        restore_cmds=lambda orig: [f"AT+GTANTTUNINGEN={orig}"],
        reenumerate_after_apply=True,
        reenumerate_after_restore=True,
        measure=measure,
        reenumerate=reenumerate,
        verify_cmd="AT+GTANTTUNINGEN?",
        parse_verify=parse_verify,
    )


def _run_rat_lte(session: Session, out_dir: Path, measure):
    def parse_orig(resp: str):
        erat = cellinfo.parse_erat(resp)
        return erat.rat_mode if erat else None

    def parse_verify(resp: str):
        erat = cellinfo.parse_erat(resp)
        return erat.rat_mode if erat else None

    return run_experiment(
        session,
        out_dir,
        name="rat-lte",
        read_cmd="AT+ERAT?",
        parse_orig=parse_orig,
        apply_cmds=lambda orig: ["AT+ERAT=3"],
        restore_cmds=lambda orig: [f"AT+ERAT={orig}"],
        reenumerate_after_apply=False,
        reenumerate_after_restore=False,
        measure=measure,
        reenumerate=lambda: None,
        verify_cmd="AT+ERAT?",
        parse_verify=parse_verify,
    )


# --- fcc-unlock-dell: the Dell challenge/response vendor unlock -----------------
#
# docs/dell-dw5931e-usb.md / docs/bench-log.md: AT+GTFCCLOCKGEN -> challenge,
# response = first 4 bytes of SHA-256(challenge_be32 || SHA-256("DW5931EFCCLOCK")[0:4]),
# AT+GTFCCLOCKVER=<decimal response>. Never AT+GTFCCLOCKMODE.

_FCC_SALT = hashlib.sha256(b"DW5931EFCCLOCK").digest()[:4]
assert _FCC_SALT == bytes.fromhex("4909b5a4")


def parse_fcc_challenge(response: str) -> int | None:
    match = re.search(r"\+GTFCCLOCKGEN:\s*(0[xX][0-9A-Fa-f]+|\d+)", response)
    if not match:
        return None
    token = match.group(1)
    return int(token, 16) if token.lower().startswith("0x") else int(token)


def fcc_unlock_response(challenge: int) -> int:
    digest = hashlib.sha256(challenge.to_bytes(4, "big") + _FCC_SALT).digest()[:4]
    return int.from_bytes(digest, "big")


def _experiment_fcc_unlock_dell(
    session: Session, out_dir: Path, *, force_oem: bool, even_if_unlocked: bool
) -> list[Check]:
    pkgver_resp = session.send("AT+GTPKGVER?")
    is_dell = "_5025." in pkgver_resp
    if not is_dell and not force_oem:
        return [Check("FAIL", "refusing: firmware is not the Dell _5025 OEM image (use --force-oem to override)")]

    fcc_resp = session.send("AT+GTFCCEFFSTATUS?")
    fcc = _parse_int_tuple(fcc_resp)
    if fcc is not None and len(fcc) >= 2 and fcc[1] == 1 and not even_if_unlocked:
        return [Check("OK", f"already unlocked (mode={fcc[0]}, status={fcc[1]}); nothing to do")]

    # Nothing persistent is set here that needs restoring: the vendor unlock
    # response is one-way, not a setting (see docs/dell-dw5931e-usb.md).
    (out_dir / "restore.txt").write_text(
        "# experiment fcc-unlock-dell has no restore step: it sends a one-way vendor\n"
        "# unlock response, it doesn't change any persistent setting.\n"
    )

    gen_resp = session.send("AT+GTFCCLOCKGEN")
    challenge = parse_fcc_challenge(gen_resp)
    if challenge is None:
        return [Check("FAIL", f"could not parse challenge from {gen_resp!r}")]
    response = fcc_unlock_response(challenge)
    session.send(f"AT+GTFCCLOCKVER={response}")
    new_fcc_resp = session.send("AT+GTFCCEFFSTATUS?")
    new_fcc = _parse_int_tuple(new_fcc_resp)
    if new_fcc is not None and len(new_fcc) >= 2 and new_fcc[1] == 1:
        return [Check("OK", f"FCC status after unlock attempt: {new_fcc}")]
    return [Check("WARN", f"FCC status after unlock attempt: {new_fcc_resp.strip()!r} (still locked?)")]


def _summarize_samples(samples: list[Sample]) -> str:
    if not samples:
        return "no samples"
    no_cell = sum(1 for s in samples if s.no_cells)
    registered = sum(1 for s in samples if at_mod.is_registered(s.cereg_stat) or at_mod.is_registered(s.c5greg_stat))
    return f"{len(samples)} sample(s), cells in {len(samples) - no_cell}/{len(samples)}, registered in {registered}/{len(samples)}"


def cmd_experiment(
    args: argparse.Namespace,
    *,
    session_factory: Callable[[], Session],
    transport_factory: Callable[[], Transport] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    out_dir: Path,
    confirm: Callable[[str], str] = input,
) -> int:
    if args.name not in _EXPERIMENT_NAMES:
        print(f"unknown experiment: {args.name!r} (choices: {', '.join(_EXPERIMENT_NAMES)})", file=sys.stderr)
        return 2
    print(_SAFETY_BANNER)
    if not args.accept_risk and confirm(f"Type '{args.name}' to continue: ").strip() != args.name:
        print("aborted")
        return 1

    session = session_factory()

    def reenumerate() -> None:
        session.send("AT+CFUN=15")
        session.transport.close()
        new_transport = _wait_for_reenumeration(
            transport_factory, _EXPERIMENT_REENUM_TIMEOUT_S, sleep=sleep, monotonic=monotonic
        )
        if new_transport is None:
            raise RuntimeError("modem did not re-enumerate within 120s")
        session.rebind(new_transport)
        session.send("AT")

    def measure() -> list[Sample]:
        return _collect_samples(session, args.measure, _EXPERIMENT_SAMPLE_INTERVAL_S, sleep, monotonic)

    before: list[Sample] = []
    after: list[Sample] = []
    try:
        if args.name == "fmode":
            orig, before, after = _run_fmode(session, out_dir, measure, reenumerate)
            checks = [Check("INFO", f"GTFMODE {orig} -> 0,0 -> restored {orig}")]
        elif args.name == "anttuner":
            orig, before, after = _run_anttuner(session, out_dir, measure, reenumerate)
            checks = [Check("INFO", f"GTANTTUNINGEN {orig} -> 0 -> restored {orig}")]
        elif args.name == "rat-lte":
            orig, before, after = _run_rat_lte(session, out_dir, measure)
            checks = [
                Check("INFO", f"ERAT {orig} -> 3 -> restored {orig} (ERAT persists across CFUN=15 on this firmware)")
            ]
        else:  # fcc-unlock-dell
            checks = _experiment_fcc_unlock_dell(
                session, out_dir, force_oem=args.force_oem, even_if_unlocked=args.even_if_unlocked
            )
    except RestoreFailed as exc:
        write_report(out_dir, session, f"fm350_diag experiment {args.name}", [Check("FAIL", str(exc))])
        return 3
    except DiagError as exc:
        print(f"experiment aborted before any change: {exc}")
        write_report(out_dir, session, f"fm350_diag experiment {args.name}", [Check("FAIL", str(exc))])
        return 2
    except KeyboardInterrupt:
        print("\ninterrupted; original value restored")
        write_report(
            out_dir,
            session,
            f"fm350_diag experiment {args.name}",
            [Check("WARN", "interrupted by user; original value restored")],
        )
        return 1
    except Exception as exc:
        print(f"experiment failed (original value restored): {exc}")
        write_report(
            out_dir,
            session,
            f"fm350_diag experiment {args.name}",
            [Check("WARN", f"failed: {exc}; original value restored")],
        )
        return 1
    finally:
        session.transport.close()

    if before or after:
        checks.append(Check("INFO", f"before: {_summarize_samples(before)}"))
        checks.append(Check("INFO", f"after: {_summarize_samples(after)}"))
    for c in checks:
        print(f"[{c.level}] {c.message}")
    write_report(out_dir, session, f"fm350_diag experiment {args.name}", checks)
    return 0


# --- backup ------------------------------------------------------------------


def _parse_mtd_partitions(proc_mtd: str) -> list[int]:
    return [int(m.group(1)) for m in re.finditer(r"^mtd(\d+):", proc_mtd, re.MULTILINE)]


def _write_sha256sums(dest: Path) -> None:
    lines = []
    for path in sorted(dest.iterdir()):
        if path.name == "SHA256SUMS" or not path.is_file():
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        lines.append(f"{digest}  {path.name}")
    (dest / "SHA256SUMS").write_text("\n".join(lines) + "\n")


def cmd_backup(args: argparse.Namespace, *, adb_factory: Callable[[], Adb] | None = None, out_dir: Path) -> int:
    adb = adb_factory() if adb_factory else Adb()
    if not adb.available():
        print("adb not found on PATH", file=sys.stderr)
        return 2
    dest = Path(args.dest) if args.dest else Path("backups") / f"fm350-{datetime.now().strftime('%Y%m%d-%H%M')}"
    dest.mkdir(parents=True, exist_ok=True)
    print("WARNING: this backup contains the IMEI and RF calibration of this exact unit.")
    print("Never share it, and never restore it onto another unit.")
    for name in _ADB_BACKUP_DIRS:
        data = adb.run(
            f"tar -C /mnt/vendor -cf - {name}", level=0, binary_output=True, timeout=_ADB_BACKUP_TIMEOUT_S
        )
        (dest / f"{name}.tar").write_bytes(data)
        if not data:
            print(f"warning: {name}.tar is empty (directory missing on this firmware?)", file=sys.stderr)
    proc_mtd = adb.run("cat /proc/mtd", level=0)
    (dest / "proc_mtd.txt").write_text(proc_mtd)
    nvdata_tar = dest / "nvdata.tar"
    if not nvdata_tar.is_file() or nvdata_tar.stat().st_size == 0:
        print("nvdata.tar is empty or missing; backup failed", file=sys.stderr)
        return 1
    if args.raw:
        for n in _parse_mtd_partitions(proc_mtd):
            data = adb.run(f"cat /dev/mtdblock{n}", level=0, binary_output=True, timeout=_ADB_BACKUP_TIMEOUT_S)
            (dest / f"mtdblock{n}.img").write_bytes(data)
    _write_sha256sums(dest)
    print(f"backup written to {dest}")
    return 0


# --- dipc ----------------------------------------------------------------------

# The stock Fibocom default (docs/dell-dw5931e-usb.md); dual_ipc_mode=3 makes
# USB unconditional. Byte-exact -- pushed and read back verbatim before
# anything on the module is touched.
DUAL_CONTENT = (
    "dual_ipc_mode:3\n"
    "ap_logging_interface:1\n"
    "md_logging_interface:1\n"
    "md_at_interface:1\n"
    "ap_pcie_port_config:3\n"
    "md_pcie_port_config:15\n"
)
assert len(DUAL_CONTENT.encode()) == 125

_DIPC_KEYS = (
    "dual_ipc_mode",
    "ap_logging_interface",
    "md_logging_interface",
    "md_at_interface",
    "ap_pcie_port_config",
    "md_pcie_port_config",
)
_CONFIRM_PHRASE = "CHANGE DIPC"


def parse_dipc_config(text: str) -> dict[str, int] | None:
    """Parse dipc_config's exact six ``key:value`` lines, in order. Returns
    None for anything else (extra/missing/reordered keys, non-integer
    values) -- this is a strict structural parser; mode validity is a
    separate check (dipc_mode_ok()).
    """
    lines = [line for line in text.strip().splitlines() if line.strip()]
    if len(lines) != len(_DIPC_KEYS):
        return None
    values: dict[str, int] = {}
    for line, expected_key in zip(lines, _DIPC_KEYS):
        key, sep, value = line.partition(":")
        if not sep or key != expected_key:
            return None
        try:
            values[key] = int(value)
        except ValueError:
            return None
    return values


def dipc_mode_ok(config: dict[str, int] | None) -> bool:
    """False for anything but dual_ipc_mode 1 or 3 -- any other value
    disables USB permanently on a USB-only setup (see DUAL_CONTENT's comment).
    """
    return config is not None and config["dual_ipc_mode"] in (1, 3)


def _ls_names(ls_output: str) -> set[str]:
    """File names from `ls -l` output (last column), for exact-name matching."""
    return {line.split()[-1] for line in ls_output.splitlines() if line.split()}


def _existing_orig(adb: Adb) -> tuple[str, str] | None:
    """(name, content) of the saved original dipc_config, if one exists.
    Prefers `.orig` (what this tool writes) over `.orig-dell` (our manual run).
    """
    names = _ls_names(adb.run("ls -l /mnt/vendor/nvdata/md_cmn/"))
    for name in _DIPC_ORIG_NAMES:
        if name in names:
            return name, adb.run(f"cat {_DIPC_DIR}/{name}")
    return None


def _verify_backup(backup_dir: Path) -> str | None:
    """None if the backup verifies, else a refusal message."""
    if not backup_dir.is_dir():
        return f"backup dir not found: {backup_dir}"
    nvdata = backup_dir / "nvdata.tar"
    if not nvdata.is_file() or nvdata.stat().st_size == 0:
        return f"{nvdata} missing or empty"
    sums_path = backup_dir / "SHA256SUMS"
    if not sums_path.is_file():
        return f"{sums_path} missing"
    for line in sums_path.read_text().splitlines():
        if not line.strip():
            continue
        digest, _, name = line.partition("  ")
        path = backup_dir / name
        if not path.is_file():
            return f"SHA256SUMS lists {name!r} but it's missing"
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            return f"SHA256SUMS mismatch for {name!r}"
    return None


def cmd_dipc_status(
    args: argparse.Namespace,
    *,
    adb_factory: Callable[[], Adb] | None = None,
    session_factory: Callable[[], Session] | None = None,
    out_dir: Path,
) -> int:
    adb = adb_factory() if adb_factory else Adb()
    checks: list[Check] = []
    if adb.available():
        devices_output = adb.devices()
        checks.append(check_adb_device_state(devices_output))
        if "\tdevice" in devices_output:
            text = adb.run("cat " + _DIPC_PATH)
            config = parse_dipc_config(text)
            if config is None:
                checks.append(Check("WARN", f"dipc_config: unparseable: {text!r}"))
            else:
                checks.append(Check("OK" if dipc_mode_ok(config) else "FAIL", f"dipc_config: {config}"))
    else:
        checks.append(Check("INFO", "adb not found; skipping file read"))
    if session_factory is not None:
        session = session_factory()
        try:
            checks.append(check_dipc(session.send("AT+GTDIPCMODE?")))
        finally:
            session.transport.close()
    for c in checks:
        print(f"[{c.level}] {c.message}")
    return 1 if any(c.level in ("WARN", "FAIL") for c in checks) else 0


def cmd_dipc_set_dual(
    args: argparse.Namespace,
    *,
    adb_factory: Callable[[], Adb] | None = None,
    out_dir: Path,
    stdin=sys.stdin,
    confirm_input: str | None = None,
) -> int:
    print(_SAFETY_BANNER)
    if not args.backup:
        print("refusing: --backup DIR is required (see `fm350_diag.py backup`)", file=sys.stderr)
        return 2
    backup_dir = Path(args.backup)
    error = _verify_backup(backup_dir)
    if error:
        print(f"refusing: {error}", file=sys.stderr)
        return 2

    adb = adb_factory() if adb_factory else Adb()
    if not adb.available():
        print("refusing: adb not found on PATH", file=sys.stderr)
        return 2

    current_text = adb.run("cat " + _DIPC_PATH)
    current = parse_dipc_config(current_text)
    if current is None or not dipc_mode_ok(current):
        print(f"refusing: current dipc_config doesn't parse or isn't mode 1/3: {current_text!r}", file=sys.stderr)
        return 2
    if current_text == DUAL_CONTENT:
        print("[OK] dipc_config is already the stock dual-mode target; nothing to do")
        return 0
    (out_dir / "dipc_config.before").write_text(current_text)

    if args.dry_run:
        print(
            "--dry-run: would back up dipc_config, cp to dipc_config.orig (if absent), push and install "
            "DUAL_CONTENT, then optionally reset. Nothing changed."
        )
        return 0

    if confirm_input is None:
        if not stdin.isatty():
            print("refusing: stdin is not a TTY, can't confirm interactively", file=sys.stderr)
            return 2
        typed = input(f"Type '{_CONFIRM_PHRASE}' to continue: ")
    else:
        typed = confirm_input
    if typed != _CONFIRM_PHRASE:
        print("aborted")
        return 1

    existing = _existing_orig(adb)
    if existing is None:
        adb.run(_DIPC_CP_ORIG, level=3)
        orig_name = "dipc_config.orig"
        if adb.run(f"cat {_DIPC_DIR}/{orig_name}") != current_text:
            print("refusing: couldn't save dipc_config.orig on the module; nothing changed", file=sys.stderr)
            return 3
    else:
        orig_name, orig_content = existing
        if not dipc_mode_ok(parse_dipc_config(orig_content)):
            print(f"refusing: existing {orig_name} isn't a valid mode-1/3 config: {orig_content!r}", file=sys.stderr)
            return 2

    with tempfile.NamedTemporaryFile("w", delete=False) as tmp:
        tmp.write(DUAL_CONTENT)
        tmp_path = Path(tmp.name)
    try:
        adb.push(tmp_path, _ADB_LEVEL3_PUSH_DEST)
    finally:
        tmp_path.unlink(missing_ok=True)

    pushed = adb.run(f"cat {_DIPC_DIR}/dipc_config.new", level=3)
    if pushed != DUAL_CONTENT:
        adb.run(_DIPC_RM_NEW, level=3)
        print("refusing: pushed dipc_config.new doesn't match DUAL_CONTENT byte for byte; nothing changed", file=sys.stderr)
        return 3

    adb.run(_DIPC_INSTALL_NEW, level=3)
    adb.run(_DIPC_RM_NEW, level=3)
    readback = adb.run("cat " + _DIPC_PATH, level=0)
    if readback != DUAL_CONTENT:
        adb.run(_dipc_restore_cmd(orig_name), level=3)
        verify = adb.run("cat " + _DIPC_PATH, level=0)
        if verify != current_text:
            print("RESTORE FAILED -- inspect the module manually over adb", file=sys.stderr)
            return 3
        print("write to dipc_config didn't take effect; restored the pre-change content", file=sys.stderr)
        return 3

    print("[OK] dipc_config is now the stock dual-mode target")
    print(
        "Reset the modem with AT+CFUN=15 to apply, then re-run `dipc status` after it re-enumerates "
        "to verify AT+GTDIPCMODE? == 3,1,1,1,3,15"
    )
    return 0


def cmd_dipc_revert(
    args: argparse.Namespace,
    *,
    adb_factory: Callable[[], Adb] | None = None,
    out_dir: Path,
    stdin=sys.stdin,
    confirm_input: str | None = None,
) -> int:
    print(_SAFETY_BANNER)
    adb = adb_factory() if adb_factory else Adb()
    if not adb.available():
        print("refusing: adb not found on PATH", file=sys.stderr)
        return 2

    existing = _existing_orig(adb)
    if existing is None:
        print("refusing: no dipc_config.orig[-dell] found on the module", file=sys.stderr)
        return 2
    orig_name, orig_content = existing
    orig_config = parse_dipc_config(orig_content)
    if orig_config is None or not dipc_mode_ok(orig_config):
        print(f"refusing: {orig_name} doesn't parse or isn't mode 1/3: {orig_content!r}", file=sys.stderr)
        return 2

    if args.dry_run:
        print(f"--dry-run: would restore dipc_config from {orig_name}. Nothing changed.")
        return 0

    if confirm_input is None:
        if not stdin.isatty():
            print("refusing: stdin is not a TTY, can't confirm interactively", file=sys.stderr)
            return 2
        typed = input(f"Type '{_CONFIRM_PHRASE}' to continue: ")
    else:
        typed = confirm_input
    if typed != _CONFIRM_PHRASE:
        print("aborted")
        return 1

    adb.run(_dipc_restore_cmd(orig_name), level=3)
    readback = adb.run("cat " + _DIPC_PATH)
    if readback != orig_content:
        print("RESTORE FAILED: readback does not match the .orig file -- inspect the module manually", file=sys.stderr)
        return 3
    print(f"[OK] dipc_config restored from {orig_name}")
    return 0


# --- plan ----------------------------------------------------------------------


def cmd_plan(args: argparse.Namespace) -> int:
    level = args.level
    print(f"Level {level} AT commands (nothing sent):\n")
    for cmd, cmd_level in sorted(AT_COMMANDS.items()):
        if cmd_level <= level:
            print(f"  [{cmd_level}] {cmd}")
    for pattern, cmd_level in AT_COMMAND_PATTERNS:
        if cmd_level <= level:
            print(f"  [{cmd_level}] {pattern.pattern}")
    print("\nADB level 0 (read-only):")
    for cmd in sorted(_ADB_LEVEL0_EXACT):
        print(f"  [0] adb exec-out {cmd}")
    for pattern in _ADB_LEVEL0_PATTERNS:
        print(f"  [0] adb exec-out {pattern.pattern}")
    if level >= 3:
        print("\nADB level 3 (DIPC write steps):")
        for cmd in sorted(_ADB_LEVEL3_EXACT):
            print(f"  [3] adb exec-out {cmd}")
        print(f"  [3] adb push <tmp with DUAL_CONTENT> {_ADB_LEVEL3_PUSH_DEST}")
    return 0


# --- CLI wiring ------------------------------------------------------------------


def _default_out_dir() -> Path:
    return Path(f"fm350-diag-{datetime.now().strftime('%Y%m%d-%H%M%S')}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fm350_diag.py",
        description="Staged, safety-gated diagnostics for a Fibocom FM350-GL / Dell DW5931e. "
        "See docs/diagnostics.md.",
    )
    parser.add_argument("--tty", help="AT port device node (Linux kernel driver), e.g. /dev/ttyUSB4")
    parser.add_argument("--iface", type=int, help="override the libusb AT interface number")
    parser.add_argument("--out", help="output directory (default: ./fm350-diag-YYYYmmdd-HHMMSS/)")
    parser.add_argument(
        "--no-redact", dest="redact", action="store_false", help="don't mask IMEI/IMSI/ICCID/TAC/cell IDs"
    )
    sub = parser.add_subparsers(dest="stage", required=True)

    p_read = sub.add_parser("read", help="level-0 read-only snapshot + sampling window + diagnosis")
    p_read.add_argument("--duration", type=float, default=DEFAULT_READ_DURATION_S)
    p_read.add_argument("--interval", type=float, default=DEFAULT_READ_INTERVAL_S)
    p_read.add_argument("--adb", action="store_true", help="also run read-only ADB probes")
    p_read.set_defaults(func=_run_read)

    p_volatile = sub.add_parser("volatile", help="level<=1 transient probes (reject cause, operator scan, CFUN cycle)")
    p_volatile.add_argument("--duration", type=float, default=DEFAULT_VOLATILE_DURATION_S)
    p_volatile.add_argument("--interval", type=float, default=DEFAULT_READ_INTERVAL_S)
    p_volatile.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    p_volatile.add_argument("--reset", action="store_true", help="also cycle CFUN=15 and wait for re-enumeration")
    p_volatile.add_argument(
        "--usb-reset", action="store_true", help="libusb only: reset the USB device (fixes adb offline)"
    )
    p_volatile.add_argument("--skip-scan", action="store_true", help="skip the AT+COPS=? operator scan")
    p_volatile.add_argument("--skip-cfun", action="store_true", help="skip the CFUN=4/1 cycle")
    p_volatile.set_defaults(func=_run_volatile)

    p_experiment = sub.add_parser("experiment", help="level<=2 temporary setting change, measured, then restored")
    p_experiment.add_argument("name", choices=_EXPERIMENT_NAMES)
    p_experiment.add_argument("--measure", type=float, default=DEFAULT_EXPERIMENT_MEASURE_S)
    p_experiment.add_argument("--accept-risk", action="store_true", help="skip typing the experiment name to confirm")
    p_experiment.add_argument(
        "--force-oem", action="store_true", help="fcc-unlock-dell: run even if firmware isn't the Dell _5025 image"
    )
    p_experiment.add_argument(
        "--even-if-unlocked", action="store_true", help="fcc-unlock-dell: run even if already unlocked"
    )
    p_experiment.set_defaults(func=_run_experiment)

    p_backup = sub.add_parser("backup", help="read-only ADB NV backup (level-0 ADB)")
    p_backup.add_argument("--dest", help="backup directory (default: backups/fm350-YYYYmmdd-HHMM/)")
    p_backup.add_argument("--raw", action="store_true", help="also dump every /dev/mtdblockN")
    p_backup.set_defaults(func=_run_backup)

    p_dipc = sub.add_parser("dipc", help="switch/revert the on-module DIPC config file (level 3, ADB)")
    dipc_sub = p_dipc.add_subparsers(dest="dipc_action", required=True)
    p_dipc_status = dipc_sub.add_parser("status", help="read the current DIPC mode (level 0)")
    p_dipc_status.set_defaults(func=_run_dipc_status)
    p_dipc_set_dual = dipc_sub.add_parser("set-dual", help="switch to stock dual mode 3,1,1,1,3,15")
    p_dipc_set_dual.add_argument("--backup", required=True, help="a verified `backup` output directory")
    p_dipc_set_dual.add_argument("--dry-run", action="store_true")
    p_dipc_set_dual.set_defaults(func=_run_dipc_set_dual)
    p_dipc_revert = dipc_sub.add_parser("revert", help="restore dipc_config from the saved .orig[-dell]")
    p_dipc_revert.add_argument("--dry-run", action="store_true")
    p_dipc_revert.set_defaults(func=_run_dipc_revert)

    p_plan = sub.add_parser("plan", help="print every command a level may send; touches nothing")
    p_plan.add_argument("level", type=int, choices=(0, 1, 2, 3))
    p_plan.set_defaults(func=_run_plan)

    return parser


def _run_read(args, *, transport_factory, session_factory, out_dir):
    return cmd_read(args, session_factory=lambda: session_factory(0), out_dir=out_dir)


def _run_volatile(args, *, transport_factory, session_factory, out_dir):
    return cmd_volatile(
        args, session_factory=lambda: session_factory(1), transport_factory=transport_factory, out_dir=out_dir
    )


def _run_experiment(args, *, transport_factory, session_factory, out_dir):
    return cmd_experiment(
        args, session_factory=lambda: session_factory(2), transport_factory=transport_factory, out_dir=out_dir
    )


def _run_backup(args, *, transport_factory, session_factory, out_dir):
    return cmd_backup(args, out_dir=out_dir)


def _run_dipc_status(args, *, transport_factory, session_factory, out_dir):
    return cmd_dipc_status(args, session_factory=lambda: session_factory(0), out_dir=out_dir)


def _run_dipc_set_dual(args, *, transport_factory, session_factory, out_dir):
    return cmd_dipc_set_dual(args, out_dir=out_dir)


def _run_dipc_revert(args, *, transport_factory, session_factory, out_dir):
    return cmd_dipc_revert(args, out_dir=out_dir)


def _run_plan(args, **_kwargs):
    return cmd_plan(args)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.stage == "plan":
        return cmd_plan(args)

    out_dir = Path(args.out) if args.out else _default_out_dir()
    out_dir.mkdir(parents=True, exist_ok=True)

    def transport_factory() -> Transport:
        return open_transport(tty=args.tty, iface=args.iface)

    def session_factory(level: int) -> Session:
        return Session(transport_factory(), level, out_dir, redact=args.redact)

    try:
        return args.func(args, transport_factory=transport_factory, session_factory=session_factory, out_dir=out_dir)
    except SafetyError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    except (DiagError, AdbError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
