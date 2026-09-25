#!/bin/sh
# driver.sh - runs one atc.sh scenario against fake_fm350.py inside the test
# container. Not meant to be run directly; copied in and invoked (per
# scenario) by ../atc-test.sh via `docker exec`.
#
# Usage: driver.sh SCENARIO OUTDIR TIMEOUT_SECONDS [BOOT_SILENCE_SECONDS]
#
# Writes, under OUTDIR:
#   ttyUSB-fake   - symlink to the fake modem's pty slave (also the
#                   network.wwan.device value applied for this run)
#   transcript.log - every AT command line fake_fm350.py received, in order
#   notify.log    - one json line per proto_send_update/proto_notify_error/
#                   proto_block_restart/proto_set_available call (see
#                   run_setup.sh's _proto_notify override)
#   setup.log     - stdout+stderr of run_setup.sh (includes atc.sh's own
#                   `echo` progress lines: "SIMcard ready", "Registered to
#                   ... on LTE", "Activate session", etc.)
#   fake.log      - fake_fm350.py's own stderr trace
#   setup.exit    - run_setup.sh's exit status (only meaningful for
#                   scenarios where proto_atc_setup returns on its own -
#                   "ok"/"slow_boot" never return on success and are always
#                   killed by the watchdog below)
set -e

scenario=$1
outdir=$2
timeout_s=$3
boot_silence=${4:-0}

mkdir -p "$outdir"
device_link="$outdir/ttyUSB-fake"
transcript="$outdir/transcript.log"
notify="$outdir/notify.log"
setup_log="$outdir/setup.log"
fake_log="$outdir/fake.log"

rm -f "$device_link" "$transcript" "$notify" "$setup_log" "$fake_log" "$outdir/setup.exit"
: >"$transcript"
: >"$notify"

python3 /root/atc-sim/fake_fm350.py \
	--scenario "$scenario" \
	--link "$device_link" \
	--transcript "$transcript" \
	--apn internet.telekom \
	--pdp IP \
	--boot-silence "$boot_silence" \
	>"$fake_log" 2>&1 &
fake_pid=$!

i=0
while [ ! -e "$device_link" ]; do
	i=$((i + 1))
	if [ "$i" -gt 10 ]; then
		echo "driver.sh: fake modem did not create $device_link within 10s" >&2
		kill -9 "$fake_pid" 2>/dev/null || true
		exit 1
	fi
	# busybox sleep has no fractional-second support; the pty/symlink appear
	# within milliseconds in practice, this loop is just a generous ceiling.
	sleep 1
done

# Apply network.wwan the same way install.sh's apply_uci_template does, from
# the real uci/network-atc.uci template (read-only here, never modified):
# strip comments/blank lines, substitute @DEVICE@/@APN@, pipe to `uci batch`.
rendered=$(grep -v '^[[:space:]]*#' /root/openwrt/uci/network-atc.uci | grep -v '^[[:space:]]*$')
rendered=$(echo "$rendered" | sed "s|@DEVICE@|$device_link|g")
rendered=$(echo "$rendered" | sed "s|@APN@|internet.telekom|g")
echo "$rendered" | uci -q batch

# atc.sh unconditionally sleeps the full `delay` config value (15s, see
# uci/network-atc.uci) before its first AT command *unless* /var/fm350.status
# already exists (atc.sh line ~216) - it's a fixed pre-sleep, not a "wait
# until the modem responds" gate. We pre-touch it for every scenario,
# including slow_boot (whose silence window is modelled by fake_fm350.py
# delaying its reply to whichever command arrives first - see
# --boot-silence in fake_fm350.py's docstring for why it has to work that
# way and not as a fixed pre-sleep here too), purely to keep this test's
# runtime reasonable; it doesn't change any AT command or netifd-facing
# behaviour, only which of two equally-real starting states ("fresh boot"
# vs "already through one boot cycle in this session") atc.sh sees.
: >/var/fm350.status

NOTIFY_LOG="$notify" ATC_INTERFACE=wwan sh /root/atc-sim/run_setup.sh >"$setup_log" 2>&1 &
setup_pid=$!

(
	sleep "$timeout_s"
	kill -9 "$setup_pid" 2>/dev/null
) &
watchdog_pid=$!

# setup_pid normally exits on its own for nosim/cgact_error (proto_atc_setup
# returns after proto_notify_error+proto_block_restart); for ok/slow_boot it
# never returns on success and is always reaped by the watchdog above, so a
# nonzero/killed wait status here is expected, not a driver.sh error.
set +e
wait "$setup_pid" 2>/dev/null
setup_exit=$?
set -e
echo "$setup_exit" >"$outdir/setup.exit"

kill -9 "$watchdog_pid" 2>/dev/null || true
kill -9 "$fake_pid" 2>/dev/null || true
wait "$watchdog_pid" 2>/dev/null || true
wait "$fake_pid" 2>/dev/null || true

exit 0
