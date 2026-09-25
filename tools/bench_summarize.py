#!/usr/bin/env python3
"""Turn the iperf3 --json results dropped by bench-throughput.sh into a
Markdown summary table. Stdlib only, no third-party dependencies.

Usage: bench_summarize.py <results-dir>

Expects files named "<mode>-<direction>-<protocol>.json" (e.g.
"sync-download-tcp.json") plus an optional sibling "<mode>-<direction>-
<protocol>.cpu" holding one %CPU sample per line (as printed by
`ps -o %cpu=`), sampled from the fm350mac process while iperf3 ran.
Prints a Markdown table to stdout; anything that doesn't match the naming
convention, or can't be parsed, is skipped with a one-line note.
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
    samples = []
    for line in cpu_file.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            samples.append(float(line))
        except ValueError:
            continue
    return _mean(samples)


def _tcp_stats(result: dict) -> tuple[float | None, int | None]:
    end = result.get("end", {})
    summary = end.get("sum_received") or end.get("sum_sent") or {}
    bps = summary.get("bits_per_second")
    retransmits = end.get("sum_sent", {}).get("retransmits")
    return bps, retransmits


def _udp_stats(result: dict) -> tuple[float | None, float | None]:
    end = result.get("end", {})
    summary = end.get("sum") or {}
    bps = summary.get("bits_per_second")
    lost_percent = summary.get("lost_percent")
    return bps, lost_percent


def summarize(results_dir: Path) -> str:
    rows = []
    notes = []
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

        if "error" in result:
            notes.append(f"- `{json_path.name}`: iperf3 error: {result['error']}")
            rows.append((mode, direction, proto, "ERROR", str(result["error"]), ""))
            continue

        cpu = _read_cpu_samples(json_path.with_suffix(".cpu"))
        cpu_str = f"{cpu:.1f}" if cpu is not None else "n/a"

        if proto == "tcp":
            bps, retransmits = _tcp_stats(result)
            mbps_str = f"{bps / 1e6:.1f}" if bps is not None else "n/a"
            retrans_str = str(retransmits) if retransmits is not None else "n/a"
        else:
            bps, lost_percent = _udp_stats(result)
            mbps_str = f"{bps / 1e6:.1f}" if bps is not None else "n/a"
            retrans_str = f"loss {lost_percent:.2f}%" if lost_percent is not None else "n/a"

        rows.append((mode, direction, proto, mbps_str, retrans_str, cpu_str))

    lines = []
    lines.append("| Mode | Direction | Protocol | Mbit/s | Retransmits | CPU % (fm350mac) |")
    lines.append("|---|---|---|---|---|---|")
    if not rows:
        lines.append("| (no results found) | | | | | |")
    else:
        for mode, direction, proto, mbps, retrans, cpu in rows:
            lines.append(f"| {mode} | {direction} | {proto} | {mbps} | {retrans} | {cpu} |")

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
