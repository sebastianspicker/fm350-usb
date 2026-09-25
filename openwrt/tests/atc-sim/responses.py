"""Golden fixture: FM350-GL AT responses used by fake_fm350.py.

Every constant below cites its source:

  - "bench-log" comments mean the string is copied verbatim from a real FM350-GL
    on the bench, no SIM inserted (../../../docs/bench-log.md, 2026-09-25 entry).
  - "at-commands" comments mean the format follows ../../../docs/at-commands.md
    (Fibocom FM350 AT Commands User Manual V2.10 cheat sheet) or the plain
    3GPP TS 27.007 grammar, since the bench had no SIM and these were never
    captured from real hardware. These are best-effort/inferred, not measured.
  - "atc.sh" comments mean the exact field layout was reverse-engineered from
    how atc-fib-fm350_gl/files/lib/netifd/proto/atc.sh parses the line (awk
    field positions), since that's the contract our fake must satisfy for the
    handler to progress, independent of what any real modem sends.

AT command line framing: \\r\\n, command echo on (ATE1 default; verified
empirically in the atc-explore Docker container that comgt's gcom scripts
tolerate/ignore the echo line), final result code OK or +CME ERROR: <text>.
"""

# --- bench-log: docs/bench-log.md, "AT+CPIN?" row, no SIM inserted ---------
CPIN_NO_SIM = '+CME ERROR: SIM not inserted'

# --- bench-log: docs/bench-log.md, "ATI / AT+CGMR" row ----------------------
# Manufacturer/Model lines are the standard 3GPP ATI fields; atc.sh greps for
# them by exact string ("Fibocom Wireless Inc." / "FM350-GL", atc.sh lines
# ~296-303). Revision string (firmware 29.20.22) is the bench-confirmed value.
ATI_REPLY = (
    'Manufacturer: Fibocom Wireless Inc.\r\n'
    'Model: FM350-GL\r\n'
    'Revision: 81600.0000.00.29.20.22\r\n'
)

# --- at-commands: SIM present, registered home network (inferred, no SIM on
# the bench to confirm). atc.sh only reads field 1 (status) and, via CxREG(),
# fields 2/3/4 (tac/cellid/rat) for a human-readable log line; format/values
# otherwise don't affect control flow. rat=7 is LTE per atc.sh's nb_rat().
CEREG_SEARCHING = '+CEREG: 2'
CEREG_REGISTERED_HOME = '+CEREG: 1,"1A2B","0102030",7'

# --- at-commands: 3GPP +CTZV network time zone URC, sent because atc.sh
# enables it with AT+CTZR=1. atc.sh only reads the URC name (":" prefix), not
# the value, to kick off its "+COPS=3,0;...;+COPS?" operator-name dance
# (atc.sh lines ~660-667), so the payload format is cosmetic here.
CTZV_URC = '+CTZV: "26/09/25,14:30:00+32,0"'

# --- inferred: AT+COPS=3,0;+COPS?;+COPS=3,2;+COPS?, one chained command that
# atc.sh sends after +CTZV (see above). Two +COPS: read results are expected:
# format 0 (long alphanumeric operator name) then format 2 (numeric PLMN),
# both rat=7/LTE (atc.sh's nb_rat mapping). atc.sh parses field 2 (format)
# and field 3 (name/plmn) plus field 4 (rat) via awk -F ',' (atc.sh
# "+COPS )" case, ~lines 585-597).
COPS_FORMAT0 = '+COPS: 0,0,"Telekom.de",7'
COPS_FORMAT2 = '+COPS: 0,2,"26201",7'

# --- at-commands / 27.007: +CGEV unsolicited PDP activation notice, sent
# before the final OK of AT+CGACT=1,1 (this ordering, URC-before-OK, is what
# lets atc.sh's OK_received state machine chain the next command - atc.sh
# lines ~604-631). Exact text only needs the "ME PDN ACT" prefix atc.sh
# matches (case '+CGEV' ) block).
CGEV_PDN_ACT = '+CGEV: ME PDN ACT 1'

# --- 27.007 §10.1.23: the two +CME ERROR strings atc.sh gives special
# handling (atc.sh lines ~528-538). "Requested service option not subscribed
# (#33)" is the literal 3GPP #33 cause text and is the only generic CME ERROR
# string that makes atc.sh abort the interface (proto_notify_error
# SESSION_FAILED + proto_block_restart + return 1); any other CME ERROR text
# during CGACT is only logged (if atc_debug>=1) and otherwise ignored - see
# the "cgact_error" scenario note in atc-test.sh for why this matters.
CME_ERROR_SESSION_FAILED = '+CME ERROR: Requested service option not subscribed (#33)'

# --- 27.007 §10.1.20 +CGPADDR: the IPv4 address handed to the session. Fake,
# CGNAT-range value (no SIM/data session was ever exercised on the bench).
# atc.sh subnet_calc(<addr>) derives netmask+gateway from the last octet
# using bit tricks (atc.sh lines ~63-83); for .45 that works out to a /30
# with gateway .46 - see atc-test.sh's assertion comments for the worked
# arithmetic this fixture depends on. Keep FAKE_V4_ADDR's last octet at 45
# (or recompute the expected netmask/gateway in atc-test.sh if it changes).
FAKE_V4_ADDR = '10.64.23.45'
FAKE_V4_GATEWAY = '10.64.23.46'
FAKE_V4_NETMASK_PREFIXLEN = '30'

# --- 27.007 §10.1.23 +CGCONTRDP: cid,bearer_id,APN,local_addr/mask,gw_addr,
# DNS_prim,DNS_sec[,PCSCF_prim,PCSCF_sec,IM_CN_flag]. atc.sh reads field 3
# (APN) and fields 6/7 (DNS, atc.sh lines ~634-645) from a $URCvalue that's
# already had every '"' character stripped globally, for every URC type,
# near the top of the read loop (atc.sh: `URCvalue=$(echo $URCvalue | sed
# -e 's/"//g' | ...)`, well before the per-command `case` dispatch) - so
# whether or not the real FM350 quotes these fields doesn't matter; this
# fixture quotes them (matching the 3GPP grammar) purely for readability,
# and atc.sh strips them before proto_add_dns_server ever sees them.
# Verified empirically (atc-test.sh's "ok" notify.log assertion checks for
# clean "8.8.8.8"/"8.8.4.4" values, not quote-wrapped ones).
FAKE_DNS1 = '8.8.8.8'
FAKE_DNS2 = '8.8.4.4'
