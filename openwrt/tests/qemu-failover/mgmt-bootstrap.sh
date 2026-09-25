#!/bin/sh
# mgmt-bootstrap.sh - typed over the QEMU serial console (there is no SSH
# yet at this point) by qemu-failover-test.sh, right after first boot.
#
# Brings up a 4th, test-harness-only "mgmt" interface on eth3 so the rest of
# the test can drive the VM over SSH instead of the serial console. eth0/lan
# (br-lan, 192.168.1.1) is deliberately left untouched: it stays available
# for the LAN test client (see provision.sh), and reusing it for SSH would
# mean bridging QEMU's slirp DHCP/gateway service onto the same L2 segment
# as the LAN client, which was tried and rejected (slirp's DHCP server won
# the race against dnsmasq for the client's lease, so its traffic never hit
# the router's mwan3 policy routing at all).
#
# network.mgmt gets a high metric so it never competes with wan/wwan for the
# main routing table's default route (mwan3 policy-routes everything dest
# 0.0.0.0/0 anyway, but this keeps router-originated traffic assertions
# unambiguous either way). It's added to the stock "lan" firewall zone
# (input ACCEPT) purely so dropbear is reachable; mwan3/the wan zone are
# never touched by this file.
#
# POSIX sh/ash. Typed as a single blob over the serial console, not run as
# a regular script file.
set -e

lan_zone=$(uci -q show firewall | sed -n "s/^\(firewall\.[^.]*\)\.name='lan'$/\1/p" | head -n1)
if [ -z "$lan_zone" ]; then
	echo "mgmt-bootstrap.sh: no firewall zone with name='lan' found" >&2
	exit 1
fi

uci -q batch <<EOF
set network.mgmt=interface
set network.mgmt.proto='dhcp'
set network.mgmt.device='eth3'
set network.mgmt.metric='100'
commit network
del_list $lan_zone.network='mgmt'
add_list $lan_zone.network='mgmt'
commit firewall
EOF

/etc/init.d/network reload
/etc/init.d/firewall reload
