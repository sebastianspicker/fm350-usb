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
# Where saved copies of user sections that install.sh had to replace live
# (restored by uninstall.sh).
STATE_DIR=/etc/fm350-usb

# shellcheck source=/dev/null # files/usr/lib/fm350/at-port.sh
. "$FILES_DIR/usr/lib/fm350/at-port.sh"

# --- mrhaav atc-fib-fm350_gl / luci-proto-atc packages -----------------------
# Pinned to the mrhaav/openwrt commit we tested against (master as of
# 2026-09-25; unchanged since 2026-05-18), and verified by SHA-256 before
# anything is installed. The .ipk of atc-fib-fm350_gl is byte-identical to the
# one openwrt/tests/atc-test.sh runs. To move to a newer release: pick the new
# commit and file names, download the files, check them, and update the URLs
# and hashes together.
MRHAAV_COMMIT="0d56d844cc49906285c9181a008186f4af515c85"
MRHAAV_BASE="https://github.com/mrhaav/openwrt/raw/$MRHAAV_COMMIT/atc"
LUCI_PROTO_ATC_IPK_URL="$MRHAAV_BASE/luci-proto-atc_2025.01.10-r2_all.ipk"
LUCI_PROTO_ATC_IPK_SHA256="c3c70dbeb90c1f181024cc6c9b0449b5c549f558cf8f932350ee6b93cebd81d6"
ATC_FIB_FM350_IPK_URL="$MRHAAV_BASE/fib-fm350_gl/atc-fib-fm350_gl_2025.08.24-r3_all.ipk"
ATC_FIB_FM350_IPK_SHA256="7a15abc63d09c36b75ac88b8601817f56d3e8e5f65385c02a5fb605fd6b15050"
LUCI_PROTO_ATC_APK_URL="$MRHAAV_BASE/luci-proto-atc-2025.01.10-r2.apk"
LUCI_PROTO_ATC_APK_SHA256="7a196e9a2565534d4657d81ce9c18794ad3687812fe941bf76aa2c4106577484"
ATC_FIB_FM350_APK_URL="$MRHAAV_BASE/fib-fm350_gl/atc-fib-fm350_gl-2025.01.11-r2.apk"
ATC_FIB_FM350_APK_SHA256="94e097b6a674f818921c648ed8c6ab80639e626c129f37d4224e64fe37c2eba0"

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
case "$apn" in
	*[!a-zA-Z0-9._-]*)
		echo "install.sh: --apn may contain only letters, digits, dots, underscores, and hyphens" >&2
		exit 1
		;;
esac

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
	# $1: "repo" (signed packages from the configured feeds) or "local"
	# (downloaded .ipk / .apk files); rest: package names or file paths.
	# Only local files skip apk's signature check: mrhaav's packages aren't
	# signed with an OpenWrt feed key.
	source_kind=$1
	shift
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
	elif [ "$source_kind" = local ]; then
		apk add --allow-untrusted "$@"
	else
		apk add "$@"
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
	# $1: url  $2: destination path  $3: expected SHA-256 of the file
	if [ "$skip_packages" -eq 1 ] || [ "$dry_run" -eq 1 ]; then
		log "[skipped] would download $1 (sha256 $3)"
		return 0
	fi
	if command -v wget >/dev/null 2>&1; then
		wget -O "$2" "$1" || return 1
	elif command -v curl >/dev/null 2>&1; then
		curl -fL -o "$2" "$1" || return 1
	else
		echo "install.sh: neither wget nor curl found" >&2
		return 1
	fi
	actual=$(sha256sum "$2" | cut -d' ' -f1)
	if [ "$actual" != "$3" ]; then
		echo "install.sh: checksum mismatch for $1" >&2
		echo "  expected $3" >&2
		echo "  got      $actual" >&2
		echo "install.sh: refusing to install it. Nothing was installed from this download." >&2
		rm -f "$2"
		return 1
	fi
	log "sha256 ok: ${2##*/}"
}

# --- pre-flight: nothing below may mutate config before this passes ----------
find_wan_zone() {
	uci -q show firewall 2>/dev/null | sed -n "s/^\(firewall\.[^.]*\)\.name='wan'$/\1/p" | head -n1
}

wan_zone=$(find_wan_zone)
if [ -z "$wan_zone" ]; then
	echo "install.sh: no firewall zone with name='wan' found in /etc/config/firewall, aborting (nothing was changed)" >&2
	exit 1
fi

# --- base packages -----------------------------------------------------------
base_pkgs="kmod-usb-net-rndis kmod-usb-serial-option comgt"
[ "$no_mwan3" -eq 1 ] || base_pkgs="$base_pkgs mwan3 luci-app-mwan3"
[ "$install_extras" -eq 1 ] && base_pkgs="$base_pkgs usbutils picocom"

log "updating package lists"
pkg_update
log "installing: $base_pkgs"
# shellcheck disable=SC2086 # base_pkgs is an intentional word-split package list
pkg_install repo $base_pkgs

# --- protocol handler ---------------------------------------------------------
if [ "$proto" = atc ]; then
	tmp_dir=$(mktemp -d "${TMPDIR:-/tmp}/fm350-usb-install.XXXXXX") || {
		echo "install.sh: mktemp failed" >&2
		exit 1
	}
	trap 'rm -rf "$tmp_dir"' EXIT
	trap 'rm -rf "$tmp_dir"; exit 130' INT
	trap 'rm -rf "$tmp_dir"; exit 143' TERM

	if [ "$pkg_mgr" = opkg ]; then
		luci_pkg="$tmp_dir/luci-proto-atc.ipk"
		fib_pkg="$tmp_dir/atc-fib-fm350_gl.ipk"
		download "$LUCI_PROTO_ATC_IPK_URL" "$luci_pkg" "$LUCI_PROTO_ATC_IPK_SHA256" || exit 1
		download "$ATC_FIB_FM350_IPK_URL" "$fib_pkg" "$ATC_FIB_FM350_IPK_SHA256" || exit 1
	else
		luci_pkg="$tmp_dir/luci-proto-atc.apk"
		fib_pkg="$tmp_dir/atc-fib-fm350_gl.apk"
		download "$LUCI_PROTO_ATC_APK_URL" "$luci_pkg" "$LUCI_PROTO_ATC_APK_SHA256" || exit 1
		download "$ATC_FIB_FM350_APK_URL" "$fib_pkg" "$ATC_FIB_FM350_APK_SHA256" || exit 1
	fi
	log "installing luci-proto-atc and atc-fib-fm350_gl"
	pkg_install local "$luci_pkg" "$fib_pkg"
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
# fm350_find_at_device (files/usr/lib/fm350/at-port.sh): USB interface :1.6
# (0e8d:7127, mode 41, default) or :1.4 (0e8d:7126, mode 40), only on a USB
# device with idVendor 0e8d.
if device=$(fm350_find_at_device); then
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
		# Even though APNs are restricted above, escape sed replacement syntax
		# for every placeholder so '&', '\' or '|' cannot change the template.
		escaped=$(printf '%s' "$value" | sed 's/[\&|]/\\&/g')
		rendered=$(printf '%s\n' "$rendered" | sed "s|@${name}@|${escaped}|g")
	done
	if [ "$dry_run" -eq 1 ]; then
		log "[dry-run] uci batch from $(basename "$template"):"
		echo "$rendered"
	else
		# No -q: a failing batch must be visible. Its exit status is not
		# relied on (idempotent `delete`s of sections that don't exist yet
		# report "Entry not found", and whether that makes `uci batch` exit
		# non-zero is unverified), so its output is shown on failure and the
		# key results are verified explicitly afterwards (verify_uci).
		if ! batch_out=$(echo "$rendered" | uci batch 2>&1); then
			echo "install.sh: note: uci batch from $(basename "$template") reported:" >&2
			echo "$batch_out" >&2
		fi
	fi
}

# Aborts unless uci option $1 currently equals $2 (post-batch sanity check).
verify_uci() {
	[ "$dry_run" -eq 1 ] && return 0
	actual=$(uci -q get "$1" 2>/dev/null) || actual=""
	if [ "$actual" != "$2" ]; then
		echo "install.sh: verification failed: $1 is '$actual', expected '$2'" >&2
		exit 1
	fi
}

# Prints "set"/"add_list" uci batch commands that recreate section $2 of
# config $1 from `uci export`, optionally skipping the (space-separated)
# option names in $3. Lists stay lists. Empty output if the section is gone.
uci_section_to_batch() {
	uci -q export "$1" 2>/dev/null | awk -v cfg="$1" -v sect="$2" -v skip=" $3 " -v q="'" '
	$1 == "config" {
		on = ($3 == q sect q)
		if (on) print "set " cfg "." sect "=" $2
		next
	}
	on && ($1 == "option" || $1 == "list") {
		key = $2
		if (index(skip, " " key " ") > 0) next
		val = $0
		# POSIX classes, not "[ \t]": busybox awk may pass "\t" inside a
		# bracket expression to regcomp unescaped, where it means "\" or "t".
		sub(/^[[:space:]]*(option|list)[[:space:]]+[^[:space:]]+[[:space:]]+/, "", val)
		print (($1 == "list") ? "add_list " : "set ") cfg "." sect "." key "=" val
	}'
}

# Saves a pre-existing section the installer is about to replace, but only
# on the very first install (state section absent, $3 = 1) and only if it
# lacks our fm350_owned marker: that is the user's own section. The copy
# lives in $STATE_DIR and is restored by uninstall.sh. A section from an
# older installer version (state section already present) is ours, not
# the user's.
save_foreign_section() {
	cfg=$1
	sect=$2
	first=$3
	[ "$first" -eq 1 ] || return 0
	uci -q get "$cfg.$sect" >/dev/null 2>&1 || return 0
	[ "$(uci -q get "$cfg.$sect.fm350_owned" 2>/dev/null)" = 1 ] && return 0
	backup="$STATE_DIR/$cfg.$sect.batch"
	[ -f "$backup" ] && return 0
	if [ "$dry_run" -eq 1 ]; then
		log "[dry-run] would save your existing $cfg.$sect to $backup (uninstall.sh restores it)"
		return 0
	fi
	# The saved section may hold a SIM PIN or credentials: keep it root-only
	# (uci's own /etc/config files are 0600).
	(umask 077 && mkdir -p "$STATE_DIR" && uci_section_to_batch "$cfg" "$sect" >"$backup.tmp")
	chmod 700 "$STATE_DIR"
	if [ ! -s "$backup.tmp" ]; then
		rm -f "$backup.tmp"
		echo "install.sh: could not save existing $cfg.$sect, refusing to overwrite it" >&2
		exit 1
	fi
	mv "$backup.tmp" "$backup"
	log "saved your existing $cfg.$sect to $backup (uninstall.sh restores it)"
}

# Save an option's original value and its set/unset state exactly once. The
# state sections belong to this installer; they survive repeated installs and
# are removed by uninstall.sh after restoration.
save_option() {
	state=$1
	name=$2
	target=$3
	if uci -q get "$state.${name}_state" >/dev/null 2>&1; then
		return 0
	fi
	if original=$(uci -q get "$target" 2>/dev/null); then
		original_state='set'
	else
		original_state='unset'
	fi
	if [ "$dry_run" -eq 1 ]; then
		log "[dry-run] would save $target ($original_state) in $state"
		return 0
	fi
	uci set "$state.${name}_state=$original_state"
	[ "$original_state" = unset ] || uci set "$state.${name}_value=$original"
}

save_section() {
	state=$1
	name=$2
	target=$3
	if uci -q get "$state.${name}_state" >/dev/null 2>&1; then
		return 0
	fi
	if uci -q get "$target" >/dev/null 2>&1; then
		original_state=present
	else
		original_state=absent
	fi
	if [ "$dry_run" -eq 1 ]; then
		log "[dry-run] would save $target ($original_state) in $state"
	else
		uci set "$state.${name}_state=$original_state"
	fi
}

ensure_state_section() {
	state=$1
	if existing_type=$(uci -q get "$state" 2>/dev/null); then
		if [ "$existing_type" != fm350_install_state ]; then
			echo "install.sh: $state already exists with type '$existing_type'; refusing to overwrite it" >&2
			exit 1
		fi
	elif [ "$dry_run" -eq 1 ]; then
		log "[dry-run] would create $state if absent"
	else
		uci set "$state=fm350_install_state"
	fi
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
	policy=${2:-failover}
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
		log "[dry-run] would set mwan3.$section.use_policy='$policy'"
	else
		uci set "mwan3.$section.use_policy=$policy"
	fi
}

# Removes the stock default_rule_v6 (policy "balanced") when wan6 isn't
# enabled, after saving it for uninstall.sh. A rule we already redirected
# (fm350_orig_policy present) is left alone.
disable_mwan3_v6_default_rule() {
	uci -q get mwan3.default_rule_v6 >/dev/null 2>&1 || return 0
	uci -q get mwan3.default_rule_v6.fm350_orig_policy >/dev/null 2>&1 && return 0
	save_foreign_section mwan3 default_rule_v6 1
	if [ "$dry_run" -eq 1 ]; then
		log "[dry-run] would remove mwan3.default_rule_v6 (wan6 is not enabled)"
	else
		log "wan6 is not enabled: removing mwan3.default_rule_v6 so LAN IPv6 isn't blackholed (saved for uninstall.sh)"
		uci delete mwan3.default_rule_v6
	fi
}

# --- network ------------------------------------------------------------------
network_first=1
uci -q get network.fm350_install_state >/dev/null 2>&1 && network_first=0
ensure_state_section network.fm350_install_state
save_section network.fm350_install_state wan network.wan
save_option network.fm350_install_state wan_metric network.wan.metric
# A network.wwan the user created themselves (no fm350_owned marker) is saved
# to $STATE_DIR before it is replaced; uninstall.sh puts it back.
save_foreign_section network wwan "$network_first"
# Remember whether "wwan" was already in the wan zone before we touched it, so
# uninstall.sh only removes it again if we were the ones who added it.
if ! uci -q get network.fm350_install_state.wan_zone_wwan_state >/dev/null 2>&1; then
	if uci_list_contains "$wan_zone.network" wwan; then
		zone_state=present
	else
		zone_state=absent
	fi
	if [ "$dry_run" -eq 1 ]; then
		log "[dry-run] would record that wwan is $zone_state in $wan_zone.network"
	else
		uci set "network.fm350_install_state.wan_zone_wwan_state=$zone_state"
	fi
fi

# On a re-run, keep options the user added to our own wwan section (pincode,
# atc_debug, custom_at, pdp, ...): everything the template sets itself is
# refreshed, everything else is re-applied after the template. Only when the
# protocol is unchanged, since options are proto-specific.
wwan_preserved=""
if [ "$(uci -q get network.wwan.fm350_owned 2>/dev/null)" = 1 ] &&
	[ "$(uci -q get network.wwan.proto 2>/dev/null)" = "$proto" ]; then
	if [ "$dry_run" -eq 1 ]; then
		log "[dry-run] would preserve user-added options on the existing network.wwan"
	else
		if [ "$proto" = atc ]; then
			template_opts="fm350_owned proto device apn auth delay defaultroute peerdns metric"
		else
			template_opts="fm350_owned proto device apn auth delay metric"
		fi
		# Drop the section-type line: only real options count as "preserved".
		wwan_preserved=$(uci_section_to_batch network wwan "$template_opts" | grep -v '^set network\.wwan=') || wwan_preserved=""
	fi
fi

log "applying network config (proto=$proto, device=$device, apn=$apn)"
if [ "$proto" = atc ]; then
	apply_uci_template "$UCI_DIR/network-atc.uci" "DEVICE=$device" "APN=$apn"
else
	apply_uci_template "$UCI_DIR/network-xmm.uci" "DEVICE=$device" "APN=$apn"
fi
if [ -n "$wwan_preserved" ]; then
	log "preserving your extra options on network.wwan"
	if ! batch_out=$(printf '%s\n' "$wwan_preserved" | uci batch 2>&1); then
		echo "install.sh: re-applying your network.wwan options failed:" >&2
		echo "$batch_out" >&2
		exit 1
	fi
	uci commit network
fi
verify_uci network.wwan.proto "$proto"
verify_uci network.wwan.apn "$apn"
verify_uci network.wwan.device "$device"
verify_uci network.wwan.fm350_owned 1

# --- firewall -------------------------------------------------------------
log "applying firewall config (wan zone: $wan_zone)"
apply_uci_template "$UCI_DIR/firewall.uci" "WAN_ZONE=$wan_zone"
if [ "$dry_run" -eq 0 ] && ! uci_list_contains "$wan_zone.network" wwan; then
	echo "install.sh: verification failed: wwan is not in $wan_zone.network" >&2
	exit 1
fi

# --- mwan3 ----------------------------------------------------------------
if [ "$no_mwan3" -eq 1 ]; then
	log "--no-mwan3: skipping mwan3 config"
else
	mwan3_first=1
	uci -q get mwan3.fm350_install_state >/dev/null 2>&1 && mwan3_first=0
	ensure_state_section mwan3.fm350_install_state
	# mwan3 sections the user created under the names we own are saved
	# before being replaced; uninstall.sh restores them.
	for section in wwan wan_m1 wwan_m2 failover default; do
		save_foreign_section mwan3 "$section" "$mwan3_first"
	done
	save_section mwan3.fm350_install_state wan mwan3.wan
	save_section mwan3.fm350_install_state globals mwan3.globals
	for option in enabled family interval down up; do
		save_option mwan3.fm350_install_state "wan_$option" "mwan3.wan.$option"
	done
	# The installer adds mmx_mask only when it is unset or empty. Capture
	# that case, including an explicitly empty original value.
	if [ -z "$(uci -q get mwan3.globals.mmx_mask 2>/dev/null)" ]; then
		save_option mwan3.fm350_install_state globals_mmx_mask mwan3.globals.mmx_mask
	fi
	log "applying mwan3 config"
	apply_uci_template "$UCI_DIR/mwan3.uci"
	mwan3_add_wan_track_ip 1.1.1.1
	mwan3_add_wan_track_ip 9.9.9.9
	mwan3_set_mmx_mask_if_unset
	log "neutralizing stock mwan3 rules that would shadow ours (default_rule_v4, https), if present"
	neutralize_mwan3_rule default_rule_v4
	neutralize_mwan3_rule https
	# IPv6 (unverified on hardware): the stock default_rule_v6 uses the
	# "balanced" policy, which can blackhole LAN IPv6 when wan6 is disabled
	# (no usable IPv6 member). With wan6 enabled, pin it to the stock
	# wan-only policy; otherwise remove the rule (original saved for
	# uninstall.sh) so IPv6 follows the normal routing table.
	if [ "$(uci -q get mwan3.wan6.enabled 2>/dev/null)" = 1 ] &&
		uci -q get mwan3.wan_only >/dev/null 2>&1; then
		log "wan6 is enabled: pointing mwan3.default_rule_v6 at wan_only (IPv6 does not fail over to wwan)"
		neutralize_mwan3_rule default_rule_v6 wan_only
	else
		disable_mwan3_v6_default_rule
	fi
	verify_uci mwan3.failover.last_resort unreachable
	verify_uci mwan3.default.use_policy failover
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

# --- shared AT port lookup + status helper script ---------------------------
# at-port.sh is sourced by fm350-status and fm350-watchdog at run time.
if [ "$dry_run" -eq 1 ]; then
	log "[dry-run] would install $FILES_DIR/usr/lib/fm350/at-port.sh to /usr/lib/fm350/at-port.sh"
	log "[dry-run] would install $SCRIPT_DIR/fm350-status.sh to /usr/bin/fm350-status"
else
	mkdir -p /usr/lib/fm350
	cp "$FILES_DIR/usr/lib/fm350/at-port.sh" /usr/lib/fm350/at-port.sh
	chmod 0644 /usr/lib/fm350/at-port.sh
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
