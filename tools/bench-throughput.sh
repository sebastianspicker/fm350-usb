#!/usr/bin/env bash
# bench-throughput.sh -- compare fm350mac's two USB data paths (`--io sync`
# vs `--io async`) for throughput, without burning a metered SIM.
#
# Real mode (needs a data SIM + an iperf3 server you control):
#   tools/bench-throughput.sh --apn <apn> --server <iperf3-host>
#
# Volume is capped: every TCP run is `iperf3 -n <max-bytes>` (default 5M), the
# default tests are TCP download and upload in async mode only, and only the
# iperf3 server's host route goes through the tunnel (the default route is
# left alone unless you pass --full-tunnel). A pre-flight estimate of the
# worst-case data use is printed first; above 50 MB you must pass --yes.
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

# Estimates above this many bytes need --yes (or an interactive confirmation).
CONFIRM_THRESHOLD_BYTES=50000000

# -- defaults, overridable on the command line -------------------------------
MODES=(async)
TESTS=(down up)
MAX_BYTES_ARG="5M"
MAX_BYTES=5000000
DURATION=20
LOOPBACK=0
DRY_RUN=0
YES=0
FULL_TUNNEL=0
APN=""
SERVER=""
SERVER_IP=""
PORT=5201
UDP_BITRATE_ARG="5M"
UDP_BITRATE=5000000
UDP_TIME=1
RESULTS_DIR=""
PING_COUNT=500
PING_INTERVAL=0.01
PING_SIZE=1400
UP_WAIT_TIMEOUT=60  # bring-up includes AT+CGACT, which may take up to 60 s

usage() {
    cat <<'EOF'
Usage:
  bench-throughput.sh --apn APN --server HOST [options]      # real modem, iperf3
  bench-throughput.sh --loopback [options]                    # no SIM, ping the fake modem
  bench-throughput.sh --dry-run [--loopback] [--apn APN --server HOST] [options]

Options:
  --apn APN            APN to use (required unless --loopback)
  --server HOST        iperf3 server to test against (required unless --loopback)
  --port PORT          iperf3 server port (default: 5201)
  --max-bytes N        data cap per TCP run, with K/M suffix (decimal), e.g. 5M
                       (default: 5M). TCP runs use `iperf3 -n N`. 0 = no volume
                       cap: --duration is used instead (can burn a lot of data)
  --duration SECONDS   seconds per run, only when --max-bytes 0 (default: 20)
  --tests LIST         comma-separated: down, up, udp (default: down,up)
                       udp runs both directions
  --udp-bitrate RATE   UDP target rate, e.g. 5M (default: 5M). Must be > 0; the UDP
                       run time is derived from --max-bytes so it stays within the cap
                       (iperf3 ignores -n for UDP, so a rate above --max-bytes * 8 per
                       second is lowered to that)
  --modes LIST         comma-separated bridge modes to test (default: async)
  --yes                don't ask for confirmation when the estimated worst-case
                       data use is above 50 MB
  --full-tunnel        route the Mac's DEFAULT route through the tunnel
                       (fm350mac up --default-route). All Mac traffic then uses
                       the SIM; background syncs/updates can eat the data plan
  --results-dir DIR    where to write results (default: tools/bench-results/<timestamp>-<pid>-<kind>)
  --ping-count N       --loopback only: pings per bridge mode (default: 500)
  --ping-interval SEC  --loopback only: ping -i value (default: 0.01)
  --ping-size BYTES    --loopback only: ping -s value (default: 1400)
  --loopback           use the in-process fake modem instead of a real SIM/USB device
  --dry-run            print the plan and every command, run nothing
  -h, --help           show this help

Real mode runs, for each bridge mode: fm350mac status (before), then
fm350mac up --apn APN --io MODE --route-host SERVER_IP, the selected iperf3
tests, then stops the session, verifies teardown and takes fm350mac status
again. Before every iperf3 run the harness checks that the route to the
server really points at the tunnel's utun and that `up` is still alive,
and it records the bytes that went over the utun during each test.

Loopback mode instead pings the fake modem's peer (198.51.100.1) and
reports loss, average RTT and packets/s per bridge mode -- no iperf3 or
SIM needed.
EOF
}

log() {
    printf '[%s] %s\n' "$(date '+%H:%M:%S')" "$*" >&2
}

# Parse "5M" / "500K" / "1G" / "123" into an integer (decimal multiples).
# Prints the value; returns 1 if it isn't a non-negative integer.
parse_size() {
    local v="$1" n mult=1
    case "$v" in
        *[kK]) mult=1000; n="${v%?}" ;;
        *[mM]) mult=1000000; n="${v%?}" ;;
        *[gG]) mult=1000000000; n="${v%?}" ;;
        *) n="$v" ;;
    esac
    [[ "$n" =~ ^[0-9]{1,12}$ ]] || return 1
    echo $((10#$n * mult))
}

# -- argument parsing ---------------------------------------------------------
while [[ $# -gt 0 ]]; do
    case "$1" in
        --apn) APN="$2"; shift 2 ;;
        --server) SERVER="$2"; shift 2 ;;
        --port) PORT="$2"; shift 2 ;;
        --max-bytes) MAX_BYTES_ARG="$2"; shift 2 ;;
        --duration) DURATION="$2"; shift 2 ;;
        --udp-bitrate) UDP_BITRATE_ARG="$2"; shift 2 ;;
        --tests)
            IFS=',' read -r -a TESTS <<< "$2"
            shift 2
            ;;
        --modes)
            IFS=',' read -r -a MODES <<< "$2"
            shift 2
            ;;
        --yes) YES=1; shift ;;
        --full-tunnel) FULL_TUNNEL=1; shift ;;
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

if [[ "${#MODES[@]}" -eq 0 ]]; then
    echo "--modes must include sync or async" >&2
    exit 2
fi

for mode in "${MODES[@]}"; do
    if [[ "$mode" != "sync" && "$mode" != "async" ]]; then
        echo "unknown --io mode: $mode (expected sync or async)" >&2
        exit 2
    fi
done

if [[ "${#TESTS[@]}" -eq 0 ]]; then
    echo "--tests must include down, up or udp" >&2
    exit 2
fi

for t in "${TESTS[@]}"; do
    case "$t" in
        down|up|udp) ;;
        *)
            echo "unknown test: $t (expected down, up or udp)" >&2
            exit 2
            ;;
    esac
done

if ! MAX_BYTES="$(parse_size "$MAX_BYTES_ARG")"; then
    echo "invalid --max-bytes: $MAX_BYTES_ARG (expected an integer with optional K/M suffix)" >&2
    exit 2
fi

if [[ ! "$DURATION" =~ ^[0-9]+$ ]] || (( 10#$DURATION < 1 )); then
    echo "invalid --duration: $DURATION (expected an integer >= 1)" >&2
    exit 2
fi
DURATION=$((10#$DURATION))

if ! UDP_BITRATE="$(parse_size "$UDP_BITRATE_ARG")" || (( UDP_BITRATE < 1 )); then
    echo "invalid --udp-bitrate: $UDP_BITRATE_ARG (expected a rate > 0, e.g. 5M; unlimited UDP is refused)" >&2
    exit 2
fi

# UDP volume: iperf3 ignores `-n` for UDP until the end of the 1 s interval
# (3.21: `-b 100M -n 5000000` sent 12.5 MB), so the cap can only come from
# the rate and `-t`. Clamp the rate so one second stays within MAX_BYTES, and
# round the run time DOWN so rate * time never exceeds it. Without a volume
# cap (--max-bytes 0) --duration is used instead.
if (( MAX_BYTES > 0 )); then
    if (( UDP_BITRATE > MAX_BYTES * 8 )); then
        echo "WARNING: --udp-bitrate $UDP_BITRATE_ARG would exceed --max-bytes in one second; using $((MAX_BYTES * 8)) bit/s" >&2
        UDP_BITRATE=$((MAX_BYTES * 8))
        UDP_BITRATE_ARG="$UDP_BITRATE"
    fi
    UDP_TIME=$(( MAX_BYTES * 8 / UDP_BITRATE ))
    (( UDP_TIME >= 1 )) || UDP_TIME=1
else
    UDP_TIME="$DURATION"
fi

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
    RESULTS_DIR="$SCRIPT_DIR/bench-results/$(date '+%Y%m%d-%H%M%S')-$$-$kind"
fi

if [[ "${BENCH_THROUGHPUT_TEST:-0}" != 1 ]] && ! command -v iperf3 >/dev/null 2>&1 && [[ "$LOOPBACK" -ne 1 ]]; then
    echo "iperf3 not found: brew install iperf3" >&2
    [[ "$DRY_RUN" -eq 1 ]] || exit 1
fi

if [[ "${BENCH_THROUGHPUT_TEST:-0}" != 1 && "$DRY_RUN" -ne 1 && ! -x "$FM350MAC" ]]; then
    echo "fm350mac CLI not found at $FM350MAC (expected the fm350mac/.venv virtualenv)" >&2
    exit 1
fi

# Detected by actually invoking `status --help` (harmless, no root/AT/USB
# needed) -- skipped under --dry-run, which must not run fm350mac at all.
STATUS_JSON_SUPPORTED=0
if [[ "${BENCH_THROUGHPUT_TEST:-0}" != 1 && "$DRY_RUN" -ne 1 ]] && "$FM350MAC" status --help 2>&1 | grep -- '--json' >/dev/null; then
    STATUS_JSON_SUPPORTED=1
fi

# -- helpers ------------------------------------------------------------------

list_utuns() {
    ifconfig -l | tr ' ' '\n' | grep '^utun' || true
}

# Exact (whole-line) membership test: utun1 must not match utun10.
# $1 = name, $2 = newline-separated list.
in_list() {
    grep -Fx -- "$1" <<< "$2" >/dev/null
}

# Which interface would the kernel use to reach $1? Empty if there is no route.
route_iface_for() {
    route -n get "$1" 2>/dev/null | awk '$1 == "interface:" { print $2; exit }' || true
}

# Prints "<rx_bytes> <tx_bytes>" for interface $1 from `netstat -ibn`, or
# nothing. Reads the columns from the end of the row (Ibytes is NF-4, Obytes
# NF-1) because the Address column is empty on utun link rows.
utun_bytes() {
    netstat -ibn -I "$1" 2>/dev/null \
        | awk -v i="$1" '$1 == i && $3 ~ /^<Link/ { print $(NF-4), $(NF-1); exit }' || true
}

# Extract the interface name `fm350mac up` logged ("utun interface: utunN")
# from log file $1. Empty if not (yet) there.
utun_from_log() {
    [[ -f "$1" ]] || return 0
    sed -n 's/.*utun interface: \(utun[0-9][0-9]*\).*/\1/p' "$1" | head -n1 || true
}

# Wait for `fm350mac up` (pid $4, log $1) to bring its tunnel up, for up to
# $3 seconds. The interface name comes from the log line when present, else
# from the new-utun diff against list $2 (newline-separated). Success also
# requires the route to $5 to point at that interface. Prints the interface
# name, or nothing if `up` died or the timeout hit.
wait_for_up_iface() {
    local up_log="$1" before="$2" timeout_s="$3" pid="$4" route_host="$5"
    local waited=0
    while (( waited < timeout_s )); do
        if ! kill -0 "$pid" 2>/dev/null; then
            return 1
        fi
        local current iface new_ifs
        current="$(list_utuns)"
        iface="$(utun_from_log "$up_log")"
        if [[ -z "$iface" ]]; then
            new_ifs="$(comm -13 <(sort -u <<< "$before") <(sort -u <<< "$current") | awk 'NF')"
            iface="$(head -n1 <<< "$new_ifs")"
        fi
        if [[ -n "$iface" ]] && in_list "$iface" "$current" \
            && [[ "$(route_iface_for "$route_host")" == "$iface" ]]; then
            echo "$iface"
            return 0
        fi
        sleep 1
        waited=$((waited + 1))
    done
    return 1
}

# Globals used to hand the running `up` process and its utun name from
# start_up()/run_loopback_mode() to the cleanup trap and to verify_teardown().
UP_PID=""
UP_IFACE=""
CURRENT_MODE=""
BENCH_FAILED=0
BENCH_STARTED=0
DEFAULT_ROUTE_BEFORE=""
UTUNS_BEFORE=""

# Print a stable description of the current IPv4 default route. An empty
# signature means that no default route exists. Restricting the comparison to
# gateway and interface avoids false failures from volatile route metadata.
default_route_signature() {
    local route_info gateway iface
    route_info="$(route -n get default 2>/dev/null || true)"
    gateway="$(awk '$1 == "gateway:" { print $2; exit }' <<< "$route_info")"
    iface="$(awk '$1 == "interface:" { print $2; exit }' <<< "$route_info")"
    if [[ -n "$gateway" || -n "$iface" ]]; then
        printf 'gateway=%s;interface=%s' "$gateway" "$iface"
    fi
}

# stop_up PID [SIGNAL]. Background children of a non-interactive bash ignore
# SIGINT until they install their own handler, and fm350mac only does that
# once the session is fully up -- so during bring-up pass TERM.
stop_up() {
    local pid="$1" first_sig="${2:-INT}"
    if [[ -z "$pid" ]] || ! kill -0 "$pid" 2>/dev/null; then
        return 0
    fi
    log "stopping fm350mac up (pid $pid, SIG$first_sig)"
    kill "-$first_sig" "$pid" 2>/dev/null || true
    local waited=0
    while kill -0 "$pid" 2>/dev/null && (( waited < 10 )); do
        sleep 1
        waited=$((waited + 1))
    done
    if kill -0 "$pid" 2>/dev/null; then
        log "pid $pid still alive after SIG$first_sig, sending SIGTERM"
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
# owned is gone too. In real mode, also require the gateway/interface pair
# captured before startup so a missing (rather than merely non-utun) default
# route cannot be mistaken for successful cleanup. Logs a warning and returns
# 1 on any leftover; never raises (safe to call from the cleanup trap).
verify_teardown() {
    local iface="$1"
    local expected_default="${2-}"
    local before_utuns="${3-}"
    local before_utuns_supplied=0
    if [[ $# -ge 3 ]]; then
        before_utuns_supplied=1
    fi
    local ok=0
    local current_utuns
    current_utuns="$(list_utuns)"
    if [[ "$before_utuns_supplied" -eq 1 ]]; then
        local unexpected_utuns
        unexpected_utuns="$(comm -13 \
            <(printf '%s\n' "$before_utuns" | awk 'NF' | sort -u) \
            <(printf '%s\n' "$current_utuns" | awk 'NF' | sort -u))"
        if [[ -n "$unexpected_utuns" ]]; then
            log "WARNING: new tunnel interfaces are still present after teardown:" \
                "${unexpected_utuns//$'\n'/, }"
            ok=1
        fi
    elif [[ -n "$iface" ]] && in_list "$iface" "$current_utuns"; then
        log "WARNING: $iface is still present after teardown"
        ok=1
    fi
    if [[ "$LOOPBACK" -eq 1 ]]; then
        if netstat -rn -f inet | grep "^${LOOPBACK_PEER} " >/dev/null; then
            log "WARNING: loopback host route to $LOOPBACK_PEER is still present after teardown"
            ok=1
        fi
    else
        if [[ -n "$iface" ]] \
            && netstat -rn -f inet | awk -v i="$iface" '$1 == "default" && $NF == i { f = 1 } END { exit !f }'; then
            log "WARNING: default route still points at $iface after teardown"
            ok=1
        fi
        if [[ -n "$iface" && -n "$SERVER_IP" && "$(route_iface_for "$SERVER_IP")" == "$iface" ]]; then
            log "WARNING: host route to $SERVER_IP still points at $iface after teardown"
            ok=1
        fi
        local current_default
        current_default="$(default_route_signature)"
        if [[ "$current_default" != "$expected_default" ]]; then
            log "WARNING: default route was not restored after teardown" \
                "(expected ${expected_default:-<none>}, got ${current_default:-<none>})"
            ok=1
        fi
    fi
    return $ok
}

# Called before every iperf3 run: the run must go through the tunnel, never
# silently over some other interface (metered SIM!), and `up` must still be
# alive. Returns 1 (after logging) if either check fails.
verify_path() {
    local host="${SERVER_IP:-$SERVER}"
    if [[ -z "$UP_PID" ]] || ! kill -0 "$UP_PID" 2>/dev/null; then
        log "ERROR: fm350mac up (pid ${UP_PID:-?}) is not running; refusing to run iperf3"
        return 1
    fi
    local iface
    iface="$(route_iface_for "$host")"
    if [[ -z "$UP_IFACE" || "$iface" != "$UP_IFACE" ]]; then
        log "ERROR: route to $host uses interface '${iface:-<none>}', expected '${UP_IFACE:-<none>}'; refusing to run iperf3"
        return 1
    fi
    return 0
}

# shellcheck disable=SC2329 # invoked by the EXIT trap below
cleanup() {
    local rc=$?
    trap - EXIT INT TERM HUP
    if [[ -n "$UP_PID" ]] && kill -0 "$UP_PID" 2>/dev/null; then
        log "cleanup: fm350mac up is still running, stopping it"
        if [[ -n "$UP_IFACE" ]]; then
            stop_up "$UP_PID"
        else
            stop_up "$UP_PID" TERM
        fi
        UP_PID=""
        # The interrupted session's data use still counts against the SIM.
        if [[ "$LOOPBACK" -ne 1 && -n "$CURRENT_MODE" ]]; then
            record_sim_usage "$RESULTS_DIR/up-$CURRENT_MODE.log" "$RESULTS_DIR/sim-$CURRENT_MODE.txt"
        fi
    fi
    if [[ -n "$UP_IFACE" || "$BENCH_STARTED" -eq 1 ]]; then
        if ! verify_teardown "$UP_IFACE" "$DEFAULT_ROUTE_BEFORE" "$UTUNS_BEFORE"; then
            log "ERROR: teardown left a tunnel or route behind; inspect the Mac's routes"
            [[ "$rc" -ne 0 ]] || rc=1
        fi
    fi
    exit "$rc"
}
# Signals exit with the conventional 128+N status (a bare trap would leave $?
# at 0 for TERM/HUP, so a killed run looked successful); EXIT does the cleanup.
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

# Cumulative CPU time of $1 in seconds, from `ps -o time=` ([[dd-]hh:]mm:ss.xx).
cputime_seconds() {
    ps -p "$1" -o time= 2>/dev/null | awk '
        NF {
            n = $1; d = 0
            if (n ~ /-/) { split(n, a, "-"); d = a[1]; n = a[2] }
            k = split(n, p, ":"); s = 0
            for (i = 1; i <= k; i++) s = s * 60 + p[i]
            print s + d * 86400
            exit
        }' || true
}

# Append one "<epoch> <cpu-seconds>" sample for $1 to $2.
cpu_sample_once() {
    local pid="$1" outfile="$2" cpu
    cpu="$(cputime_seconds "$pid")"
    [[ -z "$cpu" ]] || printf '%s %s\n' "$(date +%s)" "$cpu" >> "$outfile"
}

# Sample the cputime of $pid once a second (max $3 seconds) into $2. Run in
# the background for the duration of one iperf3 test; the summary computes
# CPU % from the cputime delta over the wall-clock delta.
sample_cpu() {
    local pid="$1" outfile="$2" max_seconds="$3"
    : > "$outfile"
    local elapsed=0
    while kill -0 "$pid" 2>/dev/null && (( elapsed < max_seconds )); do
        cpu_sample_once "$pid" "$outfile"
        sleep 1
        elapsed=$((elapsed + 1))
    done
}

# Copy the driver's own end-of-session byte count ("stats: rx=N/BYTESB
# tx=N/BYTESB ...", logged by `fm350mac up` on shutdown) from up log $1 to $2.
# This is the authoritative SIM usage for the whole session (incl. bring-up
# and anything else routed via the tunnel); see the utun caveat below.
record_sim_usage() {
    local stats
    stats="$(sed -n 's/.*stats: rx=[0-9]*\/\([0-9]*\)B tx=[0-9]*\/\([0-9]*\)B.*/\1 \2/p' "$1" 2>/dev/null | tail -n1)" || true
    if [[ -z "$stats" ]]; then
        log "WARNING: no driver stats line in $(basename "$1"); SIM usage for this mode unknown"
        return 0
    fi
    echo "$stats" > "$2"
    log "SIM usage (driver count): rx $(( ${stats% *} / 1000 )) kB, tx $(( ${stats#* } / 1000 )) kB"
}

# Write the rx/tx byte delta between two utun_bytes outputs to $3.
# Caveat (seen on macOS 27, 2026-10-05): the utun's Ibytes counts every packet
# the driver writes into the tunnel twice -- a 1 MB download showed ~2.1 MB
# Ibytes while the driver counted 1.06 MB. The rx figure here is therefore an
# upper bound (about 2x); the driver count above is the real SIM usage.
record_utun_usage() {
    local before="$1" after="$2" outfile="$3"
    local b_rx="" b_tx="" a_rx="" a_tx=""
    read -r b_rx b_tx <<< "$before" || true
    read -r a_rx a_tx <<< "$after" || true
    if [[ -z "$b_rx" || -z "$b_tx" || -z "$a_rx" || -z "$a_tx" ]]; then
        log "WARNING: could not read utun byte counters; no usage recorded for $(basename "$outfile")"
        return 0
    fi
    local rx=$((a_rx - b_rx)) tx=$((a_tx - b_tx))
    echo "$rx $tx" > "$outfile"
    log "utun usage: rx $((rx / 1000)) kB, tx $((tx / 1000)) kB"
}

# run_iperf MODE DIRECTION PROTO OUT_JSON CPU_FILE UP_PID
# Returns 0 on success, 1 if iperf3 failed, 3 if the path check failed (the
# caller must then stop the whole run: nothing was sent).
run_iperf() {
    local mode="$1" direction="$2" proto="$3" out_json="$4" cpu_file="$5" up_pid="$6"
    if ! verify_path; then
        return 3
    fi
    # -i 0.1: a capped (-n) run ends at a reporting-interval boundary, so with
    # iperf3's default 1 s interval a 5 MB test is timed as a whole number of
    # seconds (seen 2026-10-05: every rate was 40.9/N Mbit/s). 0.1 s intervals
    # make that rounding 10x smaller.
    local -a args=(-c "${SERVER_IP:-$SERVER}" -p "$PORT" -i 0.1 --json)
    if [[ "$proto" == "udp" ]]; then
        args+=(-u -b "$UDP_BITRATE_ARG" -t "$UDP_TIME")
    elif (( MAX_BYTES > 0 )); then
        args+=(-n "$MAX_BYTES")
    else
        args+=(-t "$DURATION")
    fi
    [[ "$direction" == "download" ]] && args+=(-R)
    log "iperf3 ${args[*]} (mode=$mode direction=$direction proto=$proto)"
    local usage_before usage_after
    usage_before="$(utun_bytes "$UP_IFACE")"
    sample_cpu "$up_pid" "$cpu_file" 3600 &
    local sampler_pid=$!
    local failed=0
    if ! iperf3 "${args[@]}" > "$out_json" 2> "${out_json}.stderr" < /dev/null; then
        log "ERROR: iperf3 $direction/$proto failed for mode=$mode; see ${out_json}.stderr"
        failed=1
    fi
    kill "$sampler_pid" 2>/dev/null || true
    wait "$sampler_pid" 2>/dev/null || true
    cpu_sample_once "$up_pid" "$cpu_file"
    usage_after="$(utun_bytes "$UP_IFACE")"
    record_utun_usage "$usage_before" "$usage_after" "${out_json%.json}.utun"
    return "$failed"
}

# "<direction>:<proto>" pairs for the selected --tests, one per line.
test_specs() {
    local t
    for t in "${TESTS[@]}"; do
        case "$t" in
            down) echo "download:tcp" ;;
            up) echo "upload:tcp" ;;
            udp) echo "download:udp"; echo "upload:udp" ;;
        esac
    done
}

# Worst-case data use of the whole run in bytes: payload plus ~10% for
# headers/handshakes. Prints "unbounded" when TCP runs have no volume cap.
estimate_bytes() {
    local payload=0 spec proto
    while read -r spec; do
        proto="${spec#*:}"
        if [[ "$proto" == "udp" ]]; then
            payload=$((payload + (UDP_BITRATE / 8) * UDP_TIME))
        elif (( MAX_BYTES > 0 )); then
            payload=$((payload + MAX_BYTES))
        else
            echo unbounded
            return 0
        fi
    done < <(test_specs)
    echo $((payload * ${#MODES[@]} * 11 / 10))
}

describe_estimate() {
    local est
    est="$(estimate_bytes)"
    if [[ "$est" == "unbounded" ]]; then
        echo "no volume cap (--max-bytes 0): each TCP run lasts --duration (${DURATION}s) at full link speed, so data use is unbounded"
    else
        echo "worst case ~$(( (est + 999999) / 1000000 )) MB (payload plus ~10% overhead; ${#MODES[@]} mode(s))"
    fi
}

# Print the pre-flight estimate; above the threshold (or unbounded) require
# --yes or an interactive "yes". Returns 1 if the run must not proceed.
confirm_budget() {
    local est
    est="$(estimate_bytes)"
    log "pre-flight data estimate: $(describe_estimate)"
    if [[ "$est" != "unbounded" ]] && (( est <= CONFIRM_THRESHOLD_BYTES )); then
        return 0
    fi
    if [[ "$YES" -eq 1 ]]; then
        return 0
    fi
    if [[ -t 0 ]]; then
        local answer=""
        read -r -p "This run may use more than 50 MB of the SIM's data. Type 'yes' to continue: " answer || true
        [[ "$answer" == "yes" ]] && return 0
        log "aborted"
        return 1
    fi
    log "ERROR: estimated data use is above 50 MB (or unbounded); pass --yes to proceed"
    return 1
}

capture_status() {
    local out="$1"
    if [[ "$STATUS_JSON_SUPPORTED" -eq 1 ]]; then
        "$FM350MAC" status --json > "$out" 2>&1 || true
    fi
}

start_up_real() {
    local mode="$1"
    DEFAULT_ROUTE_BEFORE="$(default_route_signature)"
    UTUNS_BEFORE="$(list_utuns)"
    local -a cmd=("$FM350MAC" up --apn "$APN" --io "$mode")
    if [[ "$FULL_TUNNEL" -eq 1 ]]; then
        cmd+=(--default-route)
    else
        cmd+=(--route-host "$SERVER_IP")
    fi
    local up_log="$RESULTS_DIR/up-$mode.log"
    log "starting: ${cmd[*]}"
    BENCH_STARTED=1
    CURRENT_MODE="$mode"
    "${cmd[@]}" > "$up_log" 2>&1 &
    UP_PID=$!
    UP_IFACE="$(wait_for_up_iface "$up_log" "$UTUNS_BEFORE" "$UP_WAIT_TIMEOUT" "$UP_PID" "$SERVER_IP")" || true
    if [[ -z "$UP_IFACE" ]]; then
        if kill -0 "$UP_PID" 2>/dev/null; then
            log "ERROR: tunnel and route to $SERVER_IP did not appear within ${UP_WAIT_TIMEOUT}s for mode=$mode"
        else
            log "ERROR: fm350mac up exited during bring-up for mode=$mode; see $up_log"
        fi
        stop_up "$UP_PID" TERM
        UP_PID=""
        return 1
    fi
    log "utun $UP_IFACE is up, route to $SERVER_IP verified (mode=$mode)"
    return 0
}

run_real_mode() {
    local path_abort=0
    for mode in "${MODES[@]}"; do
        # Status talks to the modem's AT port, which `up` holds: only outside `up`.
        capture_status "$RESULTS_DIR/status-before-$mode.json"

        if ! start_up_real "$mode"; then
            BENCH_FAILED=1
            if ! verify_teardown "$UP_IFACE" "$DEFAULT_ROUTE_BEFORE" "$UTUNS_BEFORE"; then
                log "ERROR: failed startup also failed teardown verification; stopping before another mode"
                break
            fi
            BENCH_STARTED=0
            DEFAULT_ROUTE_BEFORE=""
            UTUNS_BEFORE=""
            UP_IFACE=""
            continue
        fi

        local spec direction proto rc
        for spec in $(test_specs); do
            direction="${spec%%:*}"
            proto="${spec##*:}"
            rc=0
            run_iperf "$mode" "$direction" "$proto" \
                "$RESULTS_DIR/$mode-$direction-$proto.json" "$RESULTS_DIR/$mode-$direction-$proto.cpu" \
                "$UP_PID" || rc=$?
            if [[ "$rc" -eq 3 ]]; then
                BENCH_FAILED=1
                path_abort=1
                break
            elif [[ "$rc" -ne 0 ]]; then
                BENCH_FAILED=1
            fi
        done

        stop_up "$UP_PID"
        UP_PID=""
        record_sim_usage "$RESULTS_DIR/up-$mode.log" "$RESULTS_DIR/sim-$mode.txt"
        CURRENT_MODE=""
        local teardown_ok=1
        verify_teardown "$UP_IFACE" "$DEFAULT_ROUTE_BEFORE" "$UTUNS_BEFORE" || teardown_ok=0
        capture_status "$RESULTS_DIR/status-after-$mode.json"
        if [[ "$teardown_ok" -eq 0 ]]; then
            log "ERROR: teardown check failed for mode=$mode; stopping before another mode"
            BENCH_FAILED=1
            break
        fi
        UP_IFACE=""
        BENCH_STARTED=0
        DEFAULT_ROUTE_BEFORE=""
        UTUNS_BEFORE=""
        if [[ "$path_abort" -eq 1 ]]; then
            log "ERROR: path check failed; aborting the remaining modes"
            break
        fi
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
        UTUNS_BEFORE="$(list_utuns)"
        local -a cmd=("$FM350MAC" up --apn "$APN" --loopback --io "$mode")
        local up_log="$RESULTS_DIR/up-loopback-$mode.log"
        log "starting: ${cmd[*]}"
        BENCH_STARTED=1
        "${cmd[@]}" > "$up_log" 2>&1 &
        UP_PID=$!
        UP_IFACE="$(wait_for_up_iface "$up_log" "$UTUNS_BEFORE" "$UP_WAIT_TIMEOUT" "$UP_PID" "$LOOPBACK_PEER")" || true
        if [[ -z "$UP_IFACE" ]]; then
            log "ERROR: utun/route did not appear within ${UP_WAIT_TIMEOUT}s (or up exited) for mode=$mode"
            stop_up "$UP_PID" TERM
            UP_PID=""
            BENCH_FAILED=1
            if ! verify_teardown "" "" "$UTUNS_BEFORE"; then
                log "ERROR: failed startup also failed teardown verification; stopping before another mode"
                break
            fi
            BENCH_STARTED=0
            UTUNS_BEFORE=""
            continue
        fi
        log "utun $UP_IFACE is up (loopback, mode=$mode)"

        local ping_log="$RESULTS_DIR/loopback-$mode-ping.log"
        local -a ping_cmd=(ping -i "$PING_INTERVAL" -s "$PING_SIZE" -c "$PING_COUNT" "$LOOPBACK_PEER")
        log "${ping_cmd[*]}"
        local t0 t1
        t0="$(date +%s.%N)"
        if ! "${ping_cmd[@]}" > "$ping_log" 2>&1; then
            log "ERROR: ping failed for mode=$mode; see $ping_log"
            BENCH_FAILED=1
        fi
        t1="$(date +%s.%N)"

        stop_up "$UP_PID"
        UP_PID=""
        if ! verify_teardown "$UP_IFACE" "" "$UTUNS_BEFORE"; then
            log "ERROR: teardown check failed for mode=$mode; stopping before another mode"
            BENCH_FAILED=1
            break
        fi
        UP_IFACE=""
        BENCH_STARTED=0
        UTUNS_BEFORE=""

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
        # ${a[@]+...}: an empty array is "unbound" under set -u in bash < 4.4.
        printf '%s\n' ${summary_rows[@]+"${summary_rows[@]}"}
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
            echo "  wait for the utun (from the 'utun interface:' log line) and the route to $LOOPBACK_PEER (up to ${UP_WAIT_TIMEOUT}s)"
            echo "  ping -i $PING_INTERVAL -s $PING_SIZE -c $PING_COUNT $LOOPBACK_PEER"
            echo "  kill -INT <up pid>; verify utun and the $LOOPBACK_PEER host route are gone"
            echo
        done
        echo "Then: write $RESULTS_DIR/loopback-summary.md (loss/RTT/packets-per-second per mode)."
    else
        local server_ip="${SERVER_IP:-$SERVER}"
        local route_arg="--route-host $server_ip"
        if [[ "$FULL_TUNNEL" -eq 1 ]]; then
            route_arg="--default-route"
            echo "WARNING: --full-tunnel moves the Mac's whole default route onto the SIM for the" \
                 "duration of the run; every other app's traffic is billed to it too."
            echo
        fi
        echo "Pre-flight: $(describe_estimate)"
        if [[ "$(estimate_bytes)" == "unbounded" ]] || (( $(estimate_bytes) > CONFIRM_THRESHOLD_BYTES )); then
            echo "  above 50 MB: a real run needs --yes (or an interactive 'yes')"
        fi
        echo
        for mode in "${MODES[@]}"; do
            echo "mode=$mode:"
            echo "  $FM350MAC status --json > status-before-$mode.json   # if --json is supported; before 'up'"
            echo "  $FM350MAC up --apn $APN --io $mode $route_arg"
            echo "  wait for the utun (from the 'utun interface:' log line) and the route to $server_ip (up to ${UP_WAIT_TIMEOUT}s)"
            local spec direction proto
            for spec in $(test_specs); do
                direction="${spec%%:*}"
                proto="${spec##*:}"
                local flags="" cap=""
                [[ "$direction" == "download" ]] && flags=" -R"
                if [[ "$proto" == "udp" ]]; then
                    cap="-u -b $UDP_BITRATE_ARG -t $UDP_TIME"
                elif (( MAX_BYTES > 0 )); then
                    cap="-n $MAX_BYTES"
                else
                    cap="-t $DURATION"
                fi
                echo "  # before: route -n get $server_ip must show the tunnel's utun, up must be alive"
                echo "  iperf3 -c $server_ip -p $PORT -i 0.1 $cap --json$flags   # $proto $direction"
            done
            echo "  kill -INT <up pid>; verify utun and the default route are restored"
            echo "  $FM350MAC status --json > status-after-$mode.json   # if --json is supported; after teardown"
            echo
        done
        echo "Then: python3 $SUMMARIZE $RESULTS_DIR > $RESULTS_DIR/SUMMARY.md"
    fi
}

# Dotted-quad IPv4 for $1 (a literal passes through; a hostname is resolved).
resolve_ipv4() {
    local h="$1"
    if [[ "$h" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
        echo "$h"
        return 0
    fi
    dscacheutil -q host -a name "$h" 2>/dev/null | awk '$1 == "ip_address:" && $2 ~ /^[0-9.]+$/ { print $2; exit }' || true
}

# -- main ----------------------------------------------------------------------

# Source-only hook for deterministic function tests; normal invocations still
# run the command below. The caller passes --loopback --dry-run for parsing.
if [[ "${BENCH_THROUGHPUT_TEST:-0}" == 1 ]]; then
    return 0
fi

if [[ "$LOOPBACK" -ne 1 ]]; then
    if [[ "$DRY_RUN" -eq 1 ]]; then
        SERVER_IP="$(resolve_ipv4 "$SERVER")"
        [[ -n "$SERVER_IP" ]] || SERVER_IP="<IPv4 of $SERVER>"
    else
        SERVER_IP="$(resolve_ipv4 "$SERVER")"
        if [[ -z "$SERVER_IP" ]]; then
            echo "could not resolve $SERVER to an IPv4 address (the host route needs one)" >&2
            exit 2
        fi
    fi
fi

if [[ "$DRY_RUN" -eq 1 ]]; then
    print_plan
    exit 0
fi

if [[ "$LOOPBACK" -ne 1 ]]; then
    if [[ "$FULL_TUNNEL" -eq 1 ]]; then
        log "WARNING: --full-tunnel: the Mac's DEFAULT route moves onto the SIM for this run." \
            "ALL traffic (updates, sync, backups) is billed to it. Use only on an unmetered SIM."
    fi
    confirm_budget || exit 2
fi

mkdir -p "$RESULTS_DIR"

if [[ "$LOOPBACK" -eq 1 ]]; then
    run_loopback_mode
else
    run_real_mode
fi
exit "$BENCH_FAILED"
