#!/usr/bin/env bash
# module-telemetry.sh -- record the FM350's own view of things while a command runs.
#
#   tools/module-telemetry.sh [--out DIR] [--fast-vbat] -- COMMAND [ARGS...]
#
# Example (reproduce an upload crash and keep what the module saw):
#   tools/module-telemetry.sh -- tools/bench-throughput.sh --apn internet.telekom \
#       --server <iperf3-host> --tests up --max-bytes 500M --yes
#
# Over ADB (the module's root shell, see docs/dell-dw5931e-usb.md, Step 3) it
# streams two files into DIR, every line stamped with the Mac's clock so it lines
# up with the driver's log:
#   module-log.txt      the module's syslog, kernel lines included (logread -f)
#   module-sensors.txt  supply voltage as the module's PMIC measures it (BATADC, mV)
#                       and PA/RF/PMIC temperatures (milli-degC), ~10 samples/s
#                       (1/s if the module's busybox can't sleep fractions; the
#                       first line says which)
#   module-vbat-fast.txt  only with --fast-vbat: the supply read in a tight loop,
#                       one line per 0.1 s of module uptime with that window's
#                       min/max mV and sample count, so a dip shorter than the 1 s
#                       sensor interval still shows. It keeps one module CPU core busy.
# Then it runs COMMAND, stops the recorders and prints the last samples.
#
# Why: when the module crashes and reboots, its own logs are lost (its ramoops
# area comes up invalid at boot), so whatever it logged or measured just before
# a reset survives only if it was streamed off the module while it happened.
#
# Read-only on the module. The files can contain your operator, cell and IP
# details: don't publish them unredacted. Exit status: COMMAND's, or 2 if ADB
# isn't ready or a recorder did not start (nothing is run then). At the end it
# prints when each stream's last line arrived, and says "ended at" if the
# recorder was already gone (module reset or ADB lost) before COMMAND finished.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT=""
FAST_VBAT=0

# The leading comment block, without the shebang.
usage() { awk 'NR > 1 { if (!/^#/) exit; sub(/^# ?/, ""); print }' "${BASH_SOURCE[0]}"; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        --out) OUT="${2:?--out needs a directory}"; shift 2 ;;
        --fast-vbat) FAST_VBAT=1; shift ;;
        --) shift; break ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown option: $1 (the command goes after --)" >&2; exit 2 ;;
    esac
done
[[ $# -gt 0 ]] || { usage >&2; exit 2; }

OUT="${OUT:-$SCRIPT_DIR/telemetry-results/$(date '+%Y%m%d-%H%M%S')-$$}"
if [[ -d "$OUT" && -n "$(ls -A -- "$OUT")" ]]; then
    echo "refusing: $OUT exists and is not empty; pick a new --out" >&2
    exit 2
fi
mkdir -p "$OUT"

serials="$(adb devices 2>&1 | awk -F'\t' '$2 == "device" { print $1 }')"
if [[ -z "$serials" ]]; then
    adb devices >&2 || true
    echo "ADB is not ready. ADB offline: stop the ADB server (\`adb kill-server\`) before the module restarts" >&2
    echo "and start it once the module is up; if it is already offline, a USB port reset is the remedy" >&2
    echo "(it may leave the module's log daemon spinning, so replug before measurements):" >&2
    echo "  python3 tools/fm350_diag.py usb-reset" >&2
    exit 2
fi
# Exactly one device in the `device` state: address it, so a second (offline or
# other) device can't make `adb shell` fail with "more than one device".
ADB_SERIAL=""
if [[ "$serials" =~ ^[A-Za-z0-9._:-]+$ ]]; then ADB_SERIAL="$serials"; fi
export ADB_SERIAL

# record CMD FILE: run `adb shell CMD`, prefixing each line with the Mac's clock
# (ms). Python owns adb, so stopping the recorder (TERM) stops adb too. -I: no
# import from the current directory. Carriage returns (adb shell may add them)
# are dropped, they would corrupt the column names.
# Always run it in the background (`record ... &`): it execs.
record() {
    exec python3 -I -u -c '
import os, signal, subprocess, sys, time
serial = os.environ.get("ADB_SERIAL")
p = subprocess.Popen(["adb"] + (["-s", serial] if serial else []) + ["shell", sys.argv[1]],
                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
signal.signal(signal.SIGTERM, lambda *_: (p.terminate(), sys.exit(0)))
for raw in p.stdout:
    t = time.time()
    sys.stdout.write(time.strftime("%H:%M:%S", time.localtime(t)) + ".%03d " % (t % 1 * 1000)
                     + raw.decode("utf-8", "replace").replace("\r", ""))
    sys.stdout.flush()
p.wait()' "$1" > "$2"
}

# Is background job $1 still running? Not `kill -0`: that succeeds on an exited
# child that has not been waited for yet.
alive() { jobs -rp | grep -Fx "$1" >/dev/null; }

# Runs on the module (busybox ash). Sensors are picked by name, not number.
# shellcheck disable=SC2016 # expanded on the module, not here
SAMPLER='
zones=""; names=""
for want in ltepa_ntc nrpa_ntc rf_ntc md_rf pmic; do
    for z in /sys/class/thermal/thermal_zone*; do
        read t < "$z/type"
        [ "$t" = "$want" ] && { zones="$zones $z/temp"; names="$names ${want}_mC"; }
    done
done
vbat=""
for d in /sys/bus/iio/devices/iio:device*; do
    [ -f "$d/in_voltage0_BATADC_input" ] && vbat="$d/in_voltage0_BATADC_input"
done
if sleep 0.01 2>/dev/null; then nap="sleep 0.1"
elif usleep 1 2>/dev/null; then nap="usleep 100000"
else nap="sleep 1"; fi
echo "# sampling: $nap"
echo "# uptime_s vbat_mV$names"
while :; do
    read up _ < /proc/uptime
    line="$up"
    if [ -n "$vbat" ]; then read x < "$vbat"; line="$line $x"; else line="$line -"; fi
    for f in $zones; do read x < "$f"; line="$line $x"; done
    echo "$line"
    $nap
done'

record 'logread -f -l 5' "$OUT/module-log.txt" &
LOG_PID=$!
record "$SAMPLER" "$OUT/module-sensors.txt" &
SENS_PID=$!

# Busybox here can't sleep fractions, so the fast reader spins; it prints one
# line per 0.1 s window (uptime with its last digit cut) holding the window's min.
# shellcheck disable=SC2016 # expanded on the module, not here
FAST='
vbat=""
for d in /sys/bus/iio/devices/iio:device*; do
    [ -f "$d/in_voltage0_BATADC_input" ] && vbat="$d/in_voltage0_BATADC_input"
done
[ -n "$vbat" ] || { echo "# no BATADC sensor"; exec sleep 100000; }
echo "# window_uptime_s vbat_min_mV vbat_max_mV samples"
cur=""; mn=0; mx=0; n=0
while :; do
    read up _ < /proc/uptime
    read v < "$vbat"
    w=${up%?}
    if [ "$w" != "$cur" ]; then
        [ -n "$cur" ] && echo "$cur $mn $mx $n"
        cur=$w; mn=$v; mx=$v; n=0
    fi
    [ "$v" -lt "$mn" ] && mn=$v
    [ "$v" -gt "$mx" ] && mx=$v
    n=$((n + 1))
done'
FAST_PID=""
if [[ "$FAST_VBAT" -eq 1 ]]; then
    record "$FAST" "$OUT/module-vbat-fast.txt" &
    FAST_PID=$!
fi

stop_recorders() {
    # shellcheck disable=SC2086 # FAST_PID is empty without --fast-vbat
    kill "$LOG_PID" "$SENS_PID" $FAST_PID 2>/dev/null || true
    # shellcheck disable=SC2086
    wait "$LOG_PID" "$SENS_PID" $FAST_PID 2>/dev/null || true
}
trap stop_recorders EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

sleep 2  # let both streams start before the command does anything
streams=("$LOG_PID:$OUT/module-log.txt" "$SENS_PID:$OUT/module-sensors.txt")
[[ -z "$FAST_PID" ]] || streams+=("$FAST_PID:$OUT/module-vbat-fast.txt")
for stream in "${streams[@]}"; do
    if ! alive "${stream%%:*}" || [[ ! -s "${stream#*:}" ]]; then
        echo "recorder for ${stream#*:} did not start (several ADB devices attached? module busy?); nothing was run" >&2
        exit 2
    fi
done
rc=0
"$@" || rc=$?
sleep 2  # keep the tail: a crash shows up in the command a moment before the streams end
log_alive=0; sens_alive=0
if alive "$LOG_PID"; then log_alive=1; fi
if alive "$SENS_PID"; then sens_alive=1; fi
fast_alive=0
if [[ -n "$FAST_PID" ]] && alive "$FAST_PID"; then fast_alive=1; fi
stop_recorders

# Killing the Mac-side adb client doesn't always stop the module-side `logread -f`
# and sampler, so remove leftovers while the module is still up. The patterns
# are split ("thermal""_zone") so this script's own command line doesn't match.
# shellcheck disable=SC2016 # expanded on the module, not here
CLEANUP=': fm350-telemetry-cleanup
n=0
for p in /proc/[0-9]*; do
    pid=${p#/proc/}; [ "$pid" = "$$" ] && continue
    c=$(tr "\000" " " < "$p/cmdline" 2>/dev/null) || continue
    case "$c" in *"logread -f -l"" 5"* | *"thermal""_zone"* | *"BATADC""_input"*) kill "$pid" 2>/dev/null && n=$((n + 1)) ;; esac
done
echo "$n"'
if [[ "$log_alive" -eq 1 || "$sens_alive" -eq 1 ]]; then
    if left="$(adb ${ADB_SERIAL:+-s "$ADB_SERIAL"} shell "$CLEANUP" 2>/dev/null)"; then
        echo "module: stopped ${left//[!0-9]/} leftover recorder process(es)"
    else
        echo "module: could not check for leftover recorder processes (adb shell failed)"
    fi
fi

echo "--- module telemetry in $OUT"
# The last line's timestamp is the signal when the module crashed: the streams end with it.
report_stream() {
    local last
    last="$(tail -n 1 "$2" | cut -d' ' -f1)"
    if [[ "$3" -eq 1 ]]; then
        echo "$1: last line at ${last:-n/a} (recorder still running)"
    else
        echo "$1: ended at ${last:-n/a} (recorder was gone before the command finished: module reset or ADB lost)"
    fi
}
report_stream module-log.txt "$OUT/module-log.txt" "$log_alive"
report_stream module-sensors.txt "$OUT/module-sensors.txt" "$sens_alive"
if [[ -n "$FAST_PID" ]]; then
    report_stream module-vbat-fast.txt "$OUT/module-vbat-fast.txt" "$fast_alive"
    echo "fast supply, last 3 s (Mac time, 0.1 s window, min mV, max mV, samples):"
    grep -v ' # ' "$OUT/module-vbat-fast.txt" | tail -n 30 || true
    awk '$2 ~ /^[0-9.]+$/ && $3 ~ /^[0-9]+$/ { n += $5; w++; if (min == "" || $3 + 0 < min) { min = $3 + 0; at = $1 } }
         END { printf "fast supply: windows=%d samples=%d min_mV=%s at %s\n", w, n, (min == "" ? "n/a" : min), (at == "" ? "n/a" : at) }' \
        "$OUT/module-vbat-fast.txt"
fi
echo "last samples (Mac time, uptime_s, then the columns named in the header):"
grep -v ' # ' "$OUT/module-sensors.txt" | tail -n 12 || true
# Header: "<time> # uptime_s vbat_mV <name>..."; samples: "<time> <uptime> <vbat> <value>...".
awk '$2 == "#" && $3 == "uptime_s" { for (i = 5; i <= NF; i++) name[i - 1] = $i; next }
     $2 == "#" { print "module " substr($0, index($0, "#") + 2); next }
     $2 ~ /^[0-9.]+$/ {
         n++; if ($3 ~ /^[0-9]+$/ && (min == "" || $3 + 0 < min)) min = $3 + 0
         for (i = 4; i <= NF; i++) if (!(i in max) || $i + 0 > max[i]) max[i] = $i + 0
     }
     END {
         printf "samples=%d vbat_min_mV=%s", n, (min == "" ? "n/a" : min)
         for (i = 4; i in max; i++) printf " max_%s=%s", name[i], max[i]
         print ""
     }' "$OUT/module-sensors.txt"
exit "$rc"
