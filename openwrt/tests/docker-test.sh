#!/bin/sh
# docker-test.sh - validates install.sh/uninstall.sh inside an OpenWrt Docker
# rootfs: shellcheck, a --dry-run that changes nothing, a real
# (--skip-packages) run applied twice to prove idempotency (byte-identical
# `uci show` and no duplicate sections/list entries), then uninstall.sh
# removing everything that was added. Exits non-zero on any mismatch.
#
# Runs three scenarios, each in its own container:
#   stock/unset - /etc/config/mwan3 seeded from tests/fixtures/mwan3.default (the
#           real `opkg install mwan3` default config on OpenWrt 24.10), which
#           ships rules ("https", "default_rule_v4") that would otherwise
#           shadow our failover rule (mwan3 is first-match). Checks that
#           install.sh reorders/neutralises them and uninstall.sh restores
#           them, without touching mwan3.wan/mwan3.globals structurally.
#           stock overrides existing values; unset leaves options absent.
#   empty - an empty /etc/config/mwan3 and no network.wan, exercising the
#           from-scratch section-creation path.
#
# Runs on the development host (needs Docker), not on the router. POSIX sh.
set -e

SCRIPT_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
OPENWRT_DIR=$(CDPATH='' cd -- "$SCRIPT_DIR/.." && pwd)

# aarch64 OpenWrt 24.10 rootfs, confirmed to run without emulation on an
# arm64 Docker host (checked via `docker pull` + `docker run` on 2026-09-25;
# see openwrt/README.md). Override with DOCKER_TEST_IMAGE=... if this tag
# disappears from Docker Hub.
IMAGE=${DOCKER_TEST_IMAGE:-openwrt/rootfs:armsr-armv8-openwrt-24.10}
WORKDIR=/root/openwrt
APN="internet.telekom"
WORK_TMP=$(mktemp -d "${TMPDIR:-/tmp}/docker-test.XXXXXX")

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
	echo "docker-test.sh: docker not found in PATH; see openwrt/README.md for the" >&2
	echo "shellcheck-only fallback." >&2
	exit 1
}

echo "docker-test.sh: shellcheck"
if command -v shellcheck >/dev/null 2>&1; then
	shellcheck -s sh \
		"$OPENWRT_DIR/install.sh" \
		"$OPENWRT_DIR/uninstall.sh" \
		"$OPENWRT_DIR/fm350-status.sh" \
		"$OPENWRT_DIR/files/etc/hotplug.d/usb/50-fm350_driver" \
		"$OPENWRT_DIR/files/usr/sbin/fm350-watchdog" \
		"$OPENWRT_DIR/files/etc/init.d/fm350-watchdog" \
		"$SCRIPT_DIR/docker-test.sh" || fail "shellcheck reported issues"
else
	echo "docker-test.sh: shellcheck not installed, skipping static check" >&2
fi

echo "docker-test.sh: pulling $IMAGE"
if ! docker pull "$IMAGE" >/dev/null; then
	echo "docker-test.sh: could not pull $IMAGE, no Docker validation possible" >&2
	exit 1
fi

dexec() {
	docker exec "$CONTAINER" /bin/sh -c "$1"
}

# check_count DESCRIPTION EXPECTED_COUNT PATTERN SHOW_VAR_NAME
check_count() {
	got=$(eval "printf '%s\n' \"\$$4\"" | grep -cE "$3")
	if [ "$got" -ne "$2" ]; then
		fail "[$scenario] $1: expected $2 match(es), got $got"
	fi
}

# assert_match DESCRIPTION PATTERN SHOW_VAR_NAME
assert_match() {
	if ! eval "printf '%s\n' \"\$$3\"" | grep -qE "$2"; then
		fail "[$scenario] $1"
	fi
}

# assert_no_match DESCRIPTION PATTERN SHOW_VAR_NAME
assert_no_match() {
	if eval "printf '%s\n' \"\$$3\"" | grep -qE "$2"; then
		fail "[$scenario] $1"
	fi
}

run_scenario() {
	scenario=$1
	mwan3_seed=$2 # "empty" or "stock"
	CONTAINER="fm350-usb-docker-test-$$-$scenario"

	echo "docker-test.sh: === scenario: $scenario ==="
	echo "docker-test.sh: starting container $CONTAINER"
	docker run -d --name "$CONTAINER" "$IMAGE" /bin/sh -c "sleep 3600" >/dev/null

	echo "docker-test.sh: copying openwrt/ into the container"
	dexec "mkdir -p $WORKDIR" >/dev/null
	docker cp "$OPENWRT_DIR/." "$CONTAINER:$WORKDIR" >/dev/null
	dexec "chmod +x $WORKDIR/install.sh $WORKDIR/uninstall.sh $WORKDIR/fm350-status.sh"

	echo "docker-test.sh: seeding minimal /etc/config ($scenario)"
	cat >"$WORK_TMP/network" <<'EOF'
config interface 'loopback'
	option device 'lo'
	option proto 'static'
	option ipaddr '127.0.0.1'
	option netmask '255.0.0.0'

config interface 'lan'
	option device 'eth0'
	option proto 'static'
	option ipaddr '192.168.1.1'
	option netmask '255.255.255.0'

config interface 'wan'
	option device 'eth1'
	option proto 'dhcp'
EOF
	dexec "mkdir -p /etc/config"
	if [ "$(dexec '[ -f /etc/config/network ] && echo yes || echo no')" = no ]; then
		docker cp "$WORK_TMP/network" "$CONTAINER:/etc/config/network"
	fi
	# The image ships a default /etc/config/firewall that already has a 'wan'
	# zone; only create one if it's missing.
	if [ "$(dexec '[ -f /etc/config/firewall ] && echo yes || echo no')" = no ]; then
		cat >"$WORK_TMP/firewall" <<'EOF'
config zone
	option name 'lan'
	list network 'lan'

config zone
	option name 'wan'
	list network 'wan'
EOF
		docker cp "$WORK_TMP/firewall" "$CONTAINER:/etc/config/firewall"
	fi
	if [ "$mwan3_seed" = stock ]; then
		docker cp "$OPENWRT_DIR/tests/fixtures/mwan3.default" "$CONTAINER:/etc/config/mwan3"
		if [ "$scenario" = stock ]; then
			# Exercise restoration of custom values, not just stock defaults.
			dexec "uci set network.wan.metric='77'; uci commit network; uci set mwan3.wan.enabled='0'; uci set mwan3.wan.family='ipv6'; uci set mwan3.wan.interval='31'; uci set mwan3.wan.down='7'; uci set mwan3.wan.up='9'; uci set mwan3.globals.mmx_mask='0xAA00'; uci commit mwan3"
		else
			# Existing sections with several unset options, including mmx_mask.
			dexec "uci -q delete network.wan.metric; uci commit network; uci -q delete mwan3.globals.mmx_mask; uci commit mwan3"
		fi
	else
		dexec ": > /etc/config/mwan3"
		dexec "uci -q delete network.wan; uci commit network"
	fi

	echo "docker-test.sh: [$scenario] install.sh --dry-run must change nothing"
	before_dry=$(dexec "uci show 2>/dev/null")
	dexec "cd $WORKDIR && ./install.sh --apn '$APN' --dry-run" >"$WORK_TMP/$scenario-dry-run.log" 2>&1 ||
		fail "[$scenario] install.sh --dry-run exited non-zero"
	after_dry=$(dexec "uci show 2>/dev/null")
	if [ "$before_dry" != "$after_dry" ]; then
		fail "[$scenario] install.sh --dry-run modified uci config"
	fi
	if dexec "uci -q get network.wwan >/dev/null 2>&1"; then
		fail "[$scenario] install.sh --dry-run created network.wwan"
	fi

	echo "docker-test.sh: [$scenario] install.sh --skip-packages, run 1"
	dexec "cd $WORKDIR && ./install.sh --apn '$APN' --skip-packages" >"$WORK_TMP/$scenario-run1.log" 2>&1 ||
		fail "[$scenario] install.sh run 1 exited non-zero"
	show1=$(dexec "uci show 2>/dev/null")

	echo "docker-test.sh: [$scenario] install.sh --skip-packages, run 2 (idempotency)"
	dexec "cd $WORKDIR && ./install.sh --apn '$APN' --skip-packages" >"$WORK_TMP/$scenario-run2.log" 2>&1 ||
		fail "[$scenario] install.sh run 2 exited non-zero"
	show2=$(dexec "uci show 2>/dev/null")

	if [ "$show1" != "$show2" ]; then
		fail "[$scenario] uci show differs between run 1 and run 2 (not idempotent)"
		printf '%s\n' "$show1" >"$WORK_TMP/$scenario-show1.txt"
		printf '%s\n' "$show2" >"$WORK_TMP/$scenario-show2.txt"
		diff "$WORK_TMP/$scenario-show1.txt" "$WORK_TMP/$scenario-show2.txt" >&2 || true
	fi

	# No duplicate sections or list entries after two runs.
	check_count "network.wwan=interface" 1 '^network\.wwan=interface$' show2
	check_count "'wwan' in wan zone network list" 1 "^firewall\.@zone\[[0-9]+\]\.network=.*'wwan'" show2
	check_count "firewall.wwan_allow_ra rule" 1 '^firewall\.wwan_allow_ra=rule$' show2
	check_count "mwan3.wwan track_ip entries" 1 "^mwan3\.wwan\.track_ip='1\.1\.1\.1' '9\.9\.9\.9'$" show2
	check_count "mwan3.failover use_member entries" 1 "^mwan3\.failover\.use_member='wan_m1' 'wwan_m2'$" show2
	check_count "mwan3.default rule" 1 '^mwan3\.default=rule$' show2
	check_count "mwan3.default family ipv4" 1 "^mwan3\.default\.family='ipv4'$" show2
	# Our track_ips must appear exactly once each in mwan3.wan (no duplicates
	# even when they were already present in a stock config).
	check_count "mwan3.wan track_ip contains 1.1.1.1 once" 1 "^mwan3\.wan\.track_ip=.*'1\.1\.1\.1'" show2
	check_count "mwan3.wan track_ip contains 9.9.9.9 once" 1 "^mwan3\.wan\.track_ip=.*'9\.9\.9\.9'" show2
	check_count "fm350_watchdog.main section" 1 '^fm350_watchdog\.main=fm350_watchdog$' show2
	check_count "fm350_watchdog.main.enabled" 1 "^fm350_watchdog\.main\.enabled='1'$" show2
	check_count "fm350_watchdog.main.interface" 1 "^fm350_watchdog\.main\.interface='wwan'$" show2

	dexec "[ -x /usr/sbin/fm350-watchdog ]" || fail "[$scenario] /usr/sbin/fm350-watchdog missing or not executable"
	dexec "[ -x /etc/init.d/fm350-watchdog ]" || fail "[$scenario] /etc/init.d/fm350-watchdog missing or not executable"
	dexec "ls /etc/rc.d/ 2>/dev/null | grep -q fm350-watchdog" ||
		fail "[$scenario] fm350-watchdog is not enabled (no /etc/rc.d symlink)"
	# rc.common's `enable` creates two symlinks (S95.../K10..., start/stop
	# order); idempotent means re-running it never creates more than those
	# two, even after a second install.sh run.
	rcd_count=$(dexec "ls /etc/rc.d/ 2>/dev/null | grep -c fm350-watchdog")
	[ "$rcd_count" -eq 2 ] || fail "[$scenario] expected exactly 2 fm350-watchdog rc.d symlinks (S95.../K10...), got $rcd_count"

	# mwan3.default must be the first "rule" section (mwan3 is first-match).
	first_rule=$(printf '%s\n' "$show2" | grep -E "^mwan3\.[A-Za-z0-9_]+=rule$" | head -n1)
	if [ "$first_rule" != "mwan3.default=rule" ]; then
		fail "[$scenario] mwan3.default is not the first rule section (got: $first_rule)"
	fi

	if [ "$mwan3_seed" = stock ]; then
		assert_match "mwan3.default_rule_v4 not pointed at failover" \
			"^mwan3\.default_rule_v4\.use_policy='failover'$" show2
		assert_match "mwan3.default_rule_v4 missing fm350_orig_policy" \
			"^mwan3\.default_rule_v4\.fm350_orig_policy='balanced'$" show2
		assert_match "mwan3.https not pointed at failover" \
			"^mwan3\.https\.use_policy='failover'$" show2
		assert_match "mwan3.https missing fm350_orig_policy" \
			"^mwan3\.https\.fm350_orig_policy='balanced'$" show2
		assert_match "mwan3.default_rule_v6 was touched (should be left alone)" \
			"^mwan3\.default_rule_v6\.use_policy='balanced'$" show2
		# 1.1.1.1 is already a stock track_ip: install.sh must not re-add it or
		# record it as ours, only append the genuinely new 9.9.9.9.
		assert_match "mwan3.wan lost its stock track_ip entries, or reordered them" \
			"^mwan3\.wan\.track_ip='1\.0\.0\.1' '1\.1\.1\.1' '208\.67\.222\.222' '208\.67\.220\.220' '9\.9\.9\.9'$" show2
		assert_match "mwan3.wan.fm350_added_track_ip should only record 9.9.9.9 (1.1.1.1 pre-existed)" \
			"^mwan3\.wan\.fm350_added_track_ip='9\.9\.9\.9'$" show2
		assert_match "mwan3.wan.reliability was overwritten (should stay stock '2')" \
			"^mwan3\.wan\.reliability='2'$" show2
	else
		assert_match "mwan3.wan should only have our two track_ips in the empty scenario" \
			"^mwan3\.wan\.track_ip='1\.1\.1\.1' '9\.9\.9\.9'$" show2
		assert_match "mwan3.wan.fm350_added_track_ip should record both IPs in the empty scenario" \
			"^mwan3\.wan\.fm350_added_track_ip='1\.1\.1\.1' '9\.9\.9\.9'$" show2
	fi

	echo "docker-test.sh: [$scenario] uninstall.sh"
	dexec "cd $WORKDIR && ./uninstall.sh" >"$WORK_TMP/$scenario-uninstall.log" 2>&1 ||
		fail "[$scenario] uninstall.sh exited non-zero"
	after_uninstall=$(dexec "uci show 2>/dev/null")
	if [ "$after_uninstall" != "$before_dry" ]; then
		fail "[$scenario] uninstall did not restore the original UCI state"
		printf '%s\n' "$before_dry" >"$WORK_TMP/$scenario-before.txt"
		printf '%s\n' "$after_uninstall" >"$WORK_TMP/$scenario-after.txt"
		diff "$WORK_TMP/$scenario-before.txt" "$WORK_TMP/$scenario-after.txt" >&2 || true
	fi

	assert_no_match "uninstall.sh left network.wwan behind" '^network\.wwan=' after_uninstall
	assert_no_match "uninstall.sh left 'wwan' in the wan zone's network list" \
		"^firewall\.@zone\[[0-9]+\]\.network=.*'wwan'" after_uninstall
	assert_no_match "uninstall.sh left the Allow modem RA rule behind" '^firewall\.wwan_allow_ra=' after_uninstall
	for section in wwan wan_m1 wwan_m2 failover default; do
		if printf '%s\n' "$after_uninstall" | grep -q "^mwan3\.$section="; then
			fail "[$scenario] uninstall.sh left mwan3.$section behind"
		fi
	done
	if [ "$mwan3_seed" = stock ]; then
		assert_match "uninstall.sh removed pre-existing mwan3.wan" '^mwan3\.wan=interface$' after_uninstall
		assert_match "uninstall.sh removed pre-existing mwan3.globals" '^mwan3\.globals=globals$' after_uninstall
	else
		assert_no_match "uninstall.sh left installer-created mwan3.wan" '^mwan3\.wan=' after_uninstall
		assert_no_match "uninstall.sh left installer-created mwan3.globals" '^mwan3\.globals=' after_uninstall
	fi
	assert_no_match "uninstall.sh left our 9.9.9.9 track_ip in mwan3.wan" \
		"^mwan3\.wan\.track_ip=.*'9\.9\.9\.9'" after_uninstall
	assert_no_match "uninstall.sh left the fm350_added_track_ip bookkeeping option behind" \
		'^mwan3\.wan\.fm350_added_track_ip=' after_uninstall
	assert_no_match "uninstall.sh left fm350_watchdog.main behind" '^fm350_watchdog\.main=' after_uninstall
	if dexec "[ -e /usr/sbin/fm350-watchdog ]"; then
		fail "[$scenario] uninstall.sh left /usr/sbin/fm350-watchdog behind"
	fi
	if dexec "[ -e /etc/init.d/fm350-watchdog ]"; then
		fail "[$scenario] uninstall.sh left /etc/init.d/fm350-watchdog behind"
	fi
	if dexec "[ -e /etc/config/fm350_watchdog ]"; then
		fail "[$scenario] uninstall.sh left /etc/config/fm350_watchdog behind"
	fi
	if dexec "ls /etc/rc.d/ 2>/dev/null | grep -q fm350-watchdog"; then
		fail "[$scenario] uninstall.sh left an fm350-watchdog rc.d symlink behind"
	fi
	if dexec "[ -f /usr/bin/fm350-status ]"; then
		fail "[$scenario] uninstall.sh left /usr/bin/fm350-status behind"
	fi

	if [ "$mwan3_seed" = stock ]; then
		assert_match "uninstall.sh did not restore mwan3.default_rule_v4.use_policy" \
			"^mwan3\.default_rule_v4\.use_policy='balanced'$" after_uninstall
		assert_no_match "uninstall.sh left fm350_orig_policy on mwan3.default_rule_v4" \
			'^mwan3\.default_rule_v4\.fm350_orig_policy=' after_uninstall
		assert_match "uninstall.sh did not restore mwan3.https.use_policy" \
			"^mwan3\.https\.use_policy='balanced'$" after_uninstall
		assert_no_match "uninstall.sh left fm350_orig_policy on mwan3.https" \
			'^mwan3\.https\.fm350_orig_policy=' after_uninstall
		# 1.1.1.1 was never recorded in fm350_added_track_ip (it pre-existed),
		# so uninstall.sh must not remove it: mwan3.wan.track_ip must equal
		# the stock list exactly, including 1.1.1.1.
		assert_match "mwan3.wan.track_ip must equal the stock list exactly (including 1.1.1.1) after uninstall" \
			"^mwan3\.wan\.track_ip='1\.0\.0\.1' '1\.1\.1\.1' '208\.67\.222\.222' '208\.67\.220\.220'$" after_uninstall
	else
		assert_no_match "mwan3.wan still has a track_ip option in the empty scenario after uninstall" \
			'^mwan3\.wan\.track_ip=' after_uninstall
	fi

	docker rm -f "$CONTAINER" >/dev/null 2>&1
	CONTAINER=""
}

run_scenario stock stock
run_scenario unset stock
run_scenario empty empty

if [ "$status" -eq 0 ]; then
	echo "docker-test.sh: PASS (image: $IMAGE, scenarios: stock unset empty)"
else
	echo "docker-test.sh: FAIL, see output above" >&2
	for f in "$WORK_TMP"/*.log; do
		[ -e "$f" ] || continue
		echo "--- $(basename "$f") ---" >&2
		cat "$f" >&2
	done
fi

exit "$status"
