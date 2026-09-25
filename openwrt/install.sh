#!/bin/sh
# install.sh - set up 5G failover on a GL.iNet Flint 2 (GL-MT6000) running
# vanilla OpenWrt 24.10 with a Fibocom FM350-GL (USB M.2 dongle).
#
# Automates docs/setup-guide.md steps 6-7: installs the FM350 protocol
# handler packages, writes a "wwan" network interface, adds it to the wan
# firewall zone, and configures mwan3 for wan/wwan failover.
#
# Must be run as root on the router. POSIX sh/ash (busybox) compatible.
#
# Usage: see ./install.sh --help
set -e

SCRIPT_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
UCI_DIR="$SCRIPT_DIR/uci"
FILES_DIR="$SCRIPT_DIR/files"

# --- mrhaav atc-fib-fm350_gl / luci-proto-atc package URLs -----------------
# Verified against the GitHub API listing of mrhaav/openwrt (atc/ and
# atc/fib-fm350_gl/) on 2026-09-25. Re-check with
#   curl -sI <url>
# or the GitHub contents API before relying on these if install.sh starts
# reporting 404s, since mrhaav ships new package revisions periodically and
# only keeps a limited history.
LUCI_PROTO_ATC_IPK_URL="https://github.com/mrhaav/openwrt/raw/master/atc/luci-proto-atc_2025.01.10-r2_all.ipk"
ATC_FIB_FM350_IPK_URL="https://github.com/mrhaav/openwrt/raw/master/atc/fib-fm350_gl/atc-fib-fm350_gl_2025.08.24-r3_all.ipk"
LUCI_PROTO_ATC_APK_URL="https://github.com/mrhaav/openwrt/raw/master/atc/luci-proto-atc-2025.01.10-r2.apk"
ATC_FIB_FM350_APK_URL="https://github.com/mrhaav/openwrt/raw/master/atc/fib-fm350_gl/atc-fib-fm350_gl-2025.01.11-r2.apk"

MODEMFEED_URL="https://github.com/koshev-msk/modemfeed"

# --- defaults ---------------------------------------------------------------
proto=atc
apn=""
dry_run=0
no_mwan3=0
no_watchdog=0
install_extras=0
skip_packages=0 # hidden: for openwrt/tests/docker-test.sh only, no packages/downloads

usage() {
	cat <<'EOF'
Usage: install.sh [options]

Options:
  --proto atc|xmm   Protocol handler for the FM350 wwan interface (default: atc).
                       atc: downloads and installs mrhaav's luci-proto-atc and
                            atc-fib-fm350_gl packages.
                       xmm: not in the official feeds; prints modemfeed
                            xmm-modem install instructions instead of
                            installing packages, but still applies the wwan
                            network/firewall/mwan3 config for proto "xmm".
  --apn APN         Carrier APN for the wwan interface (e.g. internet.telekom).
  --dry-run         Print the uci commands that would run; change nothing.
  --no-mwan3        Skip installing/configuring mwan3 (network + firewall only).
  --no-watchdog     Don't install/enable fm350-watchdog (see README.md).
  --extras          Also install usbutils and picocom (for manual debugging).
  -h, --help        Show this help.
EOF
}

while [ $# -gt 0 ]; do
	case "$1" in
	--proto)
		proto=$2
		shift 2
		;;
	--proto=*)
		proto=${1#*=}
		shift
		;;
	--apn)
		apn=$2
		shift 2
		;;
	--apn=*)
		apn=${1#*=}
		shift
		;;
	--dry-run)
		dry_run=1
		shift
		;;
	--no-mwan3)
		no_mwan3=1
		shift
		;;
	--no-watchdog)
		no_watchdog=1
		shift
		;;
	--extras)
		install_extras=1
		shift
		;;
	--skip-packages)
		skip_packages=1
		shift
		;;
	-h | --help)
		usage
		exit 0
		;;
	*)
		echo "install.sh: unknown argument: $1" >&2
		usage >&2
		exit 1
		;;
	esac
done

case "$proto" in
atc | xmm) ;;
*)
	echo "install.sh: --proto must be 'atc' or 'xmm', got '$proto'" >&2
	exit 1
	;;
esac

if [ -z "$apn" ]; then
	echo "install.sh: --apn is required (e.g. --apn internet.telekom)" >&2
	exit 1
fi

log() { echo "install.sh: $*"; }

# --- package manager detection ---------------------------------------------
pkg_mgr=""
if command -v opkg >/dev/null 2>&1; then
	pkg_mgr=opkg
elif command -v apk >/dev/null 2>&1; then
	pkg_mgr=apk
else
	echo "install.sh: neither opkg nor apk found, is this an OpenWrt router?" >&2
	exit 1
fi
log "package manager: $pkg_mgr"

pkg_install() {
	# $@: package names or local file paths (.ipk / .apk)
	[ "$skip_packages" -eq 1 ] && {
		log "--skip-packages: would install: $*"
		return 0
	}
	if [ "$dry_run" -eq 1 ]; then
		log "[dry-run] would install: $*"
		return 0
	fi
	if [ "$pkg_mgr" = opkg ]; then
		opkg install "$@"
	else
		apk add --allow-untrusted "$@"
	fi
}

pkg_update() {
	[ "$skip_packages" -eq 1 ] && return 0
	if [ "$dry_run" -eq 1 ]; then
		log "[dry-run] would run: $pkg_mgr update"
		return 0
	fi
	if [ "$pkg_mgr" = opkg ]; then
		opkg update
	else
		apk update
	fi
}

download() {
	# $1: url  $2: destination path
	if [ "$skip_packages" -eq 1 ] || [ "$dry_run" -eq 1 ]; then
		log "[skipped] would download $1"
		return 0
	fi
	if command -v wget >/dev/null 2>&1; then
		wget -O "$2" "$1"
	elif command -v curl >/dev/null 2>&1; then
		curl -L -o "$2" "$1"
	else
		echo "install.sh: neither wget nor curl found" >&2
		return 1
	fi
}

# --- base packages -----------------------------------------------------------
base_pkgs="kmod-usb-net-rndis kmod-usb-serial-option comgt"
[ "$no_mwan3" -eq 1 ] || base_pkgs="$base_pkgs mwan3 luci-app-mwan3"
[ "$install_extras" -eq 1 ] && base_pkgs="$base_pkgs usbutils picocom"

log "updating package lists"
pkg_update
log "installing: $base_pkgs"
# shellcheck disable=SC2086 # base_pkgs is an intentional word-split package list
pkg_install $base_pkgs

# --- protocol handler ---------------------------------------------------------
if [ "$proto" = atc ]; then
	tmp_dir=$(mktemp -d /tmp/5g-failover-install.XXXXXX) || {
		echo "install.sh: mktemp failed" >&2
		exit 1
	}
	trap 'rm -rf "$tmp_dir"' EXIT INT TERM

	if [ "$pkg_mgr" = opkg ]; then
		luci_pkg="$tmp_dir/luci-proto-atc.ipk"
		fib_pkg="$tmp_dir/atc-fib-fm350_gl.ipk"
		download "$LUCI_PROTO_ATC_IPK_URL" "$luci_pkg"
		download "$ATC_FIB_FM350_IPK_URL" "$fib_pkg"
	else
		luci_pkg="$tmp_dir/luci-proto-atc.apk"
		fib_pkg="$tmp_dir/atc-fib-fm350_gl.apk"
		download "$LUCI_PROTO_ATC_APK_URL" "$luci_pkg"
		download "$ATC_FIB_FM350_APK_URL" "$fib_pkg"
	fi
	log "installing luci-proto-atc and atc-fib-fm350_gl"
	pkg_install "$luci_pkg" "$fib_pkg"
else
	cat <<EOF
install.sh: --proto xmm selected. xmm-modem/luci-proto-xmm are not in the
official OpenWrt feeds, so install.sh cannot install them automatically.
To add them by hand:

  1. Add the modemfeed repository for this OpenWrt release, see:
       $MODEMFEED_URL
  2. opkg update   (or: apk update)
  3. opkg install xmm-modem luci-proto-xmm kmod-usb-acm kmod-usb-net-cdc-ncm
     (or the apk equivalents)

install.sh will still write the wwan network/firewall/mwan3 config below for
proto "xmm" so the rest of the failover setup is ready once xmm-modem is
installed.
EOF
fi

# --- find the AT command tty -------------------------------------------------
# USB interface :1.6 (0e8d:7127, mode 41, default) or :1.4 (0e8d:7126, mode 40).
find_at_device() {
	suffix=""
	for suffix in ":1.6" ":1.4"; do
		for dev in /sys/bus/usb/devices/*"$suffix"; do
			[ -d "$dev" ] || continue
			for tty in "$dev"/ttyUSB*; do
				[ -e "$tty" ] || continue
				echo "/dev/$(basename "$tty")"
				return 0
			done
		done
	done
	return 1
}

if device=$(find_at_device); then
	log "AT command device detected: $device"
else
	device=/dev/ttyUSB4
	log "WARNING: could not detect the FM350 AT tty from sysfs (is it plugged in?)."
	log "WARNING: defaulting network.wwan.device to '$device'; edit /etc/config/network if wrong."
fi

# --- uci helpers --------------------------------------------------------------
# Applies a uci/*.uci template after substituting @PLACEHOLDER@ tokens, using
# `|` as the sed delimiter since device paths contain `/`.
apply_uci_template() {
	# $1: template file  $2...: NAME=value substitutions
	template=$1
	shift
	# Strip comment and blank lines first, so a placeholder that happens to
	# appear inside a comment (e.g. "@DEVICE@" in the header docs) is never
	# substituted or sent to `uci batch`.
	rendered=$(grep -v '^[[:space:]]*#' "$template" | grep -v '^[[:space:]]*$')
	for kv in "$@"; do
		name=${kv%%=*}
		value=${kv#*=}
		rendered=$(echo "$rendered" | sed "s|@${name}@|${value}|g")
	done
	if [ "$dry_run" -eq 1 ]; then
		log "[dry-run] uci batch from $(basename "$template"):"
		echo "$rendered"
	else
		echo "$rendered" | uci -q batch
	fi
}

find_wan_zone() {
	uci -q show firewall 2>/dev/null | sed -n "s/^\(firewall\.[^.]*\)\.name='wan'$/\1/p" | head -n1
}

# Returns success if uci list $1 (e.g. mwan3.wan.track_ip) already contains
# value $2.
uci_list_contains() {
	value=$2
	for v in $(uci -q get "$1" 2>/dev/null); do
		[ "$v" = "$value" ] && return 0
	done
	return 1
}

# Adds an IP to mwan3.wan.track_ip only if it isn't already tracked (it might
# coincidentally already be a stock mwan3 default, e.g. 1.1.1.1), and records
# the ones we actually added in mwan3.wan.fm350_added_track_ip so
# uninstall.sh can remove exactly those and nothing else. Idempotent: once an
# IP is present, a second run adds and records nothing new for it.
mwan3_add_wan_track_ip() {
	ip=$1
	uci_list_contains mwan3.wan.track_ip "$ip" && return 0
	if [ "$dry_run" -eq 1 ]; then
		log "[dry-run] would add_list mwan3.wan.track_ip='$ip' and record it in fm350_added_track_ip"
		return 0
	fi
	uci add_list mwan3.wan.track_ip="$ip"
	uci add_list mwan3.wan.fm350_added_track_ip="$ip"
}

# mwan3's own default config already ships mwan3.globals.mmx_mask; only set
# it if a user (or an empty/custom mwan3 config) left it unset.
mwan3_set_mmx_mask_if_unset() {
	if [ -z "$(uci -q get mwan3.globals.mmx_mask 2>/dev/null)" ]; then
		if [ "$dry_run" -eq 1 ]; then
			log "[dry-run] would set mwan3.globals.mmx_mask='0x3F00' (currently unset)"
		else
			uci set mwan3.globals.mmx_mask='0x3F00'
		fi
	fi
}

# mwan3's stock config ships a "https" and a "default_rule_v4" rule (both
# using policy "balanced") ahead of any appended rule. Since mwan3 is
# first-match, those would shadow our "default" rule for all IPv4 traffic
# even after it's reordered to the front of the rule list, unless they are
# pointed at "failover" too. Idempotent: the original use_policy is saved in
# fm350_orig_policy only once, so a second run doesn't overwrite the saved
# original with the now-current "failover" value.
neutralize_mwan3_rule() {
	section=$1
	uci -q get "mwan3.$section" >/dev/null 2>&1 || return 0
	if ! uci -q get "mwan3.$section.fm350_orig_policy" >/dev/null 2>&1; then
		orig=$(uci -q get "mwan3.$section.use_policy" 2>/dev/null || true)
		if [ "$dry_run" -eq 1 ]; then
			log "[dry-run] would set mwan3.$section.fm350_orig_policy='$orig'"
		else
			uci set "mwan3.$section.fm350_orig_policy=$orig"
		fi
	fi
	if [ "$dry_run" -eq 1 ]; then
		log "[dry-run] would set mwan3.$section.use_policy='failover'"
	else
		uci set "mwan3.$section.use_policy=failover"
	fi
}

# --- network ------------------------------------------------------------------
log "applying network config (proto=$proto, device=$device, apn=$apn)"
if [ "$proto" = atc ]; then
	apply_uci_template "$UCI_DIR/network-atc.uci" "DEVICE=$device" "APN=$apn"
else
	apply_uci_template "$UCI_DIR/network-xmm.uci" "DEVICE=$device" "APN=$apn"
fi

# --- firewall -------------------------------------------------------------
wan_zone=$(find_wan_zone)
if [ -z "$wan_zone" ]; then
	echo "install.sh: no firewall zone with name='wan' found in /etc/config/firewall, aborting" >&2
	exit 1
fi
log "applying firewall config (wan zone: $wan_zone)"
apply_uci_template "$UCI_DIR/firewall.uci" "WAN_ZONE=$wan_zone"

# --- mwan3 ----------------------------------------------------------------
if [ "$no_mwan3" -eq 1 ]; then
	log "--no-mwan3: skipping mwan3 config"
else
	log "applying mwan3 config"
	apply_uci_template "$UCI_DIR/mwan3.uci"
	mwan3_add_wan_track_ip 1.1.1.1
	mwan3_add_wan_track_ip 9.9.9.9
	mwan3_set_mmx_mask_if_unset
	log "neutralizing stock mwan3 rules that would shadow ours (default_rule_v4, https), if present"
	neutralize_mwan3_rule default_rule_v4
	neutralize_mwan3_rule https
	[ "$dry_run" -eq 1 ] || uci commit mwan3
fi

# --- kernel < 6.6: bind the option1 serial driver by hand -------------------
kernel_lt_6_6() {
	kv=$(uname -r)
	major=$(echo "$kv" | cut -d. -f1 | sed 's/[^0-9].*//')
	minor=$(echo "$kv" | cut -d. -f2 | sed 's/[^0-9].*//')
	[ -n "$major" ] && [ "$major" -lt 6 ] 2>/dev/null && return 0
	[ -n "$major" ] && [ -n "$minor" ] && [ "$major" -eq 6 ] 2>/dev/null && [ "$minor" -lt 6 ] 2>/dev/null && return 0
	return 1
}

if kernel_lt_6_6; then
	log "kernel $(uname -r) < 6.6: installing hotplug driver for option1 new_id binding"
	if [ "$dry_run" -eq 1 ]; then
		log "[dry-run] would copy $FILES_DIR/etc/hotplug.d/usb/50-fm350_driver to /etc/hotplug.d/usb/50-fm350_driver"
	else
		mkdir -p /etc/hotplug.d/usb
		cp "$FILES_DIR/etc/hotplug.d/usb/50-fm350_driver" /etc/hotplug.d/usb/50-fm350_driver
		chmod 0755 /etc/hotplug.d/usb/50-fm350_driver
	fi
else
	log "kernel $(uname -r) >= 6.6: no hotplug driver needed (option driver already knows the FM350)"
fi

# --- status helper script ---------------------------------------------------
if [ "$dry_run" -eq 1 ]; then
	log "[dry-run] would install $SCRIPT_DIR/fm350-status.sh to /usr/bin/fm350-status"
else
	cp "$SCRIPT_DIR/fm350-status.sh" /usr/bin/fm350-status
	chmod 0755 /usr/bin/fm350-status
fi

# --- fm350-watchdog -----------------------------------------------------------
if [ "$no_watchdog" -eq 1 ]; then
	log "--no-watchdog: skipping fm350-watchdog"
else
	log "installing fm350-watchdog (uci config: fm350_watchdog.main, see uci/fm350-watchdog.uci)"
	# fm350_watchdog is a brand-new config with no package of its own to
	# create /etc/config/fm350_watchdog: uci refuses `set`/`commit` on a
	# config it has never seen a file for (even an empty one), so make sure
	# that file exists before batching the section into it.
	if [ "$dry_run" -eq 1 ]; then
		[ -f /etc/config/fm350_watchdog ] || log "[dry-run] would create empty /etc/config/fm350_watchdog"
	else
		[ -f /etc/config/fm350_watchdog ] || : >/etc/config/fm350_watchdog
	fi
	apply_uci_template "$UCI_DIR/fm350-watchdog.uci"
	if [ "$dry_run" -eq 1 ]; then
		log "[dry-run] would copy $FILES_DIR/usr/sbin/fm350-watchdog to /usr/sbin/fm350-watchdog"
		log "[dry-run] would copy $FILES_DIR/etc/init.d/fm350-watchdog to /etc/init.d/fm350-watchdog"
		log "[dry-run] would run: /etc/init.d/fm350-watchdog enable"
	else
		cp "$FILES_DIR/usr/sbin/fm350-watchdog" /usr/sbin/fm350-watchdog
		chmod 0755 /usr/sbin/fm350-watchdog
		mkdir -p /etc/init.d
		cp "$FILES_DIR/etc/init.d/fm350-watchdog" /etc/init.d/fm350-watchdog
		chmod 0755 /etc/init.d/fm350-watchdog
		# Only enable (symlink for boot), never start here: install.sh may be
		# running where procd isn't up yet (e.g. tests/docker-test.sh's plain
		# rootfs container), and mwan3/network/firewall aren't reloaded by
		# install.sh either - see the final log message below for the same
		# "you reload/start it" convention used for those. `enable` itself
		# still needs /var/lock (for its own lock file, via
		# /lib/functions/procd.sh); a bare rootfs container doesn't have it.
		mkdir -p /var/lock
		[ -x /etc/init.d/fm350-watchdog ] && /etc/init.d/fm350-watchdog enable
	fi
fi

if [ "$dry_run" -eq 1 ]; then
	log "dry run complete, nothing was changed"
else
	log "done. Reload with: /etc/init.d/network reload && /etc/init.d/firewall reload"
	[ "$no_mwan3" -eq 1 ] || log "and: /etc/init.d/mwan3 restart"
	[ "$no_watchdog" -eq 1 ] || log "and: /etc/init.d/fm350-watchdog start"
	log "check status with: fm350-status"
fi
