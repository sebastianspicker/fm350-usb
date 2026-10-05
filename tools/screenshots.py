#!/usr/bin/env python3
"""Regenerate the README/Pages screenshots from real command output.

Runs a handful of read-only fm350mac/openwrt commands and renders their
actual output to GitHub-dark SVG terminal windows (stdlib only; the SVG
rendering approach and look are adapted from the same author's
rclone-webdav-sync tools/screenshots.py, MIT).

Some shots need things that aren't always available:

  - fm350mac at/status/probe need the real FM350-GL modem on USB (0e8d:7127
    or :7126). Checked via `ioreg` (macOS only).
  - the install.sh dry-run shot needs Docker with the OpenWrt rootfs image
    already pulled (see openwrt/tests/docker-test.sh).
  - the qemu-failover summary needs qemu-system-aarch64 and takes about two
    minutes even with a warm cache (openwrt/tests/.cache).

When a gate isn't satisfied, that shot is skipped and the existing SVG (if
any) is left alone -- nothing here ever invents output.

Every live command run is read-only: no AT "set" commands, no PDP session,
no writes to the modem. AT/status/probe output is redacted (see redact())
before it's rendered.

Usage:
  tools/screenshots.py [--out DIR] [--only NAME...] [--skip-docker] [--skip-qemu] [--skip-live]
"""

from __future__ import annotations

import argparse
import os
import plistlib
import re
import shutil
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FM350MAC_DIR = os.path.join(REPO, "fm350mac")
FM350MAC_BIN = os.path.join(FM350MAC_DIR, ".venv", "bin", "fm350mac")
OPENWRT_DIR = os.path.join(REPO, "openwrt")
DOCKER_IMAGE = "openwrt/rootfs:armsr-armv8-openwrt-24.10"

MONO = "ui-monospace, SFMono-Regular, Menlo, Consolas, Liberation Mono, monospace"

# ---------------------------------------------------------------------------
# Running commands
# ---------------------------------------------------------------------------


def run(argv, *, cwd=None, env=None, timeout=None, merge_stderr=True):
    """Run argv, returning (returncode, output). Never raises for a
    non-zero exit; timeouts and missing binaries are the caller's problem.
    """
    proc = subprocess.run(
        argv,
        cwd=cwd,
        env=env,
        timeout=timeout,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT if merge_stderr else subprocess.DEVNULL,
    )
    return proc.returncode, proc.stdout.decode("utf-8", "replace")


def modem_present() -> bool:
    """True if the FM350-GL (USB 0e8d:7127 or the AT-only 0e8d:7126) is
    currently enumerated. macOS only (uses ioreg); returns False elsewhere
    or if ioreg fails for any reason -- this only gates which shots we try
    to (re)capture, so fail closed rather than raising.
    """
    if sys.platform != "darwin":
        return False
    try:
        out = subprocess.run(
            ["ioreg", "-p", "IOUSB", "-l", "-w0", "-a"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10,
        ).stdout
        data = plistlib.loads(out)
    except Exception:
        return False

    def walk(node) -> bool:
        if isinstance(node, dict):
            if node.get("idVendor") == 0x0E8D and node.get("idProduct") in (0x7127, 0x7126):
                return True
            return any(walk(child) for child in node.get("IORegistryEntryChildren", []) or [])
        if isinstance(node, list):
            return any(walk(item) for item in node)
        return False

    return walk(data)


def docker_openwrt_available() -> tuple[bool, str | None]:
    if not shutil.which("docker"):
        return False, "docker not found on PATH"
    rc, _ = run(["docker", "image", "inspect", DOCKER_IMAGE], merge_stderr=True)
    if rc != 0:
        return False, (
            f"docker image {DOCKER_IMAGE!r} isn't pulled locally "
            "(run openwrt/tests/docker-test.sh once, or `docker pull` it)"
        )
    return True, None


def qemu_available() -> tuple[bool, str | None]:
    if not shutil.which("qemu-system-aarch64"):
        return False, "qemu-system-aarch64 not found on PATH"
    return True, None


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------
#
# Applied to every live-modem shot (at/status/probe). Covers, per the
# fields these commands can surface:
#   - IMEI (AT+CGSN, 15 digits), IMSI (AT+CIMI, 15 digits starting with the
#     MCC), ICCID (AT+CCID/CRSM, 19-20 digits starting "89"), and a module
#     serial from AT+EGMR -- none of these are queried by the shots below
#     today, but the rules stay in place in case a future shot adds one.
#   - AT+GTCCINFO's serving-cell row (the one starting "1,"): its TAC (4
#     hex chars) and cell ID (9 hex chars). Neighbour rows ("2,...") already
#     report FFFF/00FFFFFFF placeholders for those fields, not real values.
#   - any USB/Ethernet MAC address that isn't the FM350-GL's well-known
#     fake permanent address (probe always reports this on real hardware).

_FAKE_MAC = "00:00:11:12:13:14"
_MAC_RE = re.compile(r"\b([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}\b")
# IMEI/IMSI/ICCID/serial: any run of 15+ digits, wherever it shows up.
_LONG_DIGITS_RE = re.compile(r"\d{15,}")
# +GTCCINFO serving-cell row: "1,<rat>,<mcc>,<x>,<TAC hex4>,<cellid hex9>,..."
_SERVING_CELL_RE = re.compile(r"^(1,\d+,\d+,\d+,)([0-9A-Fa-f]{4})(,)([0-9A-Fa-f]{9})(,)", re.MULTILINE)
# `fm350mac status`'s human-readable "TAC=... cell_id=..." fields. status
# has its own --redact for this (see shot_status()); this is a second,
# independent layer in case that's ever forgotten or regresses.
_TAC_EQ_RE = re.compile(r"\bTAC=[0-9A-Fa-fXx]+")
_CELLID_EQ_RE = re.compile(r"\bcell_id=[0-9A-Fa-fXx]+")
# AT+EGMR's module-serial response; format isn't pinned down here since
# nothing below queries it, so redact the whole value conservatively.
_EGMR_RE = re.compile(r"^(\+EGMR:\s*)(.+)$", re.MULTILINE)


def redact(text: str) -> str:
    text = _SERVING_CELL_RE.sub(lambda m: m.group(1) + "xxxx" + m.group(3) + "xxxxxxxxx" + m.group(5), text)
    text = _TAC_EQ_RE.sub("TAC=REDACTED", text)
    text = _CELLID_EQ_RE.sub("cell_id=REDACTED", text)
    text = _EGMR_RE.sub(lambda m: m.group(1) + "REDACTED", text)
    text = _MAC_RE.sub(lambda m: m.group(0) if m.group(0).lower() == _FAKE_MAC else "xx:xx:xx:xx:xx:xx", text)
    text = _LONG_DIGITS_RE.sub(lambda m: "x" * len(m.group(0)), text)
    return text


# ---------------------------------------------------------------------------
# Trimming (keep long, honest output readable in a screenshot)
# ---------------------------------------------------------------------------


def _trim_gtccinfo(lines: list[str], keep_neighbours: int = 3) -> list[str]:
    """+GTCCINFO?: keep the serving-cell row and the first few neighbour
    rows, eliding the rest with a count (real modems report a dozen-plus
    neighbours; all of them isn't useful in a screenshot).
    """
    out: list[str] = []
    seen = 0
    total_neighbours = sum(1 for line in lines if line.startswith("2,"))
    for line in lines:
        if line.startswith("2,"):
            seen += 1
            if seen <= keep_neighbours:
                out.append(line)
            elif seen == keep_neighbours + 1:
                out.append(f"… ({total_neighbours - keep_neighbours} more neighbour cells elided)")
        else:
            out.append(line)
    return out


def _trim_gtsenrdtemp(lines: list[str], keep_head: int = 4, keep_tail: int = 1) -> list[str]:
    """+GTSENRDTEMP=0: dozens of sensor readings; keep a few from each end."""
    if len(lines) <= keep_head + keep_tail + 1:
        return lines
    elided = len(lines) - keep_head - keep_tail
    return lines[:keep_head] + [f"… ({elided} more sensor readings elided)"] + lines[-keep_tail:]


def trim_status(text: str) -> str:
    """Trim the two long, repetitive blocks in `fm350mac status` output."""
    lines = text.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        if line.startswith("+GTCCINFO:"):
            i += 1
            block = []
            while i < len(lines) and lines[i].strip() and not lines[i].startswith((">>>", "OK")):
                block.append(lines[i])
                i += 1
            out.extend(_trim_gtccinfo(block))
            continue
        if line.startswith("+GTSENRDTEMP:"):
            block = [line]
            out.pop()  # re-added via the block below
            i += 1
            while i < len(lines) and lines[i].startswith("+GTSENRDTEMP:"):
                block.append(lines[i])
                i += 1
            out.extend(_trim_gtsenrdtemp(block))
            continue
        i += 1
    return "\n".join(out)


def trim_uci_batch(text: str, keep_head: int = 6, keep_tail: int = 2) -> str:
    """install.sh --dry-run prints a full `uci batch` per config file; the
    mwan3 one is long. Trim any run of boring uci-command lines
    (set/add_list/delete/del_list/reorder) longer than keep_head+keep_tail.
    """
    lines = text.split("\n")
    is_uci_line = lambda line: line.strip().split(" ", 1)[0] in (  # noqa: E731
        "set", "add_list", "delete", "del_list", "reorder",
    )
    out: list[str] = []
    i = 0
    while i < len(lines):
        if is_uci_line(lines[i]):
            j = i
            while j < len(lines) and is_uci_line(lines[j]):
                j += 1
            run_len = j - i
            if run_len > keep_head + keep_tail:
                out.extend(lines[i:i + keep_head])
                out.append(f"… ({run_len - keep_head - keep_tail} more uci commands elided)")
                out.extend(lines[j - keep_tail:j])
            else:
                out.extend(lines[i:j])
            i = j
        else:
            out.append(lines[i])
            i += 1
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Shots
# ---------------------------------------------------------------------------


class Skip(Exception):
    """Raised by a shot builder when its gate isn't satisfied."""


def shot_help():
    display = "fm350mac --help"
    rc, out = run([FM350MAC_BIN, "--help"], timeout=15)
    if rc != 0:
        raise Skip("fm350mac --help exited non-zero")
    return display, "$ %s\n%s" % (display, out.rstrip("\n"))


def shot_identity():
    display = "fm350mac at 'AT+GTPKGVER?' 'AT+GTDIPCMODE?' 'AT+GTFCCEFFSTATUS?' 'AT+CPIN?'"
    if not modem_present():
        raise Skip("modem (USB 0e8d:7127) not present")
    rc, out = run(
        [FM350MAC_BIN, "at", "AT+GTPKGVER?", "AT+GTDIPCMODE?", "AT+GTFCCEFFSTATUS?", "AT+CPIN?"],
        timeout=30,
    )
    if rc != 0:
        raise Skip("fm350mac at exited non-zero")
    return display, "$ %s\n%s" % (display, redact(out.rstrip("\n")))


def shot_status():
    # fm350mac status has its own --redact (masks TAC/cell_id, and IMSI/
    # ICCID/IMEI if it ever prints them); redact()/trim_status() still run
    # on top as a second, independent layer in case that ever regresses or
    # a future status format adds something this script doesn't know about.
    display = "fm350mac status --redact"
    if not modem_present():
        raise Skip("modem (USB 0e8d:7127) not present")
    rc, out = run([FM350MAC_BIN, "status", "--redact"], timeout=30)
    if rc != 0:
        raise Skip("fm350mac status --redact exited non-zero")
    text = trim_status(redact(out.rstrip("\n")))
    return display, "$ %s\n%s" % (display, text)


def shot_doctor():
    display = "fm350mac doctor"
    if not modem_present():
        raise Skip("modem (USB 0e8d:7127) not present")
    rc, out = run([FM350MAC_BIN, "doctor"], timeout=60)
    if rc not in (0, 1):  # 1 = at least one WARN, still a valid screenshot
        raise Skip("fm350mac doctor exited with %d" % rc)
    return display, "$ %s\n%s" % (display, redact(out.rstrip("\n")))


def shot_probe():
    display = "fm350mac probe"
    if not modem_present():
        raise Skip("modem (USB 0e8d:7127) not present")
    rc, out = run([FM350MAC_BIN, "probe"], timeout=30)
    if rc != 0:
        raise Skip("fm350mac probe exited non-zero")
    return display, "$ %s\n%s" % (display, redact(out.rstrip("\n")))


def shot_install_dry_run():
    display = "openwrt/install.sh --apn internet.example --dry-run"
    ok, reason = docker_openwrt_available()
    if not ok:
        raise Skip(reason)
    rc, out = run(
        [
            "docker", "run", "--rm",
            "-v", f"{OPENWRT_DIR}:/root/openwrt:ro",
            "-w", "/root/openwrt",
            DOCKER_IMAGE,
            "sh", "-c", "./install.sh --apn internet.example --dry-run",
        ],
        merge_stderr=False,  # drop docker's own "platform mismatch" stderr warning
        timeout=60,
    )
    if rc != 0:
        raise Skip("install.sh --dry-run exited non-zero under docker")
    text = trim_uci_batch(out.rstrip("\n"))
    return display, "$ %s\n%s" % (display, text)


_QEMU_SUMMARY_ALLOW_RE = re.compile(
    r"^qemu-failover-test\.sh: ("
    r"timing bounds:|=== assertion|measured |"
    r"(baseline|post-failover|post-failback) egress:|"
    r"both (wan and wwan down|uplinks restored)|"
    r"router-originated ping|observed:|"
    r"PASS$|FAIL, )"
)


def shot_qemu_summary():
    display = "openwrt/tests/qemu-failover-test.sh"
    ok, reason = qemu_available()
    if not ok:
        raise Skip(reason)
    rc, out = run(
        [os.path.join(OPENWRT_DIR, "tests", "qemu-failover-test.sh")],
        cwd=os.path.join(OPENWRT_DIR, "tests"),
        timeout=240,
    )
    lines = [line for line in out.split("\n") if _QEMU_SUMMARY_ALLOW_RE.match(line)]
    if not lines:
        raise Skip("qemu-failover-test.sh produced no recognizable summary lines")
    lines.insert(1, "… (VM boot, package install, and provisioning elided)")
    text = "$ %s\n%s" % (display, "\n".join(lines))
    if rc != 0:
        text += "\n(exit code %d)" % rc
    return display, text


SHOTS = [
    ("help", shot_help),
    ("identity", shot_identity),
    ("status", shot_status),
    ("doctor", shot_doctor),
    ("probe", shot_probe),
    ("install-dry-run", shot_install_dry_run),
    ("qemu-failover-summary", shot_qemu_summary),
]

# ---------------------------------------------------------------------------
# SVG rendering (GitHub-dark terminal window; same look as the reference
# rclone-webdav-sync tools/screenshots.py, ported here since this repo has
# no other dependency on it).
# ---------------------------------------------------------------------------

FONT_SIZE = 14.0
CHAR_W = FONT_SIZE * 0.62
LINE_H = 21.0
PAD_X = 22.0
PAD_TOP = 16.0
PAD_BOTTOM = 22.0
TITLE_H = 36.0
MIN_COLS = 64

BG = "#0d1117"
BAR = "#161b22"
BORDER = "#30363d"
FG = "#c9d1d9"
DIM = "#8b949e"
GREEN = "#3fb950"
RED = "#f85149"
AMBER = "#d29922"
BRIGHT = "#f0f6fc"


def esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def line_colors(line: str):
    """Split a line into (prefix, prefix_color, rest, rest_color, bold)."""
    if line.startswith("$ "):
        return "$ ", GREEN, line[2:], BRIGHT, True
    for word, color in (("FAIL", RED), ("WARNING", AMBER), ("PASS", GREEN), ("OK", GREEN)):
        if line.startswith(word + " ") or line == word or line.startswith(word + ":"):
            return line[: len(word)], color, line[len(word):], FG, False
    if line.startswith("…"):
        return "", DIM, line, DIM, False
    if line.startswith("==="):
        return "", FG, line, BRIGHT, True
    return "", FG, line, FG, False


def render_svg(title: str, text: str, path: str) -> None:
    lines = text.split("\n")
    cols = max([len(line) for line in lines] + [MIN_COLS])
    width = PAD_X * 2 + cols * CHAR_W
    height = TITLE_H + PAD_TOP + len(lines) * LINE_H + PAD_BOTTOM

    parts = [
        '<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" '
        'viewBox="0 0 %d %d" role="img" aria-label="%s">'
        % (round(width), round(height), round(width), round(height), esc(title)),
        "<title>%s</title>" % esc(title),
        '<defs><clipPath id="win"><rect x="0.5" y="0.5" width="%d" height="%d" rx="10"/>'
        "</clipPath></defs>" % (round(width) - 1, round(height) - 1),
        '<g clip-path="url(#win)">',
        '<rect x="0.5" y="0.5" width="%d" height="%d" rx="10" fill="%s" stroke="%s"/>'
        % (round(width) - 1, round(height) - 1, BG, BORDER),
        '<rect x="0.5" y="0.5" width="%d" height="%d" fill="%s"/>' % (round(width) - 1, TITLE_H, BAR),
        '<line x1="0.5" y1="%.1f" x2="%.1f" y2="%.1f" stroke="%s"/>'
        % (TITLE_H + 0.5, round(width) - 0.5, TITLE_H + 0.5, BORDER),
    ]
    for i, color in enumerate(("#ff5f56", "#ffbd2e", "#27c93f")):
        parts.append('<circle cx="%.1f" cy="18.5" r="6" fill="%s"/>' % (20 + i * 20, color))
    parts.append(
        '<text x="%.1f" y="23" text-anchor="middle" font-family="%s" font-size="12.5" '
        'fill="%s">%s</text>' % (round(width) / 2, MONO, DIM, esc(title))
    )

    font = 'font-family="%s" font-size="%.1f"' % (MONO, FONT_SIZE)
    y = TITLE_H + PAD_TOP + FONT_SIZE
    for line in lines:
        prefix, pcolor, rest, rcolor, bold = line_colors(line)
        weight = ' font-weight="600"' if bold else ""
        x = PAD_X
        if prefix:
            parts.append(
                '<text x="%.1f" y="%.1f" %s fill="%s"%s xml:space="preserve">%s</text>'
                % (x, y, font, pcolor, weight, esc(prefix))
            )
            x += len(prefix) * CHAR_W
        if rest:
            parts.append(
                '<text x="%.1f" y="%.1f" %s fill="%s"%s xml:space="preserve">%s</text>'
                % (x, y, font, rcolor, weight, esc(rest))
            )
        y += LINE_H

    parts.append("</g></svg>")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(parts) + "\n")


# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").strip().splitlines()[0])
    parser.add_argument("--out", default=os.path.join(REPO, "docs", "assets", "screenshots"))
    parser.add_argument("--only", nargs="*", default=None, help="render only these shot names")
    parser.add_argument("--skip-docker", action="store_true", help="skip the install.sh dry-run shot")
    parser.add_argument("--skip-qemu", action="store_true", help="skip the qemu-failover summary shot (~2 min)")
    parser.add_argument("--skip-live", action="store_true", help="skip shots that need the real modem")
    args = parser.parse_args()

    if not os.access(FM350MAC_BIN, os.X_OK):
        sys.exit("error: %s is not executable (run `uv sync` in fm350mac/?)" % FM350MAC_BIN)

    os.makedirs(args.out, exist_ok=True)
    selected = set(args.only) if args.only else {name for name, _ in SHOTS}

    for name, builder in SHOTS:
        if name not in selected:
            continue
        if args.skip_docker and name == "install-dry-run":
            print("skipping %s.svg: --skip-docker" % name)
            continue
        if args.skip_qemu and name == "qemu-failover-summary":
            print("skipping %s.svg: --skip-qemu" % name)
            continue
        if args.skip_live and name in ("identity", "status", "doctor", "probe"):
            print("skipping %s.svg: --skip-live" % name)
            continue

        out_path = os.path.join(args.out, name + ".svg")
        try:
            title, text = builder()
        except Skip as exc:
            existing = " (keeping existing %s.svg)" % name if os.path.exists(out_path) else " (no existing %s.svg)" % name
            print("skipping %s.svg: %s%s" % (name, exc, existing))
            continue
        except (subprocess.TimeoutExpired, OSError) as exc:
            existing = " (keeping existing %s.svg)" % name if os.path.exists(out_path) else " (no existing %s.svg)" % name
            print("skipping %s.svg: %s%s" % (name, exc, existing))
            continue

        render_svg(title, text, out_path)
        print("wrote %s.svg" % name)

    return 0


if __name__ == "__main__":
    sys.exit(main())
