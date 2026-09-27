#!/usr/bin/env bash
# Deterministic benchmark failure-path checks; no modem, ping, or route writes.
# shellcheck disable=SC2034,SC1091,SC2329
# Variables/functions are consumed by the dynamically sourced benchmark.
set -euo pipefail

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BENCH="$TEST_DIR/../bench-throughput.sh"
test_tmp="$(mktemp -d "${TMPDIR:-/tmp}/fm350-bench-test.XXXXXX")"
BENCH_THROUGHPUT_TEST=1
set -- --loopback --dry-run --results-dir "$test_tmp"
# shellcheck source=../bench-throughput.sh
. "$BENCH"
trap 'rm -rf "$test_tmp"' EXIT INT TERM

fail() { echo "FAIL: $*" >&2; exit 1; }
assert_eq() { [[ "$1" == "$2" ]] || fail "expected '$2', got '$1' ($3)"; }

# The real run_iperf must propagate iperf3's status after collecting samples.
SERVER=example.invalid
DURATION=0
sample_cpu() { :; }
iperf3() { echo 'simulated iperf failure' >&2; return 7; }
if run_iperf sync download tcp "$test_tmp/failed.json" "$test_tmp/failed.cpu" 0; then
    fail 'run_iperf accepted a failed transfer'
fi
[[ -s "$test_tmp/failed.json.stderr" ]] || fail 'iperf stderr was lost'

# Real-mode cleanup must restore the same gateway/interface pair, not merely
# remove the utun route and leave the Mac without a default route.
LOOPBACK=0
mock_current_utuns=""
list_utuns() { printf '%s' "$mock_current_utuns"; }
netstat() { printf 'default            192.0.2.1          UGScg                 en0\n'; }
default_route_signature() { printf 'gateway=192.0.2.1;interface=en0'; }
verify_teardown utun1 'gateway=192.0.2.1;interface=en0' '' || fail 'restored default route was rejected'
if verify_teardown utun1 'gateway=192.0.2.254;interface=en0' ''; then
    fail 'missing original default route was accepted'
fi
mock_current_utuns=utun9
if verify_teardown '' 'gateway=192.0.2.1;interface=en0' ''; then
    fail 'unidentified utun left by a startup timeout was accepted'
fi
mock_current_utuns=""

# A mode that never starts verifies route cleanup before moving on and still
# leaves a summary, with a failing result.
MODES=(sync)
BENCH_FAILED=0
verify_calls=0
start_up_real() { BENCH_STARTED=1; DEFAULT_ROUTE_BEFORE='baseline'; UTUNS_BEFORE='utun0'; return 1; }
verify_teardown() {
    verify_calls=$((verify_calls + 1))
    [[ "${3-}" == utun0 ]] || return 1
}
python3() { echo '| no results |'; }
run_real_mode
assert_eq "$BENCH_FAILED" 1 'failed startup'
assert_eq "$verify_calls" 1 'failed startup teardown verification'
assert_eq "$BENCH_STARTED" 0 'failed startup state reset'
[[ -f "$test_tmp/SUMMARY.md" ]] || fail 'startup failure lost the summary'

# A failed transfer is reflected in the overall result even if other tests
# and teardown can continue.
BENCH_FAILED=0
start_up_real() { BENCH_STARTED=1; DEFAULT_ROUTE_BEFORE='baseline'; UP_PID=""; UP_IFACE=utun1; return 0; }
run_iperf() { return 1; }
stop_up() { :; }
verify_teardown() { return 0; }
run_real_mode
assert_eq "$BENCH_FAILED" 1 'failed transfer'

# A failing ping is recorded; a failed teardown stops before the next mode.
FM350MAC=/usr/bin/true
MODES=(sync)
BENCH_FAILED=0
BENCH_STARTED=0
list_utuns() { :; }
wait_for_new_utun() { echo utun1; }
verify_teardown() { return 0; }
ping() { printf '1 packets transmitted, 0 packets received, 100.0%% packet loss\n'; return 1; }
run_loopback_mode
assert_eq "$BENCH_FAILED" 1 'failed ping'
[[ -f "$test_tmp/loopback-summary.md" ]] || fail 'ping failure lost the summary'

MODES=(sync async)
BENCH_FAILED=0
# Command substitution runs in a subshell, so use a file to count starts.
wait_for_new_utun() { echo x >> "$test_tmp/starts"; echo utun1; }
ping() { printf '1 packets transmitted, 1 packets received, 0.0%% packet loss\nround-trip min/avg/max/stddev = 1.0/1.0/1.0/0.0 ms\n'; }
verify_teardown() { return 1; }
run_loopback_mode
assert_eq "$BENCH_FAILED" 1 'failed teardown'
assert_eq "$(wc -l < "$test_tmp/starts" | tr -d ' ')" 1 'stopped before second mode'
assert_eq "$UP_IFACE" utun1 'leftover interface retained for EXIT check'

# The EXIT cleanup must upgrade an otherwise successful status.
UP_PID=""
if ( trap - EXIT INT TERM; cleanup ); then
    fail 'cleanup accepted a leftover interface or route'
fi

echo 'bench-throughput-test.sh: PASS'
