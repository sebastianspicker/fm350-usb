#!/bin/sh
# remote-helpers.sh - sourced (not executed) inside the QEMU OpenWrt guest,
# by provision.sh and by ad hoc `. .../remote-helpers.sh; <call>` one-liners
# that qemu-failover-test.sh sends over SSH during the failover assertions.
#
# NIC layout assumed (set up by qemu-failover-test.sh, see its top comment):
#   eth0 = lan (br-lan, stock OpenWrt config, unmodified)
#   eth1 = wan (real internet via QEMU slirp)
#   eth2 = wwan (stand-in cellular uplink, real internet via QEMU slirp)
#   eth3 = mgmt (test-harness-only SSH access, not part of mwan3/firewall)
# The LAN test client is a netns ("lanclient") reachable through a veth
# plugged into br-lan, so its traffic traverses mwan3 policy routing exactly
# like a real LAN host's would.

# wait_iface_status IFACE PATTERN TIMEOUT_S
# Polls `mwan3 status`'s "interface IFACE is ..." line against an extended
# regex PATTERN (e.g. "online", "offline|not connected") until it matches or
# TIMEOUT_S elapses. Returns 1 on timeout.
wait_iface_status() {
	iface=$1
	pattern=$2
	timeout_s=$3
	elapsed=0
	while [ "$elapsed" -lt "$timeout_s" ]; do
		if mwan3 status 2>/dev/null | grep -qE "interface $iface is $pattern"; then
			return 0
		fi
		sleep 2
		elapsed=$((elapsed + 2))
	done
	return 1
}

# wait_failover_member IFACE TIMEOUT_S
# Polls until the "failover:" ipv4 policy block in `mwan3 status` shows
# IFACE at 100%, i.e. mwan3 has actually repointed traffic, not just marked
# an interface up/down. Returns 1 on timeout.
wait_failover_member() {
	iface=$1
	timeout_s=$2
	elapsed=0
	while [ "$elapsed" -lt "$timeout_s" ]; do
		if mwan3 status 2>/dev/null | awk '/^failover:$/{f=1;next} f{print;exit}' | grep -qE "^ ${iface} \(100%\)$"; then
			return 0
		fi
		sleep 2
		elapsed=$((elapsed + 2))
	done
	return 1
}

# wait_failover_unreachable TIMEOUT_S
# Polls until the "failover:" ipv4 policy block reports "unreachable"
# (both wan and wwan down, mwan3.failover.last_resort applies).
wait_failover_unreachable() {
	timeout_s=$1
	elapsed=0
	while [ "$elapsed" -lt "$timeout_s" ]; do
		if mwan3 status 2>/dev/null | awk '/^failover:$/{f=1;next} f{print;exit}' | grep -q '^ unreachable$'; then
			return 0
		fi
		sleep 2
		elapsed=$((elapsed + 2))
	done
	return 1
}

# egress_reset
# Zeroes the nft egress-proof counters (see provision.sh for the table).
egress_reset() {
	nft reset counter inet egresstest egress_wan >/dev/null
	nft reset counter inet egresstest egress_wwan >/dev/null
}

# egress_read NAME  (NAME: egress_wan | egress_wwan)
# Prints the current packet count for the named counter.
egress_read() {
	nft list counter inet egresstest "$1" 2>/dev/null | sed -n 's/.*packets \([0-9]*\).*/\1/p'
}

# lan_ping TARGET COUNT TIMEOUT_S
# Runs COUNT pings from the "lanclient" netns to TARGET, TIMEOUT_S per
# packet. Exit status is ping's (0 = all replies received).
lan_ping() {
	ip netns exec lanclient ping -c "$2" -W "$3" "$1"
}

# fail_link_dead IFNAME
# Simulates "link up, upstream dead": drops all egress on IFNAME (including
# mwan3's own track_ip pings) without touching carrier state, so only
# mwan3's ping-based tracking (not netifd's hotplug ifdown) can notice.
fail_link_dead() {
	nft flush chain inet failtest out 2>/dev/null
	nft add rule inet failtest out oifname "$1" counter drop
}

# fail_link_restore
# Undoes fail_link_dead.
fail_link_restore() {
	nft flush chain inet failtest out 2>/dev/null
}
