#!/bin/sh
# watchdog-test.sh - unit-tests fm350-watchdog's decision logic (tick(), in
# files/usr/sbin/fm350-watchdog) against fake ifstatus/ifup/ifdown/logger and
# a fake clock (now()), without procd, netifd or a real modem. Real `uci`
# and `jsonfilter` are used (both ship in the base OpenWrt rootfs), so
# should_manage()/interface_up() run unmodified.
#
# Runs on the development host (needs Docker, for a real uci/jsonfilter),
# not on the router. POSIX sh.
#
# Scenarios (see tests/watchdog-harness.sh.tmpl below, generated into
# WORK_TMP): healthy up -> no action; pending < threshold -> no action;
# pending > threshold -> restart; a second restart is withheld until the
# backoff elapses, and the backoff itself doubles; a disabled interface
# (network.wwan.disabled=1) and a paused watchdog
# (/tmp/fm350-watchdog.pause) both -> no action even once well past the
# pending threshold.
set -e

SCRIPT_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
OPENWRT_DIR=$(CDPATH='' cd -- "$SCRIPT_DIR/.." && pwd)
WATCHDOG_SH="$OPENWRT_DIR/files/usr/sbin/fm350-watchdog"
INITD_SH="$OPENWRT_DIR/files/etc/init.d/fm350-watchdog"

IMAGE=${DOCKER_TEST_IMAGE:-openwrt/rootfs:armsr-armv8-openwrt-24.10}
WORK_TMP=$(mktemp -d "${TMPDIR:-/tmp}/watchdog-test.XXXXXX")

status=0
fail() {
	echo "FAIL: $*" >&2
	status=1
}

CONTAINER=""
# shellcheck disable=SC2329 # invoked indirectly via the trap below
cleanup() {
	[ -n "$CONTAINER" ] && docker rm -f "$CONTAINER" >/dev/null 2>&1
	rm -rf "$WORK_TMP"
}
trap cleanup EXIT INT TERM

command -v docker >/dev/null 2>&1 || {
	echo "watchdog-test.sh: docker not found in PATH" >&2
	exit 1
}

echo "watchdog-test.sh: shellcheck"
if command -v shellcheck >/dev/null 2>&1; then
	shellcheck -s sh "$WATCHDOG_SH" "$INITD_SH" "$SCRIPT_DIR/watchdog-test.sh" ||
		fail "shellcheck reported issues"
else
	echo "watchdog-test.sh: shellcheck not installed, skipping static check" >&2
fi

# --- the harness that actually drives tick() --------------------------------
# Written to WORK_TMP (not checked in) so it can be shellchecked too.
cat >"$WORK_TMP/harness.sh" <<'HARNESS'
#!/bin/sh
# shellcheck disable=SC2034,SC2154,SC2329
# SC2034/SC2154 (assigned/referenced but not [visibly] assigned): tick() and
# load_config(), sourced from fm350-watchdog below, both read and write
# pending_since/up_since/backoff/next_allowed_restart/pending_threshold/
# enabled/interface/etc - shellcheck's single-file analysis can't see that
# (source=/dev/null, deliberately: fm350-watchdog only exists in the
# container this runs in, not on the host doing the shellcheck pass).
# SC2329 (function never invoked): ifup/ifdown/ifstatus/logger/now are all
# invoked indirectly, by tick() (again, in the sourced file).
#
# Sourced-and-driven test harness for fm350-watchdog's tick(). Stubs
# ifstatus/ifup/ifdown/logger (counted/logged) and now() (a fake clock);
# uci and jsonfilter are the real binaries. Not meant to be run directly.
set -e
FM350_WATCHDOG_TEST=1
# shellcheck source=/dev/null
. /root/fm350-watchdog

# The stubs below must be defined *after* sourcing fm350-watchdog: ash keeps
# only the last definition of a given function name, and fm350-watchdog
# defines its own ifup/ifdown/ifstatus/logger/now (thin wrappers around the
# real commands) at source time, which would otherwise clobber these.
ifup_calls=0
ifdown_calls=0
ifup() { ifup_calls=$((ifup_calls + 1)); }
ifdown() { ifdown_calls=$((ifdown_calls + 1)); }
logger() { :; } # discard; restart_interface()'s log() call still runs fine

STUB_UP=false
ifstatus() { printf '{"up":%s,"pending":true}' "$STUB_UP"; }

STUB_NOW=1000000
now() { echo "$STUB_NOW"; }

STUB_UPTIME=99999 # well past boot_grace unless a scenario says otherwise
uptime_s() { echo "$STUB_UPTIME"; }

FAILED=0
fail() {
	echo "FAIL: $*"
	FAILED=1
}
assert_eq() {
	# $1 description  $2 actual  $3 expected
	[ "$2" = "$3" ] || fail "$1 (expected '$3', got '$2')"
}

reset_state() {
	load_config
	pending_since=0
	up_since=0
	backoff=0
	next_allowed_restart=0
	ifup_calls=0
	ifdown_calls=0
}

uci -q delete network.wwan >/dev/null 2>&1 || true
uci commit network >/dev/null 2>&1 || true
rm -f /tmp/fm350-watchdog.pause

echo "=== healthy up: no action ==="
reset_state
STUB_UP=true
STUB_NOW=1000000
tick
assert_eq "healthy up: ifup calls" "$ifup_calls" 0
assert_eq "healthy up: ifdown calls" "$ifdown_calls" 0

echo "=== pending < threshold: no action ==="
reset_state
STUB_UP=false
STUB_NOW=1000000
tick # first non-up observation: records pending_since
STUB_NOW=$((1000000 + 100)) # 100s < pending_threshold (180)
tick
assert_eq "pending<threshold: ifup calls" "$ifup_calls" 0

echo "=== pending > threshold: restart ==="
reset_state
STUB_UP=false
STUB_NOW=1000000
tick
STUB_NOW=$((1000000 + 200)) # 200s > pending_threshold (180)
tick
assert_eq "pending>threshold: ifup calls" "$ifup_calls" 1
assert_eq "pending>threshold: ifdown calls" "$ifdown_calls" 1
assert_eq "pending>threshold: backoff seeded to pending_threshold" "$backoff" "$pending_threshold"

echo "=== backoff withholds a second restart, then doubles ==="
# Continues from the previous scenario's state: still pending, backoff=180,
# next_allowed_restart = 1000200 + 180 = 1000380.
STUB_NOW=$((1000200 + 100)) # before next_allowed_restart
tick
assert_eq "backoff: no restart before backoff elapses" "$ifup_calls" 1
STUB_NOW=$((1000200 + 181)) # after next_allowed_restart
tick
assert_eq "backoff: second restart once backoff elapses" "$ifup_calls" 2
assert_eq "backoff: doubled" "$backoff" $((pending_threshold * 2))

echo "=== disabled interface: no action ==="
reset_state
uci set network.wwan=interface
uci set network.wwan.disabled=1
uci commit network
STUB_UP=false
STUB_NOW=1000000
tick
STUB_NOW=$((1000000 + 300)) # well past pending_threshold
tick
assert_eq "disabled: ifup calls" "$ifup_calls" 0
uci -q delete network.wwan
uci commit network

echo "=== paused (pause file): no action ==="
reset_state
touch /tmp/fm350-watchdog.pause
STUB_UP=false
STUB_NOW=1000000
tick
STUB_NOW=$((1000000 + 300)) # well past pending_threshold
tick
assert_eq "paused: ifup calls" "$ifup_calls" 0
rm -f /tmp/fm350-watchdog.pause

echo "=== boot grace: no restart shortly after boot ==="
reset_state
STUB_UP=false
STUB_UPTIME=120 # < boot_grace (300)
STUB_NOW=1000000
tick
STUB_NOW=$((1000000 + 200)) # past pending_threshold, but still in boot grace
tick
assert_eq "boot grace: ifup calls" "$ifup_calls" 0
STUB_UPTIME=400
STUB_NOW=$((1000000 + 230))
tick
assert_eq "boot grace over: restart" "$ifup_calls" 1
STUB_UPTIME=99999

echo "=== non-numeric config falls back to defaults ==="
[ -f /etc/config/fm350_watchdog ] || : >/etc/config/fm350_watchdog
uci set fm350_watchdog.main=fm350_watchdog
uci set fm350_watchdog.main.pending_threshold='180s'
uci set fm350_watchdog.main.backoff_max='abc'
uci commit fm350_watchdog
reset_state
assert_eq "non-numeric pending_threshold -> default" "$pending_threshold" 180
assert_eq "non-numeric backoff_max -> default" "$backoff_max" 1800
STUB_UP=false
STUB_NOW=1000000
tick
STUB_NOW=$((1000000 + 200))
tick
assert_eq "non-numeric config: still restarts" "$ifup_calls" 1
uci -q delete fm350_watchdog.main
uci commit fm350_watchdog

if [ "$FAILED" -eq 0 ]; then
	echo "watchdog-harness: PASS"
else
	echo "watchdog-harness: FAIL"
fi
exit "$FAILED"
HARNESS

if command -v shellcheck >/dev/null 2>&1; then
	shellcheck -s sh "$WORK_TMP/harness.sh" || fail "shellcheck reported issues (harness.sh)"
fi

echo "watchdog-test.sh: pulling $IMAGE"
if ! docker pull "$IMAGE" >/dev/null; then
	echo "watchdog-test.sh: could not pull $IMAGE, no Docker validation possible" >&2
	exit 1
fi

dexec() {
	docker exec "$CONTAINER" /bin/sh -c "$1"
}

CONTAINER="5g-failover-watchdog-test-$$"
echo "watchdog-test.sh: starting container $CONTAINER"
docker run -d --name "$CONTAINER" "$IMAGE" /bin/sh -c "sleep 3600" >/dev/null

dexec "mkdir -p /etc/config"
dexec "[ -f /etc/config/network ] || : > /etc/config/network"
docker cp "$WATCHDOG_SH" "$CONTAINER:/root/fm350-watchdog" >/dev/null
docker cp "$WORK_TMP/harness.sh" "$CONTAINER:/root/harness.sh" >/dev/null
dexec "chmod +x /root/fm350-watchdog /root/harness.sh"

echo "watchdog-test.sh: running harness.sh in the container"
dexec "/root/harness.sh" >"$WORK_TMP/harness-output.log" 2>&1 ||
	fail "harness.sh exited non-zero (see $WORK_TMP/harness-output.log)"
cat "$WORK_TMP/harness-output.log"
grep -q "watchdog-harness: PASS" "$WORK_TMP/harness-output.log" ||
	fail "harness.sh did not report PASS"

docker rm -f "$CONTAINER" >/dev/null 2>&1
CONTAINER=""

if [ "$status" -eq 0 ]; then
	echo "watchdog-test.sh: PASS (image: $IMAGE)"
else
	echo "watchdog-test.sh: FAIL, see output above" >&2
fi

exit "$status"
