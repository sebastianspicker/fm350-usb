#!/usr/bin/env python3
"""iperf3 throughput/stability bench for the Fibocom L830-EB over macOS NCM (en7), no sudo.

Prerequisites: `l830_ncm.py up` and `sudo l830_net.sh up SERVER`.

  l830_bench.py --bytes 5M [--server IP] [--parallel 4] [--directions down,up|down|up] [--budget MB --prior-mb MB]
  l830_bench.py selftest

Per direction: iperf3 -n BYTES (-R for the download) -J with a wall-clock limit. SIM use = en7 byte-counter
delta, appended to the shared ledger tools/bench-results/.sim-ledger ("<epoch> <rx> <tx>", as
tools/bench-throughput.sh). Refuses when the route to the server is not via the session interface, when
--budget would be exceeded (ledger + --prior-mb + worst case), or above 50 MB per direction without --yes.
Alongside: AT telemetry (AT+XCESQ?, AT+CGACT? every 2 s) on the AT port the session does not use, the
passive USB recorder usb_transitions.sh, and the module's exception counter (AT+XLOG=7) before and after.
Single TCP streams are loss-limited on lossy mobile paths; --parallel 4 measures what the link carries.
Results: tools/bench-results/l830-<timestamp>/ (iperf3 JSON, telemetry.log, transitions.log, SUMMARY.md).
"""

import argparse
import json
import re
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from l830_common import REPO, STATE, AtTimeoutError, TtyTransport, at_ports

HERE = Path(__file__).resolve().parent
RESULTS = REPO / "tools" / "bench-results"
LEDGER = RESULTS / ".sim-ledger"
SESSION = STATE / "l830_ncm_session.json"
CONFIRM_BYTES = 50_000_000


def parse_bytes(s: str) -> int:
    m = re.fullmatch(r"(\d+)([KMG]?)", s.upper())
    if not m:
        raise argparse.ArgumentTypeError(f"bad size {s!r} (e.g. 5M, 250M)")
    return int(m.group(1)) * {"": 1, "K": 10**3, "M": 10**6, "G": 10**9}[m.group(2)]


def ledger_used(path: Path = LEDGER) -> int:
    if not path.exists():
        return 0
    rows = (row.split() for row in path.read_text().splitlines())
    return sum(int(f[1]) + int(f[2]) for f in rows if len(f) == 3)


def iperf_summary(j: dict, download: bool) -> dict:
    end = j.get("end", {})
    recv, sent = end.get("sum_received", {}), end.get("sum_sent", {})
    side = recv if download else sent
    # an upload the server cut short has an empty sum_received: fall back to the sender side, then bytes/s
    bps = recv.get("bits_per_second") or side.get("bits_per_second") or (
        side.get("bytes", 0) * 8 / side["seconds"] if side.get("seconds") else 0)
    return {"bytes": side.get("bytes", 0), "seconds": round(side.get("seconds", 0), 2), "mbit_s": round(bps / 1e6, 1),
            "retransmits": sent.get("retransmits"), "error": j.get("error")}


def selftest() -> None:
    assert parse_bytes("5M") == 5_000_000 and parse_bytes("250M") == 250_000_000
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "ledger"
        p.write_text("1 10 20\n2 5 5\nbroken line\n")
        assert ledger_used(p) == 40
    j = {"end": {"sum_received": {"bytes": 5_000_000, "seconds": 2.0, "bits_per_second": 2e7},
                 "sum_sent": {"bytes": 5_100_000, "seconds": 2.0, "retransmits": 3}}}
    assert iperf_summary(j, True) == {"bytes": 5_000_000, "seconds": 2.0, "mbit_s": 20.0, "retransmits": 3,
                                      "error": None}
    cut_upload = {"end": {"sum_received": {}, "sum_sent": {"bytes": 258_867_200, "seconds": 237.0, "retransmits": 8903}}}
    assert iperf_summary(cut_upload, False)["mbit_s"] == 8.7  # 2026-11-08 17:56
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "ledger"
        assert record_usage("en7", 100, 50, p, lambda iface: (400, 150)) == 400 and ledger_used(p) == 400
        # counters restarted after a USB drop: a lower bound, never a negative line
        assert record_usage("en7", 1000, 1000, p, lambda iface: (30, 20)) == 50 and ledger_used(p) == 450

        def no_counters(iface):
            sys.exit("no byte counters")
        assert record_usage("en7", 0, 0, p, no_counters) == 0 and ledger_used(p) == 450

        def interrupted(*args, **kwargs):
            raise KeyboardInterrupt
        readings = iter([(0, 0), (7_000_000, 1_000)])
        a = argparse.Namespace(bytes=5_000_000, server="192.0.2.1", port=5201, parallel=1)
        try:
            run_iperf(a, "down", "en7", Path(d), run=interrupted, counters=lambda iface: next(readings), ledger=p)
        except KeyboardInterrupt:
            pass
        assert ledger_used(p) == 450 + 7_001_000  # Ctrl-C mid-run: the use is still in the ledger
    print("selftest ok")


def en_counters(iface: str) -> tuple[int, int]:
    out = subprocess.run(["netstat", "-I", iface, "-b"], capture_output=True, text=True, check=True).stdout
    for line in out.splitlines():
        f = line.split()  # Name Mtu <Link#n> Address Ipkts Ierrs Ibytes Opkts Oerrs Obytes Coll
        if len(f) >= 10 and f[0] == iface and f[2].startswith("<Link#"):
            return int(f[6]), int(f[9])
    sys.exit(f"no byte counters for {iface} in netstat output:\n{out}")


class Telemetry(threading.Thread):
    """AT+XCESQ? and AT+CGACT? every 2 s; a lost port means the module dropped off USB."""

    def __init__(self, path: str, out: Path):
        super().__init__(daemon=True)
        self.path, self.out, self.stop = path, out, threading.Event()

    def run(self) -> None:
        with self.out.open("w") as f:
            try:
                port = TtyTransport(self.path)
            except OSError as exc:
                f.write(f"{time.time():.1f} open failed: {exc}\n")
                return
            try:
                while not self.stop.is_set():
                    try:
                        for cmd in ("AT+XCESQ?", "AT+CGACT?"):
                            ans = " | ".join(x for x in port.command(cmd, timeout=5).splitlines() if x.startswith("+"))
                            f.write(f"{time.time():.1f} {cmd} {ans}\n")
                    except AtTimeoutError as exc:  # an OSError too, but a slow answer is not a lost port
                        f.write(f"{time.time():.1f} AT timeout: {exc}\n")
                    except OSError as exc:
                        f.write(f"{time.time():.1f} AT PORT LOST: {exc}\n")
                        return
                    f.flush()
                    self.stop.wait(2)
            finally:
                port.close()


def xlog(path: str) -> str:
    try:
        port = TtyTransport(path)
        try:
            return " ".join(port.command("AT+XLOG=7", timeout=5).split())  # display only, per the module's =? help
        finally:
            port.close()
    except OSError as exc:
        return f"unavailable: {exc}"


def record_usage(iface: str, rx0: int, tx0: int, ledger: Path = LEDGER, counters=en_counters) -> int:
    """Append this run's SIM use (iface byte-counter delta) to the ledger; returns the bytes.

    Called from a finally, so an interrupted run is counted too."""
    try:
        rx1, tx1 = counters(iface)
    except (SystemExit, subprocess.CalledProcessError) as exc:
        print(f"WARNING: no byte counters for {iface} after the run ({exc}): its SIM use is NOT in the ledger",
              file=sys.stderr)
        return 0
    if rx1 < rx0 or tx1 < tx0:
        print(f"WARNING: the {iface} counters restarted (module dropped off USB?): the ledger gets a lower bound",
              file=sys.stderr)
    # a re-attached interface counts from 0 again: what it shows now is the least that was used
    rx, tx = (rx1 - rx0 if rx1 >= rx0 else rx1), (tx1 - tx0 if tx1 >= tx0 else tx1)
    with ledger.open("a") as f:
        f.write(f"{int(time.time())} {rx} {tx}\n")
    return rx + tx


def run_iperf(a, direction: str, iface: str, out: Path, run=subprocess.run, counters=en_counters,
              ledger: Path = LEDGER) -> dict:
    rx0, tx0 = counters(iface)
    limit = a.bytes * 8 / 1e6 + 120  # seconds: 1 Mbit/s floor plus slack
    cmd = ["iperf3", "-c", a.server, "-p", str(a.port), "-n", str(a.bytes), "-J", "--connect-timeout", "5000"]
    if direction == "down":
        cmd.append("-R")
    if a.parallel > 1:
        cmd += ["-P", str(a.parallel)]  # -n stays the total across all streams
    t0 = time.time()
    p = None
    used = 0
    try:
        try:
            p = run(cmd, capture_output=True, text=True, timeout=limit)
            j = json.loads(p.stdout or "{}")
        except subprocess.TimeoutExpired:
            j = {"error": f"wall-clock limit {limit:.0f} s"}
        except json.JSONDecodeError:
            j = {"error": f"no JSON from iperf3: {(p.stderr if p else '')[-300:]}"}
        (out / f"iperf3_{direction}.json").write_text(json.dumps(j, indent=1))
    finally:  # also on Ctrl-C: the data is spent either way
        used = record_usage(iface, rx0, tx0, ledger, counters)
    return {"direction": direction, "start": time.strftime("%H:%M:%S", time.localtime(t0)),
            "sim_mb": round(used / 1e6, 1), **iperf_summary(j, direction == "down")}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("action", nargs="?", default="run", choices=["run", "selftest"])
    ap.add_argument("--bytes", type=parse_bytes, default=parse_bytes("5M"), help="per direction, e.g. 5M, 250M")
    ap.add_argument("--server", default="185.150.27.2", help="iperf3 server IPv4 (must be routed via en7)")
    ap.add_argument("--port", type=int, default=5201)
    ap.add_argument("--parallel", type=int, default=1, choices=range(1, 9), metavar="N", help="iperf3 -P")
    ap.add_argument("--directions", default="down,up", choices=["down,up", "down", "up"])
    ap.add_argument("--budget", type=int, metavar="MB", help="refuse when ledger + --prior-mb + worst case exceeds it")
    ap.add_argument("--prior-mb", type=int, default=0, help="SIM use not in the ledger, counted against --budget")
    ap.add_argument("--yes", action="store_true", help=f"required above {CONFIRM_BYTES // 10**6} MB per direction")
    a = ap.parse_args()
    if a.action == "selftest":
        return selftest()
    if not re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", a.server):
        sys.exit("--server must be one IPv4 address")
    if not SESSION.exists():
        sys.exit(f"no {SESSION}; run l830_ncm.py up and sudo l830_net.sh up first")
    s = json.loads(SESSION.read_text())
    iface = s["iface"]
    route = subprocess.run(["route", "-n", "get", a.server], capture_output=True, text=True).stdout
    if f"interface: {iface}" not in route:
        sys.exit(f"route to {a.server} is not via {iface}; refusing (run sudo l830_net.sh up {a.server})")
    directions = a.directions.split(",")
    worst = int(len(directions) * a.bytes * 1.1)  # 10 % protocol overhead per direction
    used = ledger_used() + a.prior_mb * 10**6
    print(f"SIM use so far {used / 1e6:.0f} MB (ledger + {a.prior_mb} MB prior); this run worst case "
          f"{worst / 1e6:.0f} MB; budget {a.budget if a.budget else 'none'} MB")
    if a.budget and used + worst > a.budget * 10**6:
        sys.exit("would exceed --budget; refusing")
    if a.bytes > CONFIRM_BYTES and not a.yes:
        sys.exit(f"{a.bytes / 1e6:.0f} MB per direction needs --yes")

    out = RESULTS / f"l830-{time.strftime('%Y%m%d-%H%M%S')}"
    out.mkdir(parents=True)
    others = at_ports(exclude=s["at_port"])
    if not others:
        sys.exit("no second AT port for telemetry")
    tport = others[0]
    xlog_before = xlog(tport)
    rec_secs = int(len(directions) * (a.bytes * 8 / 5e6 + 90))  # both runs at >= 5 Mbit/s
    usb = subprocess.Popen(["bash", str(HERE / "usb_transitions.sh"), str(rec_secs), str(out)])
    tel = Telemetry(tport, out / "telemetry.log")
    tel.start()
    results = []
    try:
        for direction in directions:
            r = run_iperf(a, direction, iface, out)
            results.append(r)
            print(r)
            if r["error"]:
                print("run failed; stopping before the next direction")
                break
    finally:
        tel.stop.set()
        tel.join(timeout=10)
        usb.terminate()
        xlog_after = xlog(tport)

    trans = out / "transitions.log"
    events = trans.read_text().splitlines()[1:] if trans.exists() else []
    lost = [x for x in (out / "telemetry.log").read_text().splitlines() if "LOST" in x]
    lines = [f"# L830-EB NCM bench {out.name}", "",
             f"server {a.server}:{a.port}, {a.bytes / 1e6:.0f} MB per direction, {a.parallel} stream(s), "
             f"iface {iface} ({s['ip']}), telemetry port {tport}", "",
             "| Direction | Start | MB | s | Mbit/s | Retransmits | SIM MB | Error |", "|---|---|---|---|---|---|---|---|"]
    lines += [f"| {r['direction']} | {r['start']} | {r['bytes'] / 1e6:.1f} | {r['seconds']} | {r['mbit_s']} | "
              f"{r['retransmits']} | {r['sim_mb']} | {r['error'] or ''} |" for r in results]
    lines += ["", f"- USB events during the run: {len(events)} {events if events else ''}",
              f"- AT port lost: {lost or 'no'}", f"- XLOG before: {xlog_before}", f"- XLOG after:  {xlog_after}",
              f"- SIM use now: {(ledger_used() + a.prior_mb * 10**6) / 1e6:.0f} MB (ledger + {a.prior_mb} MB prior)"]
    (out / "SUMMARY.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
