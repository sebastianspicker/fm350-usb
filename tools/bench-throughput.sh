#!/usr/bin/env bash
# bench-throughput.sh -- compare fm350mac's two USB data paths (`--io sync`
# vs `--io async`) for throughput.
#
# Real mode (needs a data SIM + an iperf3 server you control):
#   tools/bench-throughput.sh --apn <apn> --server <iperf3-host> [--udp-bitrate 50M]
#
# Loopback mode (no SIM, no USB device; today's dry run against the
# in-process fake modem, see fm350mac/src/fm350mac/loopback.py):
#   tools/bench-throughput.sh --loopback
#
# --dry-run prints the plan (every command that would run) without
# touching the system, in either mode.
#
# Requires the fm350mac root helper to already be installed (see
# `fm350mac helper install`) so that `fm350mac up` needs no sudo.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
FM350MAC="$REPO_ROOT/fm350mac/.venv/bin/fm350mac"
SUMMARIZE="$SCRIPT_DIR/bench_summarize.py"
VENV_PYTHON="$REPO_ROOT/fm350mac/.venv/bin/python3"

LOOPBACK_PEER="198.51.100.1"

# -- defaults, overridable on the command line -------------------------------
MODES=(sync async)
DURATION=20
LOOPBACK=0
DRY_RUN=0
APN=""
SERVER=""
PORT=5201
UDP_BITRATE=""
RESULTS_DIR=""
PING_COUNT=500
PING_INTERVAL=0.01
PING_SIZE=1400
UP_WAIT_TIMEOUT=15

usage() {
    cat <<'EOF'
Usage:
  bench-throughput.sh --apn APN --server HOST [options]      # real modem, iperf3
  bench-throughput.sh --loopback [options]                    # no SIM, ping the fake modem
  bench-throughput.sh --dry-run [--loopback] [--apn APN --server HOST] [options]

Options:
  --apn APN            APN to use (required unless --loopback)
  --server HOST        iperf3 server to test against (required unless --loopback)
  --port PORT           iperf3 server port (default: 5201)
  --duration SECONDS     seconds per iperf3 test, TCP and UDP alike (default: 20)
  --udp-bitrate RATE     also run a UDP test at this target rate (e.g. 100M); omitted by default
  --modes LIST           comma-separated bridge modes to test (default: sync,async)
  --results-dir DIR      where to write results (default: tools/bench-results/<timestamp>-<mode>)
  --ping-count N         --loopback only: pings per bridge mode (default: 500)
  --ping-interval SEC    --loopback only: ping -i value (default: 0.01)
  --ping-size BYTES      --loopback only: ping -s value (default: 1400)
  --loopback             use the in-process fake modem instead of a real SIM/USB device
  --dry-run              print the plan and every command, run nothing
  -h, --help             show this help

Real mode runs, for each bridge mode: fm350mac up --apn APN --io MODE
--default-route, iperf3 TCP download (-R) and upload (and UDP if
--udp-bitrate is given), then stops the session and verifies teardown.
fm350mac up has no option to route only the iperf3 server through the
tunnel (no host-route flag, only --default-route) as of this writing, so
this harness moves the Mac's default route for the duration of the test;
see the README note below the plan.

Loopback mode instead pings the fake modem's peer (198.51.100.1) and
reports loss, average RTT and packets/s per bridge mode -- no iperf3 or
SIM needed.
EOF
}

log() {
    printf '[%s] %s\n' "$(date '+%H:%M:%S')" "$*" >&2
}

# -- argument parsing ---------------------------------------------------------
while [[ $# -gt 0 ]]; do
    case "$1" in
        --apn) APN="$2"; shift 2 ;;
        --server) SERVER="$2"; shift 2 ;;
        --port) PORT="$2"; shift 2 ;;
        --duration) DURATION="$2"; shift 2 ;;
        --udp-bitrate) UDP_BITRATE="$2"; shift 2 ;;
        --modes)
            IFS=',' read -r -a MODES <<< "$2"
            shift 2
            ;;
        --results-dir) RESULTS_DIR="$2"; shift 2 ;;
        --ping-count) PING_COUNT="$2"; shift 2 ;;
        --ping-interval) PING_INTERVAL="$2"; shift 2 ;;
        --ping-size) PING_SIZE="$2"; shift 2 ;;
        --loopback) LOOPBACK=1; shift ;;
        --dry-run) DRY_RUN=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *)
            echo "unknown option: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

for mode in "${MODES[@]}"; do
    if [[ "$mode" != "sync" && "$mode" != "async" ]]; then
        echo "unknown --io mode: $mode (expected sync or async)" >&2
        exit 2
    fi
done

if [[ "$LOOPBACK" -eq 1 ]]; then
    APN="${APN:-bench-loopback}"
else
    if [[ -z "$APN" || -z "$SERVER" ]]; then
        echo "real mode needs both --apn and --server (no default server is hard-coded)" >&2
        exit 2
    fi
fi

if [[ -z "$RESULTS_DIR" ]]; then
    kind="iperf3"
    [[ "$LOOPBACK" -eq 1 ]] && kind="loopback"
    RESULTS_DIR="$SCRIPT_DIR/bench-results/$(date '+%Y%m%d-%H%M%S')-$kind"
fi

if ! command -v iperf3 >/dev/null 2>&1 && [[ "$LOOPBACK" -ne 1 ]]; then
    echo "iperf3 not found: brew install iperf3" >&2
    [[ "$DRY_RUN" -eq 1 ]] || exit 1
fi

if [[ ! -x "$FM350MAC" ]]; then
    echo "fm350mac CLI not found at $FM350MAC (expected the fm350mac/.venv virtualenv)" >&2
    exit 1
fi

# Detected by actually invoking `status --help` (harmless, no root/AT/USB
# needed) -- skipped under --dry-run, which must not run fm350mac at all.
STATUS_JSON_SUPPORTED=0
if [[ "$DRY_RUN" -ne 1 ]] && "$FM350MAC" status --help 2>&1 | grep -q -- '--json'; then
    STATUS_JSON_SUPPORTED=1
fi

# -- helpers ------------------------------------------------------------------

list_utuns() {
    ifconfig -l | tr ' ' '\n' | grep '^utun' || true
}

# Poll ifconfig -l until an interface shows up that wasn't in $1 ("$2" is the
# pre-existing list, newline-separated), or until $3 seconds pass. Prints the
# new interface name (empty if none appeared).
wait_for_new_utun() {
    local before="$1" timeout_s="$2"
    local waited=0
    while (( waited < timeout_s )); do
        local after new_ifs
        after="$(list_utuns)"
        new_ifs="$(comm -13 <(sort <<< "$before") <(sort <<< "$after"))"
        if [[ -n "$new_ifs" ]]; then
            head -n1 <<< "$new_ifs"
            return 0
        fi
        sleep 1
        waited=$((waited + 1))
    done
    echo ""
}

# Globals used to hand the running `up` process and its utun name from
# start_up()/run_loopback_mode() to the cleanup trap and to verify_teardown().
UP_PID=""
UP_IFACE=""

stop_up() {
    local pid="$1"
    if [[ -z "$pid" ]] || ! kill -0 "$pid" 2>/dev/null; then
        return 0
    fi
    log "stopping fm350mac up (pid $pid, SIGINT)"
    kill -INT "$pid" 2>/dev/null || true
    local waited=0
    while kill -0 "$pid" 2>/dev/null && (( waited < 10 )); do
        sleep 1
        waited=$((waited + 1))
    done
    if kill -0 "$pid" 2>/dev/null; then
        log "pid $pid still alive after SIGINT, sending SIGTERM"
        kill -TERM "$pid" 2>/dev/null || true
        sleep 2
    fi
    if kill -0 "$pid" 2>/dev/null; then
        log "pid $pid still alive after SIGTERM, sending SIGKILL"
        kill -KILL "$pid" 2>/dev/null || true
    fi
    wait "$pid" 2>/dev/null || true
}

# Check that the utun this run created is gone, and that whatever route it
# owned is gone too (the loopback host route, or -- in real mode -- the
# default route handed to it by --default-route). Logs a warning and
# returns 1 on any leftover; never raises (safe to call from the cleanup
# trap).
verify_teardown() {
    local iface="$1"
    local ok=0
    if [[ -n "$iface" ]] && list_utuns | grep -qx "$iface"; then
        log "WARNING: $iface is still present after teardown"
        ok=1
    fi
    if [[ "$LOOPBACK" -eq 1 ]]; then
        if netstat -rn -f inet | grep -q "^${LOOPBACK_PEER} "; then
            log "WARNING: loopback host route to $LOOPBACK_PEER is still present after teardown"
            ok=1
        fi
    elif [[ -n "$iface" ]] && netstat -rn -f inet | grep '^default ' | grep -q "$iface"; then
        log "WARNING: default route still points at $iface after teardown"
        ok=1
    fi
    return $ok
}

cleanup() {
    local rc=$?
    trap - EXIT INT TERM
    if [[ -n "$UP_PID" ]] && kill -0 "$UP_PID" 2>/dev/null; then
        log "cleanup: fm350mac up is still running, stopping it"
        stop_up "$UP_PID"
        UP_PID=""
    fi
    if [[ -n "$UP_IFACE" ]]; then
        verify_teardown "$UP_IFACE" || true
    fi
    exit "$rc"
}
trap cleanup EXIT INT TERM

# Sample `ps -o %cpu=` for $pid once a second, up to $max_seconds, appending
# each sample to $outfile. Meant to be run in the background for the
# duration of one iperf3 test.
sample_cpu() {
    local pid="$1" outfile="$2" max_seconds="$3"
    : > "$outfile"
    local elapsed=0
    while kill -0 "$pid" 2>/dev/null && (( elapsed < max_seconds )); do
        ps -p "$pid" -o %cpu= 2>/dev/null >> "$outfile" || true
        sleep 1
        elapsed=$((elapsed + 1))
    done
}

run_iperf() {
    local mode="$1" direction="$2" proto="$3" out_json="$4" cpu_file="$5" up_pid="$6"
    local -a args=(-c "$SERVER" -p "$PORT" -t "$DURATION" --json)
    [[ "$direction" == "download" ]] && args+=(-R)
    if [[ "$proto" == "udp" ]]; then
        args+=(-u -b "$UDP_BITRATE")
    fi
    log "iperf3 ${args[*]} (mode=$mode direction=$direction proto=$proto)"
    sample_cpu "$up_pid" "$cpu_file" "$((DURATION + 5))" &
    local sampler_pid=$!
    if ! iperf3 "${args[@]}" > "$out_json" 2> "${out_json}.stderr"; then
        log "WARNING: iperf3 $direction/$proto failed for mode=$mode; see ${out_json}.stderr"
    fi
    wait "$sampler_pid" 2>/dev/null || true
}

start_up_real() {
    local mode="$1"
    local before_utuns
    before_utuns="$(list_utuns)"
    local -a cmd=("$FM350MAC" up --apn "$APN" --io "$mode" --default-route)
    log "starting: ${cmd[*]}"
    "${cmd[@]}" > "$RESULTS_DIR/up-$mode.log" 2>&1 &
    UP_PID=$!
    UP_IFACE="$(wait_for_new_utun "$before_utuns" "$UP_WAIT_TIMEOUT")"
    if [[ -z "$UP_IFACE" ]]; then
        log "ERROR: utun did not appear within ${UP_WAIT_TIMEOUT}s for mode=$mode"
        stop_up "$UP_PID"
        UP_PID=""
        return 1
    fi
    log "utun $UP_IFACE is up (mode=$mode)"
    return 0
}

run_real_mode() {
    for mode in "${MODES[@]}"; do
        if ! start_up_real "$mode"; then
            continue
        fi

        if [[ "$STATUS_JSON_SUPPORTED" -eq 1 ]]; then
            "$FM350MAC" status --json > "$RESULTS_DIR/status-before-$mode.json" 2>&1 || true
        fi

        run_iperf "$mode" download tcp \
            "$RESULTS_DIR/$mode-download-tcp.json" "$RESULTS_DIR/$mode-download-tcp.cpu" "$UP_PID"
        run_iperf "$mode" upload tcp \
            "$RESULTS_DIR/$mode-upload-tcp.json" "$RESULTS_DIR/$mode-upload-tcp.cpu" "$UP_PID"
        if [[ -n "$UDP_BITRATE" ]]; then
            run_iperf "$mode" download udp \
                "$RESULTS_DIR/$mode-download-udp.json" "$RESULTS_DIR/$mode-download-udp.cpu" "$UP_PID"
            run_iperf "$mode" upload udp \
                "$RESULTS_DIR/$mode-upload-udp.json" "$RESULTS_DIR/$mode-upload-udp.cpu" "$UP_PID"
        fi

        if [[ "$STATUS_JSON_SUPPORTED" -eq 1 ]]; then
            "$FM350MAC" status --json > "$RESULTS_DIR/status-after-$mode.json" 2>&1 || true
        fi

        stop_up "$UP_PID"
        UP_PID=""
        verify_teardown "$UP_IFACE" || log "WARNING: teardown check failed for mode=$mode"
        UP_IFACE=""
    done

    local python_bin="python3"
    command -v python3 >/dev/null 2>&1 || python_bin="$VENV_PYTHON"
    "$python_bin" "$SUMMARIZE" "$RESULTS_DIR" | tee "$RESULTS_DIR/SUMMARY.md"
    log "results in $RESULTS_DIR"
}

# -- loopback mode -------------------------------------------------------------

# Parses macOS ping(8) summary output; sets the globals below.
PING_TRANSMITTED=""
PING_RECEIVED=""
PING_LOSS_PCT=""
PING_AVG_MS=""

parse_ping_log() {
    local ping_log="$1"
    local stats_line rtt_line
    stats_line="$(grep 'packets transmitted' "$ping_log" || true)"
    rtt_line="$(grep 'round-trip' "$ping_log" || true)"
    PING_TRANSMITTED="$(awk '{print $1}' <<< "$stats_line")"
    PING_RECEIVED="$(awk '{print $4}' <<< "$stats_line")"
    PING_LOSS_PCT="$(awk '{print $7}' <<< "$stats_line" | tr -d '%')"
    PING_AVG_MS="$(awk -F'= ' '{print $2}' <<< "$rtt_line" | awk -F'/' '{print $2}')"
}

run_loopback_mode() {
    local -a summary_rows=()
    for mode in "${MODES[@]}"; do
        local before_utuns
        before_utuns="$(list_utuns)"
        local -a cmd=("$FM350MAC" up --apn "$APN" --loopback --io "$mode")
        log "starting: ${cmd[*]}"
        "${cmd[@]}" > "$RESULTS_DIR/up-loopback-$mode.log" 2>&1 &
        UP_PID=$!
        UP_IFACE="$(wait_for_new_utun "$before_utuns" "$UP_WAIT_TIMEOUT")"
        if [[ -z "$UP_IFACE" ]]; then
            log "ERROR: utun did not appear within ${UP_WAIT_TIMEOUT}s for mode=$mode"
            stop_up "$UP_PID"
            UP_PID=""
            continue
        fi
        log "utun $UP_IFACE is up (loopback, mode=$mode)"

        local ping_log="$RESULTS_DIR/loopback-$mode-ping.log"
        local -a ping_cmd=(ping -i "$PING_INTERVAL" -s "$PING_SIZE" -c "$PING_COUNT" "$LOOPBACK_PEER")
        log "${ping_cmd[*]}"
        local t0 t1
        t0="$(date +%s.%N)"
        "${ping_cmd[@]}" > "$ping_log" 2>&1 || true
        t1="$(date +%s.%N)"

        stop_up "$UP_PID"
        UP_PID=""
        verify_teardown "$UP_IFACE" || log "WARNING: teardown check failed for mode=$mode"
        UP_IFACE=""

        parse_ping_log "$ping_log"
        local elapsed pps
        elapsed="$(awk "BEGIN { print $t1 - $t0 }")"
        if [[ -n "$PING_TRANSMITTED" ]] && awk "BEGIN { exit !($elapsed > 0) }"; then
            pps="$(awk "BEGIN { printf \"%.1f\", $PING_TRANSMITTED / $elapsed }")"
        else
            pps="n/a"
        fi
        summary_rows+=("| $mode | ${PING_TRANSMITTED:-n/a} | ${PING_RECEIVED:-n/a} | ${PING_LOSS_PCT:-n/a}% | ${PING_AVG_MS:-n/a} | $pps |")
    done

    {
        echo "| Mode | Sent | Received | Loss | Avg RTT (ms) | Packets/s |"
        echo "|---|---|---|---|---|---|"
        printf '%s\n' "${summary_rows[@]}"
    } | tee "$RESULTS_DIR/loopback-summary.md"
    log "results in $RESULTS_DIR"
}

# -- dry-run: print the plan, touch nothing -----------------------------------

print_plan() {
    echo "Plan (dry-run, nothing will be executed):"
    echo "  results dir: $RESULTS_DIR"
    echo "  fm350mac status --json support is detected at run time (fm350mac status --help);" \
         "the status-before/-after capture below runs only if it's supported"
    echo
    if [[ "$LOOPBACK" -eq 1 ]]; then
        for mode in "${MODES[@]}"; do
            echo "mode=$mode:"
            echo "  $FM350MAC up --apn $APN --loopback --io $mode"
            echo "  wait for a new utun (up to ${UP_WAIT_TIMEOUT}s)"
            echo "  ping -i $PING_INTERVAL -s $PING_SIZE -c $PING_COUNT $LOOPBACK_PEER"
            echo "  kill -INT <up pid>; verify utun and the $LOOPBACK_PEER host route are gone"
            echo
        done
        echo "Then: write $RESULTS_DIR/loopback-summary.md (loss/RTT/packets-per-second per mode)."
    else
        for mode in "${MODES[@]}"; do
            echo "mode=$mode:"
            echo "  $FM350MAC up --apn $APN --io $mode --default-route"
            echo "  wait for a new utun (up to ${UP_WAIT_TIMEOUT}s)"
            echo "  $FM350MAC status --json > status-before-$mode.json   # if --json is supported"
            echo "  iperf3 -c $SERVER -p $PORT -t $DURATION --json -R   # TCP download"
            echo "  iperf3 -c $SERVER -p $PORT -t $DURATION --json      # TCP upload"
            if [[ -n "$UDP_BITRATE" ]]; then
                echo "  iperf3 -c $SERVER -p $PORT -t $DURATION --json -R -u -b $UDP_BITRATE   # UDP download"
                echo "  iperf3 -c $SERVER -p $PORT -t $DURATION --json -u -b $UDP_BITRATE      # UDP upload"
            fi
            echo "  $FM350MAC status --json > status-after-$mode.json   # if --json is supported"
            echo "  kill -INT <up pid>; verify utun and the default route are restored"
            echo
        done
        echo "Then: python3 $SUMMARIZE $RESULTS_DIR > $RESULTS_DIR/SUMMARY.md"
        echo
        echo "NOTE: 'fm350mac up' has no host-route-only option (only --apn, --pdp, --cid," \
             "--default-route, --dns, --io, ...), so this plan uses --default-route --" \
             "the iperf3 run moves the Mac's whole default route through the tunnel for" \
             "its duration. A '--route-host HOST' (or similar) flag would let this" \
             "harness route only the iperf3 server through the tunnel and leave the rest" \
             "of the Mac's traffic alone."
    fi
}

# -- main ----------------------------------------------------------------------

if [[ "$DRY_RUN" -eq 1 ]]; then
    print_plan
    exit 0
fi

mkdir -p "$RESULTS_DIR"

if [[ "$LOOPBACK" -eq 1 ]]; then
    run_loopback_mode
else
    run_real_mode
fi
