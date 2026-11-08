"""Shared helpers for the Fibocom L830-EB tools (USB mode 0, macOS).

See docs/fibocom-l830-eb-investigation-2026-11-08.md. In USB mode 0 the module has three CDC-ACM ports:
two answer AT commands, one is a trace port that streams data unprompted. fm350_diag.TtyTransport
drains a port "until it is quiet" without a time limit, which never ends on the trace port, so every
drain is capped here.
"""

import glob
import os
import select
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
STATE = Path(__file__).resolve().parent / "state"  # git-ignored: session file, logs, pf token
sys.path.insert(0, str(REPO / "tools"))
sys.path.insert(0, str(REPO / "fm350mac" / "src"))

import fm350_diag  # noqa: E402
from fm350mac.at import AtTimeoutError  # noqa: E402, F401  (re-exported for the tools)

TtyTransport = fm350_diag.TtyTransport
_orig_drain = TtyTransport._drain


def _bounded_drain(self, timeout_s, deadline=None):
    return _orig_drain(self, timeout_s, deadline if deadline is not None else time.monotonic() + 1.0)


TtyTransport._drain = _bounded_drain


def log(name: str, text: str) -> None:
    STATE.mkdir(exist_ok=True)
    line = f"{time.strftime('%H:%M:%S')} {text}"
    print(line)
    with (STATE / name).open("a") as f:
        f.write(line + "\n")


def at_ports(exclude: str | None = None) -> list[str]:
    """/dev/cu.usbmodem* ports that answer AT, skipping the streaming trace port (and `exclude`)."""
    found = []
    for path in sorted(glob.glob("/dev/cu.usbmodem*")):
        if path == exclude:
            continue
        try:
            port = TtyTransport(path)
        except OSError:
            continue
        try:
            ready, _, _ = select.select([port.fd], [], [], 0.3)
            if ready and os.read(port.fd, 4096):
                continue  # unsolicited data: the trace port
            if "OK" in port.command("AT", timeout=3):
                found.append(path)
        except OSError:
            pass
        finally:
            port.close()
    return found


def open_at_port(exclude: str | None = None) -> "fm350_diag.TtyTransport":
    ports = at_ports(exclude)
    if not ports:
        sys.exit("no AT port found (is the L830-EB plugged in and in USB mode 0?)")
    return TtyTransport(ports[0])
