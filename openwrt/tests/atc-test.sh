#!/bin/sh
# atc-test.sh - runs mrhaav's real atc-fib-fm350_gl netifd proto handler
# (atc.sh, unmodified) against a simulated Fibocom FM350-GL on a pty, inside
# an OpenWrt Docker rootfs, driven by our uci/network-atc.uci, and asserts
# the AT dialogue and the resulting netifd proto_* calls. No real modem, no
# SIM. Runs on the development host (needs Docker), not on the router.
# POSIX sh.
#
# AT sequence atc.sh's proto_atc_setup drives (traced from
# atc-fib-fm350_gl_2025.08.24-r3; see tests/atc-sim/fake_fm350.py's
# docstring for the full blow-by-blow including which URCs fire which
# follow-up command):
#   AT+CMEE=2, AT+CPIN?, AT+CFUN=4, ATI, AT+CREG=0, AT+CGREG=3, AT+CEREG=3,
#   AT+C5GREG=3, AT+CGEREP=2,1, AT+EIAAPN=..., AT+CGDCONT=1,"IP","<apn>",
#   AT+CTZR=1, AT+CMGF=0, AT+CSCS="GSM", AT+CNMI=2,1, AT+CFUN=1, then purely
#   URC-driven: AT+COPS=3,0;+COPS?;+COPS=3,2;+COPS?, AT+CGACT=1,1,
#   AT+CGPADDR=1, AT+CGCONTRDP=1 (this last URC alone triggers
#   proto_add_ipv4_address/proto_add_ipv4_route/proto_add_dns_server/
#   proto_send_update - it doesn't wait for its own OK).
#
# Scenarios (see tests/atc-sim/fake_fm350.py and tests/atc-sim/responses.py
# for exactly which AT responses are bench-log-verified vs. inferred):
#   ok           - SIM present, PDP activates; asserts the full AT
#                  transcript and the resulting proto_send_update ipaddr/
#                  routes/dns.
#   nosim        - AT+CPIN? returns the bench-log "SIM not inserted" CME
#                  error. FINDING: atc.sh aborts cleanly right there
#                  (proto_notify_error + proto_block_restart + return 1) -
#                  it does *not* hang, contrary to what you might expect
#                  from a modem-monitoring script.
#   cgact_error  - AT+CGACT=1,1 gets a "+CME ERROR: Requested service
#                  option not subscribed (#33)" final result. This is the
#                  *only* CME ERROR text atc.sh treats as fatal during
#                  activation (atc.sh lines ~528-538); any other CME ERROR
#                  text there is only logged (if atc_debug>=1) and
#                  otherwise silently ignored, leaving the handler stuck
#                  waiting on further URCs that will never come - a real
#                  hang risk we don't have a scenario for, noted here
#                  instead since building a hung scenario into a bounded
#                  test isn't useful.
#   slow_boot    - the fake modem is slow to answer the very first AT
#                  command it receives (see fake_fm350.py's --boot-silence
#                  semantics for why "slow" has to be modelled relative to
#                  first-command-received, not process start time); the
#                  handler still comes up exactly like "ok".
#
# FINDING (cosmetic, not asserted here): atc.sh's own "wait for the modem to
# become AT-ready" retry loop (`while [ $atOut != 'OK' ]`, atc.sh lines
# ~226-231) compares an *unquoted* $atOut. Whenever gcom's run_at.gcom
# returns a multi-word non-OK message (its timeout/error text always is,
# e.g. "Timeout running AT-command; AT+CMEE=2"), `[ $atOut != 'OK' ]`
# receives more than one word and busybox ash's `[` errors out
# ("test: ...: unknown operand" for a 2-word case verified via `[ $atOut !=
# OK ]`, matching the wiki-documented POSIX single/multi-arg `test` pitfall
# for unquoted expansions), which is *false* for the while loop - so the
# retry loop silently gives up after exactly one attempt instead of
# actually retrying, regardless of whether the modem is ready. In practice
# this rarely bites because gcom's own `waitfor 25 ...` (run_at.gcom) is
# already patient for up to 25s per command, but it means atc.sh's own
# extra retry loop around it doesn't add anything.
#
# FINDING (cosmetic): our uci/network-atc.uci deliberately leaves
# `atc_debug` unset (see openwrt/README.md, "Options used" section), which
# is also what a fresh install.sh run produces. atc.sh guards several debug
# echoes with `[ "$atc_debug" -gt N ]` without ever defaulting it (unlike
# `delay`, which does get `[ -z "$delay" ] && delay=15`, atc.sh line ~213).
# With atc_debug unset, every one of those checks fails with
# `sh: out of range` on stderr (visible throughout setup.log in every
# scenario here) - harmless (the guarded echo is just skipped, same as a
# real `-gt` false), but it's stderr noise on every single such check for
# any installation that doesn't set atc_debug, which is the documented
# default here.
#
# See openwrt/README.md for atc-fib-fm350_gl's package URL/version.
set -e

SCRIPT_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
OPENWRT_DIR=$(CDPATH='' cd -- "$SCRIPT_DIR/.." && pwd)
SIM_DIR="$SCRIPT_DIR/atc-sim"
CACHE_DIR="$SCRIPT_DIR/.cache"
IPK="$CACHE_DIR/atc-fib-fm350_gl.ipk"
# The same pinned file (commit + SHA-256) that install.sh downloads; keep the
# two in sync (see openwrt/README.md, "Package URLs").
IPK_URL="https://github.com/mrhaav/openwrt/raw/0d56d844cc49906285c9181a008186f4af515c85/atc/fib-fm350_gl/atc-fib-fm350_gl_2025.08.24-r3_all.ipk"
IPK_SHA256="7a15abc63d09c36b75ac88b8601817f56d3e8e5f65385c02a5fb605fd6b15050"

IMAGE=${DOCKER_TEST_IMAGE:-openwrt/rootfs:armsr-armv8-openwrt-24.10}
APN="internet.telekom"
WORK_TMP=$(mktemp -d "${TMPDIR:-/tmp}/atc-test.XXXXXX")

status=0
fail() {
	echo "FAIL: $*" >&2
	status=1
}

CONTAINER=""
# shellcheck disable=SC2317,SC2329 # invoked indirectly via the trap below
cleanup() {
	[ -n "$CONTAINER" ] && docker rm -f "$CONTAINER" >/dev/null 2>&1
	rm -rf "$WORK_TMP"
}
trap cleanup EXIT INT TERM

command -v docker >/dev/null 2>&1 || {
	echo "atc-test.sh: docker not found in PATH" >&2
	exit 1
}

echo "atc-test.sh: shellcheck"
if command -v shellcheck >/dev/null 2>&1; then
	shellcheck -s sh "$SCRIPT_DIR/atc-test.sh" || fail "shellcheck reported issues"
else
	echo "atc-test.sh: shellcheck not installed, skipping static check" >&2
fi

echo "atc-test.sh: python3 -m py_compile on tests/atc-sim/*.py"
if command -v python3 >/dev/null 2>&1; then
	python3 -m py_compile "$SIM_DIR/fake_fm350.py" "$SIM_DIR/responses.py" ||
		fail "python3 -m py_compile reported issues"
else
	echo "atc-test.sh: python3 not installed, skipping bytecode compile check" >&2
fi

mkdir -p "$CACHE_DIR"
if [ ! -s "$IPK" ]; then
	echo "atc-test.sh: fetching atc-fib-fm350_gl ipk (cached at $IPK for future runs)"
	if command -v curl >/dev/null 2>&1; then
		curl -sL -o "$IPK" "$IPK_URL"
	elif command -v wget >/dev/null 2>&1; then
		wget -O "$IPK" "$IPK_URL"
	else
		echo "atc-test.sh: neither curl nor wget found" >&2
		exit 1
	fi
fi
[ -s "$IPK" ] || {
	echo "atc-test.sh: failed to fetch $IPK_URL into $IPK" >&2
	exit 1
}
if command -v sha256sum >/dev/null 2>&1; then
	ipk_sha=$(sha256sum "$IPK" | cut -d' ' -f1)
else
	ipk_sha=$(shasum -a 256 "$IPK" | cut -d' ' -f1)
fi
[ "$ipk_sha" = "$IPK_SHA256" ] || {
	echo "atc-test.sh: $IPK has SHA-256 $ipk_sha, expected $IPK_SHA256 (delete it to re-download)" >&2
	exit 1
}

echo "atc-test.sh: pulling $IMAGE"
if ! docker pull "$IMAGE" >/dev/null; then
	echo "atc-test.sh: could not pull $IMAGE, no Docker validation possible" >&2
	exit 1
fi

dexec() {
	docker exec "$CONTAINER" /bin/sh -c "$1"
}

# assert_match DESCRIPTION PATTERN LOGNAME (a "$WORK_TMP/LOGNAME.log" file)
assert_match() {
	if ! grep -qE "$2" "$WORK_TMP/$3.log"; then
		fail "$1"
	fi
}

CONTAINER="fm350-usb-atc-test-$$"
echo "atc-test.sh: starting container $CONTAINER"
docker run -d --name "$CONTAINER" "$IMAGE" /bin/sh -c "sleep 3600" >/dev/null

echo "atc-test.sh: installing comgt, python3-light and atc-fib-fm350_gl in the container"
dexec "mkdir -p /var/lock /etc/config /root/openwrt/uci /root/atc-sim /root/work"
dexec "[ -f /etc/config/network ] || : > /etc/config/network"
docker cp "$IPK" "$CONTAINER:/tmp/atc-fib-fm350_gl.ipk" >/dev/null
dexec "opkg update" >"$WORK_TMP/opkg-update.log" 2>&1 ||
	fail "opkg update failed (see $WORK_TMP/opkg-update.log)"
dexec "opkg install comgt python3-light /tmp/atc-fib-fm350_gl.ipk" >"$WORK_TMP/opkg-install.log" 2>&1 ||
	fail "opkg install failed (see $WORK_TMP/opkg-install.log)"

# uci/network-atc.uci and tests/atc-sim/* are copied in read-only, never
# modified in place - the container is our sandbox, the checkout isn't.
docker cp "$OPENWRT_DIR/uci/network-atc.uci" "$CONTAINER:/root/openwrt/uci/network-atc.uci" >/dev/null
docker cp "$SIM_DIR/." "$CONTAINER:/root/atc-sim" >/dev/null

if [ "$status" -ne 0 ]; then
	echo "atc-test.sh: container setup failed, skipping scenarios" >&2
else
	# run_scenario SCENARIO TIMEOUT_SECONDS [BOOT_SILENCE_SECONDS]
	run_scenario() {
		scenario=$1
		timeout_s=$2
		boot_silence=${3:-0}
		outdir="/root/work/$scenario"

		echo "atc-test.sh: === scenario: $scenario ==="
		dexec "sh /root/atc-sim/driver.sh '$scenario' '$outdir' '$timeout_s' '$boot_silence'" \
			>"$WORK_TMP/$scenario-driver.log" 2>&1 ||
			fail "[$scenario] driver.sh exited non-zero (see $WORK_TMP/$scenario-driver.log)"

		transcript=$(dexec "cat '$outdir/transcript.log' 2>/dev/null")
		notify=$(dexec "cat '$outdir/notify.log' 2>/dev/null")
		setup_log=$(dexec "cat '$outdir/setup.log' 2>/dev/null")
		printf '%s\n' "$transcript" >"$WORK_TMP/$scenario-transcript.log"
		printf '%s\n' "$notify" >"$WORK_TMP/$scenario-notify.log"
		printf '%s\n' "$setup_log" >"$WORK_TMP/$scenario-setup.log"
	}

	# Full AT transcript for a clean activation ("ok"/"slow_boot"): every
	# command atc.sh sends, in receipt order, up to and including the
	# AT+CGCONTRDP=1 whose +CGCONTRDP: URC response is what actually
	# triggers proto_add_ipv4_address/proto_add_ipv4_route/
	# proto_add_dns_server/proto_send_update (see fake_fm350.py's docstring).
	ok_transcript=$(
		cat <<EOF
AT+CMEE=2
AT+CPIN?
AT+CFUN=4
ATI
AT+CREG=0
AT+CGREG=3
AT+CEREG=3
AT+C5GREG=3
AT+CGEREP=2,1
AT+EIAAPN="$APN",0,"IP","IP",0,"",""
AT+CGDCONT=1,"IP","$APN"
AT+CTZR=1
AT+CMGF=0
AT+CSCS="GSM"
AT+CNMI=2,1
AT+CFUN=1
AT+COPS=3,0;+COPS?;+COPS=3,2;+COPS?
AT+CGACT=1,1
AT+CGPADDR=1
AT+CGCONTRDP=1
EOF
	)

	# ok/slow_boot: proto_atc_setup never returns on a successful, still-up
	# interface (it blocks forever reading further URCs), so these always
	# run the full timeout before driver.sh's watchdog kills them - the
	# timeouts below are just "long enough for the whole AT sequence plus
	# gcom's own per-command overhead to complete", checked empirically.
	run_scenario ok 26
	run_scenario cgact_error 22
	run_scenario nosim 8
	run_scenario slow_boot 32 3

	echo "atc-test.sh: asserting scenario: ok"
	if [ "$(cat "$WORK_TMP/ok-transcript.log")" != "$ok_transcript" ]; then
		fail "[ok] AT transcript did not match the expected sequence"
		diff "$WORK_TMP/ok-transcript.log" - <<EOF >&2 || true
$ok_transcript
EOF
	fi
	assert_match "[ok] proto_send_update (data) missing link-up/modem" \
		'"action": 0, "ifname": "fake-wwan0", "link-up": true, "data": \{ "modem": "FM350-GL" \}' ok-notify
	assert_match "[ok] proto_send_update (IPv4) missing ipaddr 10.64.23.45/30" \
		'"ipaddr": \[ \{ "ipaddr": "10\.64\.23\.45", "mask": "30" \} \]' ok-notify
	assert_match "[ok] proto_send_update (IPv4) missing host route to the gateway" \
		'"target": "10\.64\.23\.46", "netmask": "128"' ok-notify
	assert_match "[ok] proto_send_update (IPv4) missing default route via the gateway (defaultroute=1)" \
		'"target": "0\.0\.0\.0", "netmask": "0", "gateway": "10\.64\.23\.46"' ok-notify
	assert_match "[ok] proto_send_update (IPv4) missing both DNS servers (peerdns=1)" \
		'"dns": \[ "8\.8\.8\.8", "8\.8\.4\.4" \]' ok-notify
	assert_match "[ok] atc.sh did not log 'SIMcard ready'" 'SIMcard ready' ok-setup
	assert_match "[ok] atc.sh did not log successful LTE registration" \
		'Registered to Telekom.de PLMN:26201 on LTE' ok-setup
	assert_match "[ok] atc.sh did not log 'Activate session'" 'Activate session' ok-setup

	echo "atc-test.sh: asserting scenario: nosim"
	if [ "$(cat "$WORK_TMP/nosim-transcript.log")" != "$(printf 'AT+CMEE=2\nAT+CPIN?\n')" ]; then
		fail "[nosim] AT transcript did not stop right after AT+CPIN? (SIM check happens before any other command)"
	fi
	assert_match "[nosim] proto_notify_error did not report 'SIM not inserted' (bench-log CME ERROR text)" \
		'"action": 3, "error": \[ "SIM not inserted" \]' nosim-notify
	assert_match "[nosim] proto_block_restart was not called" '"action": 4' nosim-notify
	dexec "cat /root/work/nosim/setup.exit" | grep -qx 1 ||
		fail "[nosim] proto_atc_setup did not return 1 (it should abort cleanly right after the CPIN check, not hang)"

	echo "atc-test.sh: asserting scenario: cgact_error"
	if [ "$(cat "$WORK_TMP/cgact_error-transcript.log")" != "$(printf '%s\n' "$ok_transcript" | sed -n '1,18p')" ]; then
		fail "[cgact_error] AT transcript did not stop right after AT+CGACT=1,1 (the fatal CME ERROR text, see header comment)"
	fi
	assert_match "[cgact_error] proto_notify_error did not report SESSION_FAILED" \
		'"action": 3, "error": \[ "SESSION_FAILED" \]' cgact_error-notify
	assert_match "[cgact_error] proto_block_restart was not called" '"action": 4' cgact_error-notify
	assert_match "[cgact_error] atc.sh did not log the APN-check hint" \
		'Activate session failed, check your APN settings' cgact_error-setup
	dexec "cat /root/work/cgact_error/setup.exit" | grep -qx 1 ||
		fail "[cgact_error] proto_atc_setup did not return 1"

	echo "atc-test.sh: asserting scenario: slow_boot"
	if [ "$(cat "$WORK_TMP/slow_boot-transcript.log")" != "$ok_transcript" ]; then
		fail "[slow_boot] AT transcript did not match the 'ok' sequence (a slow-to-answer modem should still come up the same way)"
	fi
	assert_match "[slow_boot] proto_send_update (IPv4) missing ipaddr 10.64.23.45/30" \
		'"ipaddr": \[ \{ "ipaddr": "10\.64\.23\.45", "mask": "30" \} \]' slow_boot-notify
fi

if [ "$status" -eq 0 ]; then
	echo "atc-test.sh: PASS (image: $IMAGE, scenarios: ok nosim cgact_error slow_boot)"
else
	echo "atc-test.sh: FAIL, see output above" >&2
	for f in "$WORK_TMP"/*.log; do
		[ -e "$f" ] || continue
		echo "--- $(basename "$f") ---" >&2
		cat "$f" >&2
	done
fi

exit "$status"
