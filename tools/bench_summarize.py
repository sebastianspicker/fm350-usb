#!/usr/bin/env python3
"""Turn the iperf3 --json results dropped by bench-throughput.sh into a
Markdown summary table. Stdlib only, no third-party dependencies.

Usage: bench_summarize.py <results-dir>

Expects files named "<mode>-<direction>-<protocol>.json" (e.g.
"sync-download-tcp.json") plus optional siblings:
  "<...>.cpu"   one "<epoch-seconds> <cpu-seconds>" sample per line
                (cumulative `ps -o time=` of the fm350mac process, converted);
                CPU % is the cputime delta over the wall-clock delta. A
                bare "<percent>" per line (older `ps -o %cpu=` format) is
                averaged instead.
  "<...>.utun"  "<rx_bytes> <tx_bytes>" actually moved over the utun
                interface during the test (netstat -ibn delta).
Prints a Markdown table to stdout, with bytes transferred per test and a
total; anything that doesn't match the naming convention, or can't be
parsed, is skipped with a one-line note.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

_NAME_RE = re.compile(r"^(?P<mode>[a-zA-Z0-9]+)-(?P<direction>download|upload)-(?P<proto>tcp|udp)\.json$")


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _read_cpu_samples(cpu_file: Path) -> float | None:
    if not cpu_file.is_file():
        return None
    pairs: list[tuple[float, float]] = []
    singles: list[float] = []
    for line in cpu_file.read_text().splitlines():
        parts = line.split()
        try:
            if len(parts) == 2:
                pairs.append((float(parts[0]), float(parts[1])))
            elif len(parts) == 1:
                singles.append(float(parts[0]))
        except ValueError:
            continue
    if len(pairs) >= 2:
        wall = pairs[-1][0] - pairs[0][0]
        if wall > 0:
            return max(pairs[-1][1] - pairs[0][1], 0.0) / wall * 100.0
        return None
    return _mean(singles)


def _read_utun_bytes(path: Path) -> int | None:
    if not path.is_file():
        return None
    parts = path.read_text().split()
    try:
        return sum(int(p) for p in parts[:2]) if len(parts) >= 2 else None
    except ValueError:
        return None


def _tcp_stats(result: dict) -> tuple[float | None, int | None, int | None]:
    """(bits/s, retransmits, bytes), receiver side when iperf3 provides it."""
    end = result.get("end") or {}
    summary = end.get("sum_received") or end.get("sum_sent") or {}
    retransmits = (end.get("sum_sent") or {}).get("retransmits")
    return summary.get("bits_per_second"), retransmits, summary.get("bytes")


def _udp_stats(result: dict) -> tuple[float | None, float | None, int | None]:
    """(goodput bits/s, loss percent, received bytes).

    Goodput = received bytes * 8 / duration. Receiver-side values
    (``sum_received``) are used when iperf3 provides them. Otherwise, for a
    reverse (``-R``) run the client *is* the receiver, so ``sum`` already
    counts received bytes; for a forward run ``sum`` counts sent bytes, which
    are scaled by the reported loss.
    """
    end = result.get("end") or {}
    reverse = bool(((result.get("start") or {}).get("test_start") or {}).get("reverse"))
    received = end.get("sum_received")
    summary = received or end.get("sum") or {}
    lost_percent = summary.get("lost_percent")
    if lost_percent is None:
        lost_percent = (end.get("sum") or {}).get("lost_percent")
    nbytes = summary.get("bytes")
    if nbytes is not None and not received and not reverse:
        packets, lost = summary.get("packets"), summary.get("lost_packets")
        if packets and lost is not None:
            nbytes = nbytes * (packets - lost) / packets
        elif lost_percent is not None:
            nbytes = nbytes * (1 - lost_percent / 100.0)
        nbytes = int(round(nbytes))
    seconds = summary.get("seconds") or (end.get("sum") or {}).get("seconds")
    if not seconds:
        seconds = ((result.get("start") or {}).get("test_start") or {}).get("duration")
    bps = nbytes * 8 / seconds if nbytes is not None and seconds else summary.get("bits_per_second")
    return bps, lost_percent, nbytes


def _mb(nbytes: float | None) -> str:
    return f"{nbytes / 1e6:.2f}" if nbytes is not None else "n/a"


_HEADER = (
    "| Mode | Direction | Protocol | Mbit/s | MB transferred | Retransmits | UDP loss "
    "| CPU % (fm350mac) | utun MB | Error |"
)


def summarize(results_dir: Path) -> str:
    rows = []
    notes = []
    total_bytes = 0
    total_utun = 0
    any_utun = False
    for json_path in sorted(results_dir.glob("*.json")):
        m = _NAME_RE.match(json_path.name)
        if m is None:
            continue
        mode, direction, proto = m["mode"], m["direction"], m["proto"]
        try:
            result = json.loads(json_path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            notes.append(f"- `{json_path.name}`: could not parse JSON ({exc})")
            continue

        if not isinstance(result, dict):
            notes.append(f"- `{json_path.name}`: unexpected JSON content, skipped")
            continue

        utun = _read_utun_bytes(json_path.with_suffix(".utun"))
        if utun is not None:
            any_utun = True
            total_utun += utun
        utun_str = _mb(utun)

        if "error" in result:
            notes.append(f"- `{json_path.name}`: iperf3 error: {result['error']}")
            rows.append((mode, direction, proto, "ERROR", "n/a", "", "", "n/a", utun_str, str(result["error"])))
            continue

        cpu = _read_cpu_samples(json_path.with_suffix(".cpu"))
        cpu_str = f"{cpu:.1f}" if cpu is not None else "n/a"

        retrans_str = loss_str = ""
        try:
            if proto == "tcp":
                bps, retransmits, nbytes = _tcp_stats(result)
                retrans_str = str(retransmits) if retransmits is not None else "n/a"
            else:
                bps, lost_percent, nbytes = _udp_stats(result)
                loss_str = f"{lost_percent:.2f}%" if lost_percent is not None else "n/a"
        except (AttributeError, TypeError, ValueError) as exc:
            # Valid JSON of an unexpected shape (e.g. "end": [..]): note it
            # and keep summarizing the other runs.
            notes.append(f"- `{json_path.name}`: unexpected iperf3 JSON shape ({exc}), skipped")
            continue
        mbps_str = f"{bps / 1e6:.1f}" if bps is not None else "n/a"
        if nbytes is not None:
            total_bytes += nbytes

        rows.append((mode, direction, proto, mbps_str, _mb(nbytes), retrans_str, loss_str, cpu_str, utun_str, ""))

    lines = [_HEADER, "|---|---|---|---|---|---|---|---|---|---|"]
    if not rows:
        lines.append("| (no results found) | | | | | | | | | |")
    else:
        for row in rows:
            lines.append("| " + " | ".join(row) + " |")
        total_utun_str = _mb(total_utun) if any_utun else "n/a"
        lines.append(f"| **total** | | | | {_mb(total_bytes)} | | | | {total_utun_str} | |")

    sim_lines = []
    for sim_path in sorted(results_dir.glob("sim-*.txt")):
        try:
            rx, tx = (int(v) for v in sim_path.read_text().split()[:2])
        except (OSError, ValueError):
            continue
        mode = sim_path.stem[len("sim-"):]
        sim_lines.append(f"- {mode}: rx {_mb(rx)} MB, tx {_mb(tx)} MB (total {_mb(rx + tx)} MB)")
    if sim_lines:
        lines.append("")
        lines.append("SIM usage per session (driver count, authoritative):")
        lines.extend(sim_lines)
    if any_utun:
        notes.append(
            "- utun MB is from macOS's interface counters, which count received "
            "packets twice on a utun (about 2x the real rx); use the SIM usage above."
        )

    if notes:
        lines.append("")
        lines.append("Notes:")
        lines.extend(notes)

    return "\n".join(lines) + "\n"


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(f"usage: {argv[0]} <results-dir>", file=sys.stderr)
        return 2
    results_dir = Path(argv[1])
    if not results_dir.is_dir():
        print(f"not a directory: {results_dir}", file=sys.stderr)
        return 2
    sys.stdout.write(summarize(results_dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
