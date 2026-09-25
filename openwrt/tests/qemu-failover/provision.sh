#!/bin/sh
# provision.sh - runs INSIDE the QEMU OpenWrt guest (over SSH, as root),
# copied there by qemu-failover-test.sh. Idempotency is not a design goal
# here (qemu-failover-test.sh always boots a fresh overlay), but it doesn't
# duplicate uci sections or nft rules if rerun.
#
# What it does, in order:
#   1. opkg update/install mwan3 + luci-app-mwan3 (the real package, over
#      the "wan" NIC's real internet) and kmod-veth (test-harness only, for
#      the LAN client's veth pair).
#   2. Runs our install.sh --apn ... --skip-packages (mwan3 is already
#      installed, so --skip-packages only suppresses the FM350 protocol
#      handler download, which this VM has no use for anyway).
#   3. Overrides network.wwan from the atc proto to a plain dhcp stand-in on
#      eth2 (no real modem), keeping the metric install.sh set.
#   4. Sets up nft counters that prove which NIC (eth1/eth2) LAN-client
#      traffic egresses through, and an (initially empty) nft chain that
#      qemu-failover-test.sh uses later to simulate "link up, upstream
#      dead" on demand (see remote-helpers.sh: fail_link_dead/restore).
#   5. Builds the LAN test client: a "lanclient" netns plugged into br-lan
#      via a veth pair, so its traffic traverses mwan3 policy routing like a
#      real LAN host's.
#   6. Waits for both wan and wwan to come online in mwan3.
#
# POSIX sh/ash. Not meant to be run outside the test VM.
set -e

APN=${1:-internet.telekom}

log() { echo "provision.sh: $*"; }

log "opkg update"
opkg update >/tmp/opkg-update.log 2>&1 || {
	cat /tmp/opkg-update.log >&2
	exit 1
}

log "installing mwan3, luci-app-mwan3 (real package) and kmod-veth (test harness only)"
opkg install mwan3 luci-app-mwan3 kmod-veth >/tmp/opkg-install.log 2>&1 || {
	cat /tmp/opkg-install.log >&2
	exit 1
}

log "running install.sh --apn $APN --skip-packages"
cd /root/openwrt
chmod +x install.sh uninstall.sh fm350-status.sh
./install.sh --apn "$APN" --skip-packages

log "overriding network.wwan to a plain dhcp stand-in on eth2 (no real modem)"
uci -q batch <<'EOF'
delete network.wwan.apn
delete network.wwan.pdp
delete network.wwan.auth
delete network.wwan.delay
set network.wwan.proto='dhcp'
set network.wwan.device='eth2'
commit network
EOF

log "reloading network/firewall, restarting mwan3"
/etc/init.d/network reload
/etc/init.d/firewall reload
/etc/init.d/mwan3 restart

log "setting up nft egress-proof counters (table egresstest)"
# hook postrouting (not output): LAN-client traffic is forwarded through the
# router, it never hits the output hook (that only sees packets the router
# itself originates, which is what assertion (f) below exercises).
# postrouting sees every packet about to leave a given NIC either way, so
# one chain proves egress for both the LAN client and router-local traffic.
nft delete table inet egresstest >/dev/null 2>&1 || true
nft add table inet egresstest
nft add counter inet egresstest egress_wan
nft add counter inet egresstest egress_wwan
nft add chain inet egresstest post '{ type filter hook postrouting priority -5; }'
nft add rule inet egresstest post oifname "eth1" ip daddr 8.8.8.8 counter name egress_wan
nft add rule inet egresstest post oifname "eth2" ip daddr 8.8.8.8 counter name egress_wwan

log "setting up nft fault-injection chain (table failtest, empty until a test phase enables it)"
nft delete table inet failtest >/dev/null 2>&1 || true
nft add table inet failtest
nft add chain inet failtest out '{ type filter hook output priority -1; }'

log "setting up LAN test client (netns 'lanclient' + veth on br-lan)"
ip link add veth-lan type veth peer name veth-ns
ip link set veth-lan master br-lan
ip link set veth-lan up
ip netns add lanclient
ip link set veth-ns netns lanclient
ip netns exec lanclient ip link set veth-ns name eth0
ip netns exec lanclient ip link set lo up
ip netns exec lanclient ip link set eth0 up
ip netns exec lanclient udhcpc -i eth0 -n -q -t 10 -T 3

log "waiting for wan and wwan to come online in mwan3"
i=0
wan_ok=0
wwan_ok=0
while [ "$i" -lt 60 ]; do
	wan_ok=0
	wwan_ok=0
	mwan3 status 2>/dev/null | grep -qE 'interface wan is online' && wan_ok=1
	mwan3 status 2>/dev/null | grep -qE 'interface wwan is online' && wwan_ok=1
	[ "$wan_ok" -eq 1 ] && [ "$wwan_ok" -eq 1 ] && break
	sleep 2
	i=$((i + 2))
done
if [ "$wan_ok" -ne 1 ] || [ "$wwan_ok" -ne 1 ]; then
	echo "provision.sh: wan/wwan did not both come online within 60s" >&2
	mwan3 status >&2
	exit 1
fi

log "done"
