#!/bin/sh
# uninstall.sh - remove the config added by install.sh from a GL.iNet
# Flint 2 / vanilla OpenWrt 24.10 router.
#
# Removes:
#   - network.wwan
#   - restores network.wan.metric and the mwan3.wan options changed by the
#     installer, including their original unset state; removes a wan/globals
#     section if the installer had to create it
#   - "wwan" from the wan firewall zone's network list (unless it was already
#     there before install.sh ran), and the "Allow modem RA" firewall rule
#   - your own network.wwan / mwan3 wwan, wan_m1, wwan_m2, failover, default
#     sections, if install.sh had to replace them: restored from the copies
#     it saved in /etc/fm350-usb; likewise a default_rule_v6 it removed or
#     redirected (IPv6, unverified on hardware)
#   - the mwan3 sections exclusively owned by install.sh (wwan, wan_m1,
#     wwan_m2, failover, default), but only those carrying fm350_owned, or all
#     of them if install.sh recorded mwan3 state (so an uninstall after
#     --no-mwan3 leaves your own sections alone); likewise network.wwan is
#     left alone when it is yours and install.sh never ran
#   - exactly the track_ip entries install.sh actually added to mwan3.wan,
#     read from the fm350_added_track_ip option it recorded them in (the
#     pre-existing wan/globals sections are left in place). A track IP that
#     already existed
#     before install.sh ran (e.g. it coincidentally matches a stock default)
#     is never recorded there, so it survives uninstall untouched.
#   - the use_policy override install.sh applied to the stock
#     "default_rule_v4"/"https" mwan3 rules, restored from the
#     fm350_orig_policy option it saved
#   - /etc/hotplug.d/usb/50-fm350_driver
#   - /usr/bin/fm350-status
#   - fm350-watchdog: stops+disables the service, then removes
#     /etc/init.d/fm350-watchdog, /usr/sbin/fm350-watchdog and its uci config
#     (/etc/config/fm350_watchdog, entirely ours)
#
# Leaves installed packages (comgt, mwan3, luci-proto-atc, ...) in place; see
# the printed hint at the end for how to remove them. POSIX sh/ash compatible.
set -e

# Where install.sh saved user sections it replaced (see install.sh).
STATE_DIR=/etc/fm350-usb

log() { echo "uninstall.sh: $*"; }

find_wan_zone() {
	uci -q show firewall 2>/dev/null | sed -n "s/^\(firewall\.[^.]*\)\.name='wan'$/\1/p" | head -n1
}

# Restores a stock mwan3 rule's use_policy from the fm350_orig_policy option
# install.sh saved, then removes that option. No-op if the rule doesn't exist
# or was never touched (--no-mwan3, or install.sh never ran).
restore_mwan3_rule() {
	section=$1
	uci -q get "mwan3.$section" >/dev/null 2>&1 || return 0
	if orig=$(uci -q get "mwan3.$section.fm350_orig_policy" 2>/dev/null); then
		if [ -n "$orig" ]; then
			uci set "mwan3.$section.use_policy=$orig"
		else
			uci -q delete "mwan3.$section.use_policy"
		fi
		uci -q delete "mwan3.$section.fm350_orig_policy"
	fi
}

restore_option() {
	state=$1
	name=$2
	target=$3
	case "$(uci -q get "$state.${name}_state" 2>/dev/null)" in
	set)
		# No stored value (uci drops an option set to ''): the original was
		# empty. Must not fail: set -e would abort the whole uninstall here.
		original=$(uci -q get "$state.${name}_value" 2>/dev/null) || original=""
		if [ -n "$original" ]; then
			uci set "$target=$original"
		else
			uci -q delete "$target" || true
		fi
		;;
	unset) uci -q delete "$target" || true ;;
	esac
}

restore_section() {
	state=$1
	name=$2
	target=$3
	if [ "$(uci -q get "$state.${name}_state" 2>/dev/null)" = absent ]; then
		uci -q delete "$target" || true
		return 1
	fi
	return 0
}

# Re-creates a section the user had before install.sh replaced it, from the
# uci batch file install.sh saved in $STATE_DIR (see save_foreign_section
# there). Run after the installer's section was deleted. No-op without a file.
restore_saved_section() {
	backup="$STATE_DIR/$1.$2.batch"
	[ -f "$backup" ] || return 0
	log "restoring your original $1.$2 from $backup"
	if uci -q batch <"$backup"; then
		rm -f "$backup"
	else
		log "WARNING: could not restore $1.$2; the saved copy stays in $backup"
	fi
}

# A network.wwan that exists, isn't ours (no fm350_owned marker) and with no
# install state section is the user's own: install.sh never ran (or never got
# that far), so leave it and its firewall zone membership alone.
user_wwan=0
if uci -q get network.wwan >/dev/null 2>&1 &&
	[ "$(uci -q get network.wwan.fm350_owned 2>/dev/null)" != 1 ] &&
	! uci -q get network.fm350_install_state >/dev/null 2>&1; then
	user_wwan=1
	log "network.wwan exists but was not created by install.sh: leaving it, and its wan zone membership, alone"
else
	log "removing network.wwan"
	uci -q delete network.wwan || true
fi
restore_saved_section network wwan
if restore_section network.fm350_install_state wan network.wan; then
	restore_option network.fm350_install_state wan_metric network.wan.metric
fi
# Read before the state section is deleted below.
wwan_in_zone_before=$(uci -q get network.fm350_install_state.wan_zone_wwan_state 2>/dev/null || true)
uci -q delete network.fm350_install_state || true
uci -q commit network || true

wan_zone=$(find_wan_zone)
if [ "$user_wwan" -eq 1 ]; then
	:
elif [ "$wwan_in_zone_before" = present ]; then
	log "wwan was already in the wan firewall zone before install.sh ran, leaving it there"
elif [ -n "$wan_zone" ]; then
	log "removing wwan from the wan firewall zone's network list ($wan_zone)"
	uci -q del_list "${wan_zone}.network=wwan" || true
else
	log "no firewall zone with name='wan' found, skipping network list cleanup"
fi
log "removing the 'Allow modem RA' firewall rule"
uci -q delete firewall.wwan_allow_ra || true
uci -q commit firewall || true

log "removing mwan3 sections (wwan, wan_m1, wwan_m2, failover, default)"
# Only sections install.sh created: they carry fm350_owned (or, from an older
# installer without the marker, install.sh left its mwan3 state section). After
# an install with --no-mwan3 a section with one of these names is the user's.
mwan3_installed=0
uci -q get mwan3.fm350_install_state >/dev/null 2>&1 && mwan3_installed=1
for section in wwan wan_m1 wwan_m2 failover default; do
	if [ "$mwan3_installed" -eq 1 ] || [ "$(uci -q get "mwan3.$section.fm350_owned" 2>/dev/null)" = 1 ]; then
		uci -q delete "mwan3.$section" || true
	fi
	restore_saved_section mwan3 "$section"
done
# default_rule_v6 was removed (wan6 disabled) and saved, or redirected to
# wan_only (handled by restore_mwan3_rule below).
restore_saved_section mwan3 default_rule_v6

log "removing the track_ip entries install.sh added to mwan3.wan (leaving the section and mwan3.globals in place)"
for ip in $(uci -q get mwan3.wan.fm350_added_track_ip 2>/dev/null); do
	uci -q del_list mwan3.wan.track_ip="$ip" || true
done
uci -q delete mwan3.wan.fm350_added_track_ip || true

if restore_section mwan3.fm350_install_state wan mwan3.wan; then
	for option in enabled family interval down up; do
		restore_option mwan3.fm350_install_state "wan_$option" "mwan3.wan.$option"
	done
fi
if restore_section mwan3.fm350_install_state globals mwan3.globals; then
	restore_option mwan3.fm350_install_state globals_mmx_mask mwan3.globals.mmx_mask
fi

log "restoring the original use_policy on mwan3.default_rule_v4 / default_rule_v6 / https, if we changed them"
restore_mwan3_rule default_rule_v4
restore_mwan3_rule default_rule_v6
restore_mwan3_rule https

uci -q delete mwan3.fm350_install_state || true
uci -q commit mwan3 || true

if [ -f /etc/hotplug.d/usb/50-fm350_driver ]; then
	log "removing /etc/hotplug.d/usb/50-fm350_driver"
	rm -f /etc/hotplug.d/usb/50-fm350_driver
fi

if [ -f /usr/bin/fm350-status ]; then
	log "removing /usr/bin/fm350-status"
	rm -f /usr/bin/fm350-status
fi

if [ -f /usr/lib/fm350/at-port.sh ]; then
	log "removing /usr/lib/fm350/at-port.sh"
	rm -f /usr/lib/fm350/at-port.sh
	rmdir /usr/lib/fm350 2>/dev/null || true
fi
rmdir "$STATE_DIR" 2>/dev/null || true # only if no saved sections remain

if [ -f /etc/init.d/fm350-watchdog ]; then
	log "stopping and disabling fm350-watchdog"
	mkdir -p /var/lock # /lib/functions/procd.sh needs it for its lock file
	/etc/init.d/fm350-watchdog stop >/dev/null 2>&1 || true
	/etc/init.d/fm350-watchdog disable || true
	rm -f /etc/init.d/fm350-watchdog
fi
if [ -f /usr/sbin/fm350-watchdog ]; then
	log "removing /usr/sbin/fm350-watchdog"
	rm -f /usr/sbin/fm350-watchdog
fi
if [ -f /etc/config/fm350_watchdog ]; then
	log "removing /etc/config/fm350_watchdog (fm350-watchdog's own config file)"
	rm -f /etc/config/fm350_watchdog
fi

log "done. Reload with: /etc/init.d/network reload && /etc/init.d/firewall reload && /etc/init.d/mwan3 restart"
log "packages were left installed. To remove them:"
log "  opkg remove luci-app-mwan3 mwan3 atc-fib-fm350_gl luci-proto-atc comgt kmod-usb-serial-option kmod-usb-net-rndis"
log "  (or: apk del <same package names>)"
