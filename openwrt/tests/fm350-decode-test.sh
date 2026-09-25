#!/bin/sh
# fm350-decode-test.sh - unit-tests fm350-status.sh's decoder (run_decode(),
# a pure awk program with no modem/gcom dependency) against canned AT
# responses, without a real modem. Runs on the development host under plain
# `sh` (no Docker needed): the decoder is POSIX awk, and this test needs
# nothing but that plus fm350-status.sh itself.
#
# Canned responses are the real sample dump from the task this test was
# written for (TAC/cell ID faked), covering: CEREG/C5GREG registration,
# COPS operator/RAT (including the AcT=13 "LTE-ENDC" case, which is off the
# end of the FM350 AT manual v2.10's own +COPS <AcT> table - see
# fm350-status.sh's rat_name() comment), CESQ signal quality, GTCCINFO
# serving cell (with a blank/sentinel-valued second row that must NOT be
# mistaken for a measured cell), and thermal sensor 1. A second, minimal
# dump exercises the "no cell measured" antenna hint.
set -e

SCRIPT_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
OPENWRT_DIR=$(CDPATH='' cd -- "$SCRIPT_DIR/.." && pwd)
STATUS_SH="$OPENWRT_DIR/fm350-status.sh"

status=0
fail() {
	echo "FAIL: $*" >&2
	status=1
}

echo "fm350-decode-test.sh: shellcheck"
if command -v shellcheck >/dev/null 2>&1; then
	shellcheck -s sh "$STATUS_SH" "$SCRIPT_DIR/fm350-decode-test.sh" || fail "shellcheck reported issues"
else
	echo "fm350-decode-test.sh: shellcheck not installed, skipping static check" >&2
fi

command -v awk >/dev/null 2>&1 || {
	echo "fm350-decode-test.sh: awk not found in PATH" >&2
	exit 1
}

FM350_STATUS_TEST=1
export FM350_STATUS_TEST
# shellcheck disable=SC1090,SC1091 # run_decode()/etc come from this sourced file
. "$STATUS_SH"

# --- canned dump: the task's real sample responses (TAC/cell ID faked) -----
sample_dump=$(
	cat <<'EOF'
@@Q ATI
Manufacturer: Fibocom Wireless Inc.
Model: FM350-GL
Revision: 81600.0000.00.29.20.22

@@Q AT+CPIN?
+CPIN: READY

@@Q AT+COPS?
+COPS:0,2,"26202",13

@@Q AT+CEREG?
+CEREG: 0,1

@@Q AT+C5GREG?
+C5GREG: 0

@@Q AT+CESQ
+CESQ: 17,99,255,255,4,29,75,52,57

@@Q AT+GTCCINFO?
+GTCCINFO:
1,4,262,2,1A2B,0012345AB,100,42,,,-7,29,29,4

2,4,,,FFFF,00FFFFFFF,9460,71,,43,43,10

@@Q AT+GTSENRDTEMP=1
+GTSENRDTEMP: 1,36500

@@Q AT+GTFCCEFFSTATUS?
+GTFCCEFFSTATUS: 0,1
EOF
)

decoded=$(printf '%s\n' "$sample_dump" | run_decode 0)
redacted=$(printf '%s\n' "$sample_dump" | run_decode 1)

# assert_match DESCRIPTION PATTERN TEXT
assert_match() {
	printf '%s\n' "$3" | grep -qE "$2" || fail "$1"
}
assert_no_match() {
	printf '%s\n' "$3" | grep -qE "$2" && fail "$1"
	:
}

echo "fm350-decode-test.sh: asserting decoded output"
assert_match "modem identity" '^Modem: Fibocom Wireless Inc\. FM350-GL$' "$decoded"
assert_match "CEREG: registered home network (stat=1)" \
	'^Registration \(CEREG/LTE\): registered, home network$' "$decoded"
assert_match "C5GREG: single-field (mode-only) response handled" \
	'^Registration \(C5GREG/5G NR\): reporting disabled \(mode 0\), no status reported$' "$decoded"
assert_match "COPS: numeric PLMN + AcT=13 decoded as LTE-ENDC" \
	'^Operator \(COPS\): 26202, RAT: LTE-ENDC$' "$decoded"
assert_match "CESQ: LTE RSRP -112 dBm (index 29)" 'RSRP: -112 dBm' "$decoded"
assert_match "CESQ: LTE RSRQ -18.0 dB (index 4)" 'RSRQ: -18\.0 dB' "$decoded"
assert_match "CESQ: LTE RSSI -94 dBm (index 17)" 'RSSI: -94 dBm' "$decoded"
assert_match "CESQ: UMTS fields unknown (255)" 'RSCP: unknown  EcNo: unknown' "$decoded"
assert_match "CESQ: NR SS-RSRP -105 dBm (index 52)" 'SS-RSRP: -105 dBm' "$decoded"
assert_match "CESQ: NR SS-RSRQ -6.0 dB (index 75)" 'SS-RSRQ: -6\.0 dB' "$decoded"
assert_match "CESQ: NR SS-SINR 5.0 dB (index 57)" 'SS-SINR: 5\.0 dB' "$decoded"
assert_match "GTCCINFO: serving cell decoded (mcc/mnc/tac/cell/earfcn/band/pci)" \
	'^Serving cell \(GTCCINFO\): LTE mcc=262 mnc=2 tac=1A2B cell=0012345AB earfcn=100 \(band 1\) pci=42$' "$decoded"
assert_match "GTCCINFO: band 1 derived from EARFCN 100 (band field blank)" 'band 1' "$decoded"
assert_match "GTCCINFO: RSSNR -3.5 dB (value -7)" 'RSSNR: -3\.5 dB' "$decoded"
assert_no_match "GTCCINFO: the sentinel (FFFF/00FFFFFFF) neighbour row must not appear" 'FFFF' "$decoded"
assert_no_match "GTCCINFO: no cell measured hint must NOT fire (a serving cell was found)" \
	'none measured' "$decoded"
assert_match "temperature: sensor 1, 36500 -> 36.5 C" '^Temperature \(sensor 1\): 36\.5 C$' "$decoded"
assert_match "FCC lock: mode=0 (no lock), status=1 (unlocked)" \
	'^FCC lock: mode=0 \(no lock\), status=unlocked$' "$decoded"

echo "fm350-decode-test.sh: asserting -x redaction"
assert_match "redacted: tac/cell masked" 'tac=\*\*\* cell=\*\*\*' "$redacted"
assert_match "redacted: mcc/mnc/earfcn/band/pci still shown" 'mcc=262 mnc=2.*earfcn=100 \(band 1\) pci=42' "$redacted"
assert_no_match "redacted: real TAC must not leak" '1A2B' "$redacted"
assert_no_match "redacted: real cell ID must not leak" '0012345AB' "$redacted"

# --- canned dump: no cell measured (empty GTCCINFO) -------------------------
nocell_dump=$(
	cat <<'EOF'
@@Q ATI
Manufacturer: Fibocom Wireless Inc.
Model: FM350-GL

@@Q AT+CEREG?
+CEREG: 0,2

@@Q AT+GTCCINFO?
+GTCCINFO:

EOF
)
nocell_decoded=$(printf '%s\n' "$nocell_dump" | run_decode 0)

echo "fm350-decode-test.sh: asserting the antenna hint"
assert_match "no cell measured: hint printed" 'none measured' "$nocell_decoded"
assert_match "no cell measured: antenna hint text" 'antenna pigtails' "$nocell_decoded"

# --- RSRP comes from <rsrp> (field 13), not <rxlev> (field 12) -------------
diverge_dump=$(
	cat <<'EOF'
@@Q AT+GTCCINFO?
+GTCCINFO:
1,4,262,2,1A2B,0012345AB,100,42,,,-7,50,29,4

EOF
)
diverge_decoded=$(printf '%s\n' "$diverge_dump" | run_decode 0)
echo "fm350-decode-test.sh: asserting RSRP uses the <rsrp> field"
assert_match "serving RSRP from field 13" 'RSRP: -112' "$diverge_decoded"

# --- -r -x: raw GTCCINFO rows are redacted too ------------------------------
raw_redacted=$(printf '%s\n' '+GTCCINFO:' '1,4,262,2,1A2B,0012345AB,100,42,,,-7,29,29,4' '2,4,,,C3D4,0098765CD,6300,207,,43,43,10' | redact_raw)
echo "fm350-decode-test.sh: asserting raw-mode redaction"
assert_no_match "raw redaction: serving TAC" '1A2B' "$raw_redacted"
assert_no_match "raw redaction: serving cell ID" '0012345AB' "$raw_redacted"
assert_no_match "raw redaction: neighbour TAC" 'C3D4' "$raw_redacted"
assert_no_match "raw redaction: neighbour cell ID" '0098765CD' "$raw_redacted"
assert_match "raw redaction: EARFCN kept" '6300,207' "$raw_redacted"

if [ "$status" -eq 0 ]; then
	echo "fm350-decode-test.sh: PASS"
else
	echo "fm350-decode-test.sh: FAIL, see output above" >&2
fi

exit "$status"
