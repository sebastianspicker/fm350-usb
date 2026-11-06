#!/usr/bin/env bash
# module-readout.sh -- one-shot, read-only snapshot of the FM350's boot and reset state over ADB.
#
#   tools/module-readout.sh [LABEL]
#
# Writes tools/telemetry-results/readout-<date>-<time>[-LABEL].txt and prints its path.
# Take one after a crash (usb-reset, NO replug: a replug power-cycles the module) and one on a
# healthy boot, then diff them. Sections: uptime, kernel command line, ramoops/pstore/watchdog/ccci
# dmesg lines, MT6330 PMIC exception dump and first registers, SPM watchdog latch, TOPRGU registers
# (0x10007000, via debugfs regmap), thermal zones, IIO voltages, dmesg tail.
# Needs ADB in the `device` state (see docs/diagnostics.md). The file can contain cell and IP
# details, so don't publish it unredacted.
set -euo pipefail

cd "$(dirname "$0")/.."
out="tools/telemetry-results/readout-$(date +%Y%m%d-%H%M%S)${1:+-$1}.txt"
mkdir -p tools/telemetry-results

adb get-state >/dev/null 2>&1 || { echo "module-readout: ADB not in 'device' state (adb devices)" >&2; exit 1; }

# Commands go in as an argument, not on stdin: the module's adbd has no shell v2, so stdin EOF
# never arrives and `adb shell sh <script` hangs at the prompt after the last command.
script=$(cat <<'MODULE'
echo "== uptime"; uptime
echo "== cmdline"; cat /proc/cmdline
echo "== dmesg ramoops/pstore/wdt/ccci"
dmesg | grep -iE "ramoops|pstore|persistent_ram|mrdump|mdwdt|md_wdt|ccci.*(exception|ee|wdt)|reboot-mode|wdt_status" | head -60
echo "== pstore"; ls -la /sys/fs/pstore 2>&1
echo "== pmic_dump_exception"; cat /sys/kernel/debug/mtk_pmic/pmic_dump_exception 2>&1
echo "== 0-04 name"; cat /sys/kernel/debug/regmap/0-04/name 2>&1
echo "== 0-04 regs (first 48)"; head -48 /sys/kernel/debug/regmap/0-04/registers 2>&1
echo "== spm_wdt_latch"; cat /sys/kernel/debug/spm/spm_wdt_latch 2>&1
echo "== toprgu (0x10007000)"
head -32 /sys/kernel/debug/regmap/dummy-watchdog@10007000/registers 2>&1
echo "== thermal zones (type, milli-degC)"
for f in /sys/class/thermal/thermal_zone*/; do echo "$(cat ${f}type 2>/dev/null) $(cat ${f}temp 2>/dev/null)"; done
echo "== iio voltages"
cat /sys/bus/iio/devices/iio:device*/in_voltage*_input 2>/dev/null | head -20
echo "== dmesg tail"; dmesg | tail -40
MODULE
)
adb shell "$script" </dev/null >"$out" 2>&1

echo "$out ($(wc -l <"$out" | tr -d ' ') lines)"
