#!/bin/sh
# fm350-status.sh - read-only status dump for the Fibocom FM350-GL.
#
# Finds the AT command tty from sysfs, then runs a fixed set of read-only AT
# queries through comgt's `gcom` and prints the results, decoded into
# human-readable values by default. Never sends anything that changes modem
# state (no CFUN, no CGACT, no CGDCONT, no FCC unlock).
#
# Usage: fm350-status.sh [-r] [-x] [/dev/ttyUSBn]
#   -r    Raw mode: print the AT responses as-is, undecoded (the old
#         default behaviour), instead of the decoded summary.
#   -x    Redact mode: mask the TAC and cell ID of every cell, in both the
#         decoded summary and (with -r) the raw GTCCINFO rows.
#   If the device is omitted, it is auto-detected from sysfs (USB interface
#   :1.6 for 0e8d:7127 mode 41, :1.4 for 0e8d:7126 mode 40).
#
# Decoding: registration (CEREG/C5GREG), operator/RAT (COPS), signal quality
# (CESQ, 3GPP TS 27.007 11.1.2) and serving cell (GTCCINFO) are decoded
# using the formulas/tables in the Fibocom FM350 AT Commands manual v2.10;
# each decode function below cites the section it's derived from. LTE
# band-from-EARFCN uses 3GPP TS 36.101 table 5.7.3-1, common EU/global bands
# only (1, 3, 7, 8, 20, 28, 32, 38); anything else prints "?". The thermal
# sensor reading's unit isn't specified beyond "integer type" in the manual;
# it's treated as millidegree Celsius (matches this firmware family's
# community-reported values), which is inferred, not manual-confirmed.
#
# The decoder itself (run_decode(), an awk program fed a "@@Q <command>"
# tagged transcript on stdin) has no modem/gcom dependency, so
# tests/fm350-decode-test.sh sources this file with FM350_STATUS_TEST=1 (to
# skip the "$@"-driven main() call at the bottom) and feeds it canned AT
# responses directly. POSIX sh/ash (busybox) compatible; awk is busybox awk.
set -e

raw_mode=0
redact=0

usage() {
	cat <<'EOF'
Usage: fm350-status.sh [-r] [-x] [/dev/ttyUSBn]
  -r    Raw mode: print the AT responses as-is, undecoded.
  -x    Redact mode: mask the TAC and cell ID (also in raw mode).
EOF
}

while [ $# -gt 0 ]; do
	case "$1" in
	-r)
		raw_mode=1
		shift
		;;
	-x)
		redact=1
		shift
		;;
	-h | --help)
		usage
		exit 0
		;;
	-*)
		echo "fm350-status.sh: unknown option: $1" >&2
		usage >&2
		exit 1
		;;
	*)
		break
		;;
	esac
done
device_arg=$1

# The AT tty lookup (USB interface :1.6 for 0e8d:7127 mode 41, else :1.4 for
# 0e8d:7126 mode 40, vendor 0e8d only) is shared with install.sh and
# fm350-watchdog: /usr/lib/fm350/at-port.sh when installed, else the copy
# next to this script in the repo checkout. Loaded lazily by main() so
# sourcing this file for the decoder tests needs neither.
load_at_port_lib() {
	for lib in /usr/lib/fm350/at-port.sh "$(dirname "$0")/files/usr/lib/fm350/at-port.sh"; do
		if [ -r "$lib" ]; then
			# shellcheck source=/dev/null # files/usr/lib/fm350/at-port.sh
			. "$lib"
			return 0
		fi
	done
	echo "fm350-status.sh: at-port.sh not found (expected /usr/lib/fm350/at-port.sh); pass the device explicitly, e.g. /dev/ttyUSB4" >&2
	return 1
}

# Warn if netifd's wwan is up or connecting: atc.sh then owns the same tty and
# our AT replies may be stolen by it (and its replies by us).
warn_if_wwan_active() {
	command -v ifstatus >/dev/null 2>&1 || return 0
	st=$(ifstatus wwan 2>/dev/null) || return 0
	up=$(printf '%s' "$st" | jsonfilter -e '@.up' 2>/dev/null) || up=""
	pending=$(printf '%s' "$st" | jsonfilter -e '@.pending' 2>/dev/null) || pending=""
	if [ "$up" = "true" ] || [ "$pending" = "true" ]; then
		echo "fm350-status.sh: WARNING: interface wwan is up or connecting; atc.sh shares this tty, so replies below may be incomplete or stolen. Use 'ifdown wwan' first for a clean read." >&2
	fi
}

# --- decoder: pure awk, no modem/gcom dependency -----------------------------
# Reads a "@@Q <AT command>" tagged transcript (one query's full raw
# response follows each tag line, up to the next tag) from stdin, prints the
# decoded summary to stdout. $1 (passed as awk -v redact=) is "1"/"0".
run_decode() {
	# busybox mktemp requires the template to *end* in XXXXXX (no suffix
	# after it) - a suffixed template like "...XXXXXX.awk" errors out with
	# "Invalid argument" on real OpenWrt/busybox, verified in the same
	# openwrt/rootfs image tests/docker-test.sh uses.
	awk_script=$(mktemp "${TMPDIR:-/tmp}/fm350-status-awk.XXXXXX") || {
		echo "fm350-status.sh: mktemp failed" >&2
		return 1
	}
	cat >"$awk_script" <<'AWK'
function reg_stat_name(s) {
	# 3GPP TS 27.007 10.1.20 (+CEREG) <stat>; reused for +C5GREG (not in the
	# v2.10 manual - inferred from the same 27.007 <stat> family, values 0-5
	# are the ones this modem is actually seen to return).
	if (s == 0) return "not registered, not searching"
	if (s == 1) return "registered, home network"
	if (s == 2) return "not registered, searching"
	if (s == 3) return "registration denied"
	if (s == 4) return "unknown (e.g. out of coverage)"
	if (s == 5) return "registered, roaming"
	if (s == 6) return "registered, SMS only (home)"
	if (s == 7) return "registered, SMS only (roaming)"
	if (s == 8) return "attached, emergency bearer only"
	if (s == 9) return "registered, CSFB not preferred (home)"
	if (s == 10) return "registered, CSFB not preferred (roaming)"
	return "unknown(" s ")"
}
function rat_name(n) {
	# Manual v2.10's +COPS <AcT> table only goes up to 12 (NB-IoT) and has no
	# 5G-NR/ENDC value, yet this modem answers COPS AcT with 13 for LTE-NR
	# dual connectivity in practice. atc.sh's own nb_rat() (reverse-engineered
	# from the same firmware family, see atc-fib-fm350_gl/files/lib/netifd/
	# proto/atc.sh) uses this exact table for its generic "rat" numbering
	# (CxREG rat field and, empirically, COPS AcT alike), so it's reused here.
	if (n == 0 || n == 1 || n == 3) return "GSM"
	if (n == 2 || n == 4 || n == 5 || n == 6) return "WCDMA"
	if (n == 7) return "LTE"
	if (n == 11) return "NR"
	if (n == 13) return "LTE-ENDC"
	return "unknown(" n ")"
}
# --- CESQ decode, 3GPP TS 27.007 11.1.2 (FM350 manual v2.10 section 11.1.2) -
function rssi_dbm(i) {
	if (i == 99) return "unknown"
	if (i == 0) return "<-110 dBm"
	if (i >= 1 && i <= 63) return (i - 111) " dBm"
	return "n/a"
}
function rscp_dbm(i) {
	if (i == 255) return "unknown"
	if (i == 0) return "<-120 dBm"
	if (i >= 1 && i <= 96) return (i - 121) " dBm"
	return "n/a"
}
function ecno_db(i) {
	if (i == 255) return "unknown"
	if (i == 0) return "<-24 dB"
	if (i >= 1 && i <= 49) return sprintf("%.1f dB", -24 + (i - 1) * 0.5)
	return "n/a"
}
function rsrq_db(i) {
	if (i == 255) return "unknown"
	if (i == 0) return "<-19.5 dB"
	if (i >= 1 && i <= 34) return sprintf("%.1f dB", -19.5 + (i - 1) * 0.5)
	return "n/a"
}
function rsrp_dbm(i) {
	if (i == 255) return "unknown"
	if (i == 0) return "<-140 dBm"
	if (i >= 1 && i <= 97) return (i - 141) " dBm"
	return "n/a"
}
function ssrsrq_db(i) {
	if (i == 255) return "unknown"
	if (i == 0) return "<-43 dB"
	if (i >= 1 && i <= 126) return sprintf("%.1f dB", -43 + (i - 1) * 0.5)
	return "n/a"
}
function ssrsrp_dbm(i) {
	if (i == 255) return "unknown"
	if (i == 0) return "<-156 dBm"
	if (i >= 1 && i <= 126) return (i - 157) " dBm"
	return "n/a"
}
function sssinr_db(i) {
	if (i == 255) return "unknown"
	if (i == 0) return "<-23 dB"
	if (i >= 1 && i <= 127) return sprintf("%.1f dB", -23 + (i - 1) * 0.5)
	return "n/a"
}
# 3GPP TS 36.101 table 5.7.3-1 (E-UTRA channel numbers), common EU/global
# bands only; anything outside these ranges prints "?" rather than guessing.
function lte_band(e) {
	if (e >= 0 && e <= 599) return "1"
	if (e >= 600 && e <= 1199) return "2"
	if (e >= 1200 && e <= 1949) return "3"
	if (e >= 2750 && e <= 3449) return "7"
	if (e >= 3450 && e <= 3799) return "8"
	if (e >= 6150 && e <= 6449) return "20"
	if (e >= 9210 && e <= 9659) return "28"
	if (e >= 9770 && e <= 9869) return "30"
	if (e >= 9920 && e <= 10359) return "32"
	if (e >= 37750 && e <= 38249) return "38"
	return "?"
}
function redact_val(v) {
	return redact ? "***" : v
}
# 17.3 +GTFCCEFFSTATUS <effective mode value>/<unlock status value>.
function fcc_mode_name(m) {
	if (m == 0) return "no lock"
	if (m == 1) return "one-time unlock"
	if (m == 2) return "power-up unlock (unlock needed every boot)"
	return "unknown(" m ")"
}
function fcc_unlock_name(u) {
	if (u == 0) return "locked"
	if (u == 1) return "unlocked"
	return "unknown(" u ")"
}
BEGIN {
	section = ""
	serving_found = 0
}
/^@@Q / {
	section = $0
	sub(/^@@Q /, "", section)
	next
}
section == "ATI" && /Manufacturer:/ {
	manufacturer = $0
	sub(/^.*Manufacturer: */, "", manufacturer)
	sub(/\r.*$/, "", manufacturer)
}
section == "ATI" && /Model:/ {
	model = $0
	sub(/^.*Model: */, "", model)
	sub(/\r.*$/, "", model)
	if (manufacturer != "") printf "Modem: %s %s\n", manufacturer, model
}
section == "AT+CEREG?" && /\+CEREG:/ {
	line = $0
	sub(/^.*\+CEREG:[ ]*/, "", line)
	n = split(line, f, ",")
	if (n >= 2) printf "Registration (CEREG/LTE): %s\n", reg_stat_name(f[2] + 0)
	else if (n == 1) printf "Registration (CEREG/LTE): reporting disabled (mode %s)\n", f[1]
}
section == "AT+C5GREG?" && /\+C5GREG:/ {
	line = $0
	sub(/^.*\+C5GREG:[ ]*/, "", line)
	n = split(line, f, ",")
	if (n >= 2) printf "Registration (C5GREG/5G NR): %s\n", reg_stat_name(f[2] + 0)
	else if (n == 1) printf "Registration (C5GREG/5G NR): reporting disabled (mode %s), no status reported\n", f[1]
}
section == "AT+COPS?" && /\+COPS:/ {
	line = $0
	gsub(/"/, "", line)
	sub(/^.*\+COPS:[ ]*/, "", line)
	n = split(line, f, ",")
	if (n >= 2) {
		oper = (n >= 3) ? f[3] : ""
		act = (n >= 4) ? f[4] + 0 : -1
		ratstr = (act >= 0) ? rat_name(act) : "unknown"
		if (oper != "") printf "Operator (COPS): %s, RAT: %s\n", oper, ratstr
		else printf "Operator (COPS): unknown, RAT: %s\n", ratstr
	}
}
section == "AT+CESQ" && /\+CESQ:/ {
	line = $0
	sub(/^.*\+CESQ:[ ]*/, "", line)
	n = split(line, f, ",")
	if (n >= 9) {
		print "Signal (CESQ):"
		printf "  LTE  RSSI: %s  RSRP: %s  RSRQ: %s\n", rssi_dbm(f[1] + 0), rsrp_dbm(f[6] + 0), rsrq_db(f[5] + 0)
		printf "  UMTS RSCP: %s  EcNo: %s\n", rscp_dbm(f[3] + 0), ecno_db(f[4] + 0)
		printf "  NR   SS-RSRP: %s  SS-RSRQ: %s  SS-SINR: %s\n", ssrsrp_dbm(f[8] + 0), ssrsrq_db(f[7] + 0), sssinr_db(f[9] + 0)
	}
}
section == "AT+GTCCINFO?" {
	line = $0
	gsub(/^[ \t]+|[ \t\r]+$/, "", line)
	if (line == "" || line !~ /^[0-9]/) next
	n = split(line, f, ",")
	is_service = f[1] + 0
	rat = f[2] + 0
	if (is_service != 1) next # only the serving cell, skip neighbour rows
	if (rat == 4 && n == 14) {
		# LTE/eMTC/NB-IoT serving cell (GTCCINFO section 2, manual v2.10
		# 11.1.15): IsServiceCell,rat,mcc,mnc,tac,cellid,earfcn,physicalCellId,
		# band,bandwidth,rssnr_value,rxlev,rsrp,rsrq. <rxlev> for an LTE cell
		# is defined in the same table as <rsrp> (it looks like a copy of it
		# and matches it in every sample we have); we decode <rsrp> itself,
		# like fm350mac's cellinfo.py. <band> is often
		# blank (as here), hence the EARFCN fallback via lte_band().
		mcc = f[3]; mnc = f[4]; tac = f[5]; cellid = f[6]; earfcn = f[7] + 0
		pci = f[8]; band = f[9]; rssnr = f[11] + 0; rsrp = f[13] + 0; rsrq = f[14] + 0
		if (band == "") band = lte_band(earfcn)
		printf "Serving cell (GTCCINFO): LTE mcc=%s mnc=%s tac=%s cell=%s earfcn=%s (band %s) pci=%s\n", \
			mcc, mnc, redact_val(tac), redact_val(cellid), earfcn, band, pci
		printf "  RSRP: %s  RSRQ: %s  RSSNR: %s dB\n", rsrp_dbm(rsrp), rsrq_db(rsrq), rssnr / 2.0
		serving_found = 1
	} else {
		# Same masking as redact_raw() below: fields 5 (TAC) and 6 (cell ID).
		rawline = line
		if (redact && n >= 6) {
			rawline = f[1]
			for (i = 2; i <= n; i++) rawline = rawline "," ((i == 5 || i == 6) ? "REDACTED" : f[i])
		}
		printf "Serving cell (GTCCINFO): rat=%s (decoding only implemented for LTE; raw: %s)\n", rat_name(rat), rawline
		serving_found = 1
	}
}
section == "AT+GTSENRDTEMP=1" && /\+GTSENRDTEMP:/ {
	line = $0
	sub(/^.*\+GTSENRDTEMP:[ ]*/, "", line)
	n = split(line, f, ",")
	if (n >= 2) {
		# <current_temperature> unit isn't specified beyond "integer type" in
		# the v2.10 manual; treated as millidegree Celsius, matching this
		# firmware family's community-reported values - inferred, not
		# manual-confirmed.
		printf "Temperature (sensor %s): %.1f C\n", f[1], f[2] / 1000.0
	}
}
section == "AT+GTFCCEFFSTATUS?" && /\+GTFCCEFFSTATUS:/ {
	line = $0
	sub(/^.*\+GTFCCEFFSTATUS:[ ]*/, "", line)
	n = split(line, f, ",")
	if (n >= 2) printf "FCC lock: mode=%s (%s), status=%s\n", f[1], fcc_mode_name(f[1] + 0), fcc_unlock_name(f[2] + 0)
}
END {
	if (!serving_found) {
		print "Serving cell (GTCCINFO): none measured."
		print "  Hint: check the antenna pigtails/connections first."
	}
}
AWK
	rc=0
	awk -v redact="$1" -f "$awk_script" || rc=$?
	rm -f "$awk_script"
	return "$rc"
}

# Masks the TAC and cell ID (fields 5 and 6) of +GTCCINFO cell rows, for -r -x.
redact_raw() {
	sed -E 's/^([12],[0-9]+,[^,]*,[^,]*,)[^,]*,[^,]*,/\1REDACTED,REDACTED,/'
}

main() {
	if ! command -v gcom >/dev/null 2>&1; then
		echo "fm350-status.sh: gcom not found, install comgt: opkg install comgt (or apk add comgt)" >&2
		exit 1
	fi

	if [ -n "$device_arg" ]; then
		device=$device_arg
	elif load_at_port_lib && device=$(fm350_find_at_device); then
		:
	else
		echo "fm350-status.sh: could not find the FM350 AT tty in sysfs; pass it explicitly, e.g.:" >&2
		echo "  fm350-status.sh /dev/ttyUSB4" >&2
		exit 1
	fi

	if [ ! -c "$device" ]; then
		echo "fm350-status.sh: $device is not a character device" >&2
		exit 1
	fi

	echo "AT device: $device"
	warn_if_wwan_active
	echo

	# See run_decode()'s comment above: busybox mktemp needs the template to
	# end in XXXXXX, no suffix after it.
	gcom_script=$(mktemp "${TMPDIR:-/tmp}/fm350-status-gcom.XXXXXX") || {
		echo "fm350-status.sh: mktemp failed" >&2
		exit 1
	}
	trap 'rm -f "$gcom_script"' EXIT
	trap 'rm -f "$gcom_script"; exit 130' INT
	trap 'rm -f "$gcom_script"; exit 143' TERM

	# Send $COMMAND, collect whatever the modem prints back for 2s. Modelled on
	# mrhaav's atc-fib-fm350_gl /etc/gcom/getrun_at.gcom, which uses the same
	# fixed collection window instead of matching on a terminator string.
	cat >"$gcom_script" <<'EOF'
opengt
 set com 115200n81
 set senddelay 0.02
 waitquiet 1 0.2
 flash 0.1

:start
 send $env("COMMAND")
 send "^m"
 get 2 "" $s
 print $s

:continue
 exit 0
EOF

	run_at() {
		COMMAND="$1" gcom -d "$device" -s "$gcom_script" 2>&1
	}

	# Read-only queries only: identity, SIM/registration state, signal
	# quality, serving cell, temperature and FCC-lock status. Must never
	# include CFUN=, CGACT=, CGDCONT=, or an FCC unlock command.
	queries="ATI
AT+CPIN?
AT+COPS?
AT+CEREG?
AT+C5GREG?
AT+CESQ
AT+GTCCINFO?
AT+GTSENRDTEMP=1
AT+GTFCCEFFSTATUS?"

	if [ "$raw_mode" -eq 1 ]; then
		for q in $queries; do
			echo "== $q =="
			out=$(run_at "$q") || {
				echo "(query failed: $q)" >&2
				continue
			}
			if [ "$redact" -eq 1 ]; then
				printf '%s\n' "$out" | redact_raw
			else
				printf '%s\n' "$out"
			fi
			echo
		done
		return 0
	fi

	dump=""
	for q in $queries; do
		out=$(run_at "$q") || {
			echo "(query failed: $q)" >&2
			continue
		}
		dump="$dump
@@Q $q
$out"
	done
	printf '%s\n' "$dump" | run_decode "$redact"
}

[ -n "$FM350_STATUS_TEST" ] || main "$@"
