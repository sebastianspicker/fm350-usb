#!/usr/bin/env bash
# macOS side of the Fibocom L830-EB NCM data session (needs sudo). Reads state/l830_ncm_session.json,
# written by `l830_ncm.py up`. Touches only the L830's NCM interface and host routes, never the default route.
#
#   sudo l830_net.sh up   SERVER_IP [SERVER_IP ...]   bring en7 up for these hosts only, then verify
#   sudo l830_net.sh down SERVER_IP [SERVER_IP ...]   undo everything `up` did
#
# up, in this order:
#   1. pf anchor com.apple/l830-ncm (evaluated through `anchor "com.apple/*"` in /etc/pf.conf): on en7, pass
#      only IPv4 from the session address to the given hosts and drop everything else (mDNS/Bonjour,
#      IGMP, IPv6, stray app traffic). pf is enabled with a reference token, released again by `down`.
#   2. IPv4 and IPv6 configuration off for the macOS network service of en7: stops DHCP (sent via BPF,
#      which pf cannot filter) and self-assigned 169.254 addresses. `down` restores DHCP/automatic.
#   3. session address on en7, host route per server via the module's gateway.
#   4. static ARP entry gateway -> GW_MAC: the module does not answer ARP in M-RAW_IP mode.
#   5. verify: ping the first server and count frames the Mac sends on en7 that are neither ARP nor to a
#      server; the expected count is 0.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
STATE="$HERE/state"
SESSION="$STATE/l830_ncm_session.json"
ANCHOR="com.apple/l830-ncm"
GW_MAC="${GW_MAC:-00:00:11:12:13:15}"  # any MAC works: the module ignores it; its replies come from :15
ACTION="${1:-}"
shift || true
SERVERS=("$@")
usage() { echo "usage: sudo $0 up|down SERVER_IP [SERVER_IP ...]" >&2; exit 2; }
[[ "$ACTION" == up || "$ACTION" == down ]] || usage
(( ${#SERVERS[@]} > 0 )) || usage
for s in "${SERVERS[@]}"; do
    [[ "$s" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] || { echo "not an IPv4 address: $s" >&2; exit 2; }
done
[[ "$GW_MAC" =~ ^([0-9a-f]{2}:){5}[0-9a-f]{2}$ ]] || { echo "GW_MAC must look like 00:00:11:12:13:15" >&2; exit 2; }
[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 2; }
[[ -f "$SESSION" ]] || { echo "no $SESSION; run l830_ncm.py up first" >&2; exit 2; }

read -r IFACE IP PREFIX GW < <(python3 -I -c '
import json, sys; s = json.load(open(sys.argv[1])); print(s["iface"], s["ip"], s["prefix"], s["gateway"])' "$SESSION")
# Trust boundary: only ever touch the L830's NCM interface (device-supplied MAC 00:00:11:12:13:xx).
[[ "$IFACE" =~ ^en[0-9]+$ ]] || { echo "bad iface $IFACE" >&2; exit 2; }
ifconfig "$IFACE" | grep -q 'ether 00:00:11:12:13:' || { echo "$IFACE is not the L830 NCM interface; refusing" >&2; exit 2; }
for v in "$IP" "$GW"; do [[ "$v" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] || { echo "bad address $v" >&2; exit 2; }; done
MASK=$(python3 -I -c 'import ipaddress, sys; print(ipaddress.IPv4Network("0.0.0.0/" + sys.argv[1]).netmask)' "$PREFIX")
MYMAC=$(ifconfig "$IFACE" | awk '/ether/{print $2; exit}')
SERVICE=$(networksetup -listnetworkserviceorder | awk -v dev="$IFACE" '
    /^\([0-9*]+\) /{name = $0; sub(/^\([0-9*]+\) /, "", name)} index($0, "Device: " dev ")") {print name; exit}')

if [[ "$ACTION" == up ]]; then
    echo "== 1. pf anchor $ANCHOR: on $IFACE pass only $IP -> ${SERVERS[*]}"
    {
        for s in "${SERVERS[@]}"; do echo "pass out quick on $IFACE inet from $IP to $s keep state"; done
        echo "block drop out quick on $IFACE all"
    } | pfctl -a "$ANCHOR" -f - 2>/dev/null
    pfctl -sr 2>/dev/null | grep -q 'anchor "com.apple/\*"' \
        || { echo "main pf ruleset has no anchor \"com.apple/*\"; the rules would not apply. Stopping." >&2
             pfctl -a "$ANCHOR" -F rules 2>/dev/null || true; exit 1; }
    pfctl -E 2>&1 | awk '/Token/{print $NF}' > "$STATE/pf.token"
    pfctl -a "$ANCHOR" -sr 2>/dev/null
    echo "== 2. network service '${SERVICE:-none}': IPv4/IPv6 configuration off (no DHCP, no 169.254)"
    if [[ -n "$SERVICE" ]]; then
        networksetup -setv4off "$SERVICE"; networksetup -setv6off "$SERVICE"
    else
        echo "   no macOS network service for $IFACE found; DHCP may still be sent (pf blocks the rest)"
    fi
    echo "== 3. $IFACE: $IP/$PREFIX ($MASK), host routes via $GW"
    ifconfig "$IFACE" inet "$IP" netmask "$MASK" up
    for s in "${SERVERS[@]}"; do route -n add -host "$s" "$GW" >/dev/null; done
    echo "== 4. static ARP $GW -> $GW_MAC"
    arp -d "$GW" ifscope "$IFACE" >/dev/null 2>&1 || true
    arp -s "$GW" "$GW_MAC" ifscope "$IFACE"
    echo "== 5. verify: ping ${SERVERS[0]}, capture what the Mac sends on $IFACE besides ARP and the servers"
    filter="ether src $MYMAC and not arp"
    for s in "${SERVERS[@]}"; do filter="$filter and not host $s"; done
    tcpdump -n -e -i "$IFACE" -w "$STATE/leak.pcap" "$filter" 2>/dev/null &
    TD=$!
    sleep 1
    ping -c 3 -t 6 "${SERVERS[0]}" | tail -2 || true
    sleep 4; kill "$TD" 2>/dev/null || true; wait "$TD" 2>/dev/null || true
    # an empty capture still has a 24-byte header; no file means tcpdump never ran (no false "0")
    [[ -s "$STATE/leak.pcap" ]] || { echo "   capture failed: leak check NOT done" >&2; exit 1; }
    leaks=$(tcpdump -n -r "$STATE/leak.pcap" 2>/dev/null | wc -l | tr -d ' ')
    echo "   frames sent besides ARP and the servers: $leaks (expected 0)"
    (( leaks == 0 )) || tcpdump -n -e -r "$STATE/leak.pcap" 2>/dev/null | head -5
else
    arp -d "$GW" ifscope "$IFACE" >/dev/null 2>&1 || true
    for s in "${SERVERS[@]}"; do route -n delete -host "$s" >/dev/null 2>&1 || true; done
    ifconfig "$IFACE" inet "$IP" delete 2>/dev/null || true
    if [[ -n "$SERVICE" ]]; then networksetup -setdhcp "$SERVICE"; networksetup -setv6automatic "$SERVICE"; fi
    pfctl -a "$ANCHOR" -F rules 2>/dev/null || true
    [[ -s "$STATE/pf.token" ]] && pfctl -X "$(cat "$STATE/pf.token")" 2>/dev/null || true
    rm -f "$STATE/pf.token"
    echo "== $IFACE cleaned (service '${SERVICE:-none}' back to DHCP/automatic, pf anchor flushed, token released)"
fi
