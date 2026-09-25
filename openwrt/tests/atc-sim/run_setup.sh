#!/bin/sh
# shellcheck shell=dash disable=SC3043,SC2034,SC1091,SC2329
# SC3043 (local): ash (this script's actual target shell) supports `local`,
# matching atc.sh's own style; only plain POSIX sh doesn't.
# SC2034 (INCLUDE_ONLY): consumed by atc.sh, which is sourced below, not
# visible to shellcheck's single-file analysis.
# SC1091 (not following sourced files): /lib/functions.sh, netifd-proto.sh
# and atc.sh only exist inside the OpenWrt Docker rootfs this runs in.
# SC2329 (_proto_notify never invoked): it overrides the real
# _proto_notify, called indirectly by atc.sh via proto_send_update etc.
#
# run_setup.sh - runs atc.sh's proto_atc_setup for network.wwan standalone,
# without netifd/procd/ubus. Copied into the test container and invoked by
# driver.sh (never run directly, and not shellcheck-clean POSIX-wise: it
# depends on ash-only locals in the sourced netifd/atc.sh scripts).
#
# Deliberately *not* `set -e`/`set -u`: atc.sh (unmodified, see below) relies
# on ash's lenient defaults - e.g. `[ "$atOut" != 'OK' ] && echo $atOut`
# assumes a failing `gcom` command substitution does not abort the script.
# Running this under `set -e` would make command substitutions from a
# nonzero-exit gcom call kill the whole script, which is not how atc.sh
# behaves in production.
#
# Mirrors netifd's own `_proto_do_setup()` (lib/netifd/netifd-proto.sh):
#   json_load "$data"; eval "proto_$1_setup \"$interface\" \"$ifname\""
# $data here is built from the already-applied `network.wwan` uci section
# (the real uci/network-atc.uci template, rendered by driver.sh), so this
# test is genuinely driven by our uci config, not a hand-written stand-in.
#
# Stub #1 (documented): atc.sh's own top-of-file block
#   [ -n "$INCLUDE_ONLY" ] || { . /lib/functions.sh; . ../netifd-proto.sh; init_proto "$@"; }
# is skipped by setting INCLUDE_ONLY=1 before sourcing it - this is a hook
# atc.sh ships for exactly this purpose (standalone sourcing), so we source
# functions.sh/netifd-proto.sh ourselves instead, matching what that block
# would have done.
#
# Stub #2 (documented): we inject "ifname" into the config json ourselves.
# network.wwan has no "ifname" uci option; netifd would normally leave it
# unset for a no_device proto like atc, which sends atc.sh into its sysfs
# USB-topology walk (atc.sh: `readlink -f /sys/class/tty/$devname/device`
# then `ls $devpath/../../*/net/`, ~lines 199-207) to find the RNDIS net
# device sibling of the AT tty. Docker has no real FM350 USB device tree to
# walk, so we set ifname here to skip it; nothing else in atc.sh's own logic
# is bypassed.
#
# Stub #3 (documented): _proto_notify (netifd-proto.sh) is the single
# function all of proto_send_update/proto_notify_error/proto_block_restart/
# proto_set_available funnel through before calling
# `ubus call network.interface notify_proto "$(json_dump)"`. We override
# just that one function to append the json payload to $NOTIFY_LOG instead,
# since no real netifd/ubus is running. Every proto_add_ipv4_address /
# proto_add_ipv4_route / proto_add_dns_server / proto_init_update /
# proto_send_update call in atc.sh itself runs completely unmodified; they
# just build up shell variables that _proto_notify's real implementation
# (also unmodified) turns into the json we capture.

interface=${ATC_INTERFACE:-wwan}

. /lib/functions.sh
. /lib/netifd/netifd-proto.sh

_proto_notify() {
	local interface="$1"
	json_add_string "interface" "$interface"
	json_dump >>"${NOTIFY_LOG:-/dev/null}"
}

INCLUDE_ONLY=1
. /lib/netifd/proto/atc.sh

device=$(uci -q get network."$interface".device)
apn=$(uci -q get network."$interface".apn)
pdp=$(uci -q get network."$interface".pdp)
auth=$(uci -q get network."$interface".auth)
delay=$(uci -q get network."$interface".delay)
defaultroute=$(uci -q get network."$interface".defaultroute)
peerdns=$(uci -q get network."$interface".peerdns)
metric=$(uci -q get network."$interface".metric)

json_init
json_add_string device "$device"
json_add_string ifname "${ATC_FAKE_IFNAME:-fake-wwan0}" # stub #2, see header
json_add_string apn "$apn"
json_add_string pdp "$pdp"
json_add_string auth "$auth"
json_add_string delay "$delay"
json_add_boolean defaultroute "${defaultroute:-1}"
json_add_boolean peerdns "${peerdns:-1}"
json_add_int metric "${metric:-0}"
data=$(json_dump)

echo "run_setup.sh: config json: $data"

json_load "$data"
proto_atc_setup "$interface"
exit $?
