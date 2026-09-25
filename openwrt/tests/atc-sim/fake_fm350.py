#!/usr/bin/env python3
"""fake_fm350.py - FM350-GL AT command emulator on a pty, for atc-test.sh.

Opens a pty, symlinks its slave device to --link, and drives the exact AT
dialogue atc.sh (mrhaav's atc-fib-fm350_gl proto handler) expects, so the
real, unmodified atc.sh can run against it start to finish. Response text
comes from responses.py (see that file for which values are copied from a
real no-SIM bench log vs. inferred from 3GPP 27.007 / atc.sh's own parsing).

AT sequence atc.sh drives (traced from atc-fib-fm350_gl_2025.08.24-r3's
proto_atc_setup, see openwrt/README.md for the package source):

  1. AT+CMEE=2                                   (verbose CME errors)
  2. AT+CPIN?                                    (SIM status gate)
  3. AT+CFUN=4                                   (flight mode on)
  4. ATI                                         (manufacturer/model gate)
  5. AT+CREG=0 / AT+CGREG=3 / AT+CEREG=3 / AT+C5GREG=3 / AT+CGEREP=2,1
  6. AT+EIAAPN="<apn>",0,"<pdp>","<pdp>",<auth>,"<user>","<pass>"
  7. AT+CGDCONT=1,"<pdp>","<apn>"
  8. AT+CTZR=1 / AT+CMGF=0 / AT+CSCS="GSM" / AT+CNMI=2,1
  9. AT+CFUN=1                                   (flight mode off, fire-and-
     forget via gcom's at.gcom; from here atc.sh reads raw URC lines off the
     tty in a `while read` loop instead of using gcom's request/response
     scripts, and drives the rest of the session purely off unsolicited
     result codes):
       -> +CEREG: 2                              (searching)
       -> +CEREG: 1,...                          (registered)
       -> +CTZV: ...                             (kicks off the operator
          name/PLMN dance, since re_connect==0)
 10. AT+COPS=3,0;+COPS?;+COPS=3,2;+COPS?         (fired off +CTZV)
       -> +COPS: 0,0,"<name>",<rat>              (format 0: operator name)
       -> +COPS: 0,2,"<plmn>",<rat>              (format 2: fires CGACT)
       -> OK
 11. AT+CGACT=1,1                                (fired by OK_received==1)
       -> +CGEV: ME PDN ACT ...                  (or +CME ERROR: ...)
       -> OK
 12. AT+CGPADDR=1                                (fired by OK_received==2)
       -> +CGPADDR: 1,"<ipv4>"
       -> OK
 13. AT+CGCONTRDP=1                              (fired by OK_received==3)
       -> +CGCONTRDP: 1,5,"<apn>","<addr>","<gw>","<dns1>","<dns2>",...
          (this line alone makes atc.sh call proto_init_update/
          proto_add_ipv4_address/proto_add_ipv4_route/proto_add_dns_server/
          proto_send_update - it does not wait for the trailing OK)
       -> OK

Scenarios (--scenario):
  ok           - full sequence above, SIM present, PDP activates cleanly.
  nosim        - AT+CPIN? returns the bench-log "SIM not inserted" CME
                 error; atc.sh aborts right after step 2, the sequence never
                 reaches step 3.
  cgact_error  - like "ok" through step 10, then AT+CGACT=1,1 gets a
                 "+CME ERROR: Requested service option not subscribed (#33)"
                 final result (no OK) instead of +CGEV/OK; atc.sh aborts
                 there (the one CME ERROR text it treats as fatal, see
                 responses.py).
  slow_boot    - like "ok", but the fake delays answering the *first*
                 command it receives (whichever one that is) by
                 --boot-silence seconds, simulating a modem that's slow to
                 wake up. Delaying relative to *when the first command
                 arrives* (not to process start time) matters because
                 atc.sh unconditionally sleeps its configured `delay` before
                 sending anything (atc.sh line ~216, "Modem boot delay") -
                 a start-time-relative silence window would just be
                 swallowed by that sleep and never be observed. Kept well
                 under gcom's own 25s per-command waitfor timeout (see
                 run_at.gcom), so atc.sh's "AT+CMEE=2 readiness" retry loop
                 (atc.sh lines ~226-231) is never exercised - see
                 atc-test.sh for why that loop is a bit of a red herring.

Every received command line is appended to --transcript, one per line, in
receipt order, for atc-test.sh to assert against.
"""
import argparse
import os
import pty
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import responses as R  # noqa: E402


def log(msg):
    print(f"fake_fm350: {msg}", file=sys.stderr, flush=True)


class FakeModem:
    def __init__(self, scenario, link_path, transcript_path, apn, pdp, boot_silence):
        self.scenario = scenario
        self.link_path = link_path
        self.transcript_path = transcript_path
        self.apn = apn
        self.pdp = pdp
        self.boot_silence = boot_silence
        self.booted = boot_silence <= 0
        self.master = None
        self._transcript = open(transcript_path, "a", buffering=1)

    # --- pty setup -----------------------------------------------------
    def open(self):
        master, slave = pty.openpty()
        self.master = master
        # Keep our own slave fd open for the process lifetime (never read or
        # written): with zero open slave fds, Linux fails os.read(master, ...)
        # with EIO immediately, even though gcom/ash will open the slave
        # path fresh for each command. Verified empirically in the
        # atc-explore Docker container.
        self.slave = slave
        slave_name = os.ttyname(slave)
        try:
            os.remove(self.link_path)
        except FileNotFoundError:
            pass
        os.symlink(slave_name, self.link_path)
        log(f"pty slave {slave_name} linked at {self.link_path}")
        return slave_name

    # --- wire helpers ----------------------------------------------------
    def send(self, text):
        os.write(self.master, text.encode())

    def echo_and_reply(self, cmd, body):
        # ATE1-style echo of the command line, then the response body
        # (already \r\n-framed by the caller).
        self.send(cmd + "\r\n" + body)

    def log_transcript(self, cmd):
        self._transcript.write(cmd + "\n")

    # --- main loop ---------------------------------------------------
    def run(self):
        buf = b""
        while True:
            try:
                data = os.read(self.master, 4096)
            except OSError:
                break
            if not data:
                break
            buf += data
            while b"\r" in buf:
                line, buf = buf.split(b"\r", 1)
                if buf[:1] == b"\n":
                    buf = buf[1:]
                text = line.decode(errors="replace").strip()
                if text:
                    self.on_command(text)

    def on_command(self, cmd):
        log(f"<- {cmd!r}")
        self.log_transcript(cmd)
        if not self.booted:
            # Delay relative to *receiving this (first) command*, not to
            # process start time: atc.sh unconditionally sleeps its
            # configured `delay` (15s, see uci/network-atc.uci) before
            # sending anything (atc.sh line ~216), which would otherwise
            # swallow a start-time-relative silence window before the first
            # command ever arrives and make --boot-silence a no-op.
            if self.boot_silence > 0:
                log(f"boot-silence: staying quiet {self.boot_silence:.1f}s before answering")
                time.sleep(self.boot_silence)
            self.booted = True
        self.dispatch(cmd)

    # --- AT dialogue ---------------------------------------------------
    def dispatch(self, cmd):
        u = cmd.upper()

        if u == "AT+CPIN?":
            if self.scenario == "nosim":
                self.echo_and_reply(cmd, f"\r\n{R.CPIN_NO_SIM}\r\n")
            else:
                self.echo_and_reply(cmd, "\r\n+CPIN: READY\r\n\r\nOK\r\n")
            return

        if u == "ATI":
            self.echo_and_reply(cmd, f"\r\n{R.ATI_REPLY}\r\nOK\r\n")
            return

        if u.startswith("AT+CGDCONT="):
            self.echo_and_reply(cmd, "\r\nOK\r\n")
            return

        if u.startswith("AT+EIAAPN="):
            self.echo_and_reply(cmd, "\r\nOK\r\n")
            return

        if u == "AT+CFUN=1":
            self.echo_and_reply(cmd, "\r\nOK\r\n")
            threading.Thread(target=self._after_cfun_on, daemon=True).start()
            return

        if u == "AT+COPS=3,0;+COPS?;+COPS=3,2;+COPS?":
            body = f"\r\n{R.COPS_FORMAT0}\r\n{R.COPS_FORMAT2}\r\nOK\r\n"
            self.echo_and_reply(cmd, body)
            return

        if u == "AT+CGACT=1,1":
            if self.scenario == "cgact_error":
                self.echo_and_reply(cmd, f"\r\n{R.CME_ERROR_SESSION_FAILED}\r\n")
            else:
                self.echo_and_reply(cmd, f"\r\n{R.CGEV_PDN_ACT}\r\nOK\r\n")
            return

        if u == "AT+CGPADDR=1":
            self.echo_and_reply(cmd, f'\r\n+CGPADDR: 1,"{R.FAKE_V4_ADDR}"\r\nOK\r\n')
            return

        if u == "AT+CGCONTRDP=1":
            line = (
                f'+CGCONTRDP: 1,5,"{self.apn}","{R.FAKE_V4_ADDR}.255.255.255.252",'
                f'"{R.FAKE_V4_GATEWAY}","{R.FAKE_DNS1}","{R.FAKE_DNS2}",'
                f'"0.0.0.0","0.0.0.0",0'
            )
            self.echo_and_reply(cmd, f"\r\n{line}\r\nOK\r\n")
            return

        # AT+CMEE=2, AT+CFUN=4, AT+CREG=0, AT+CGREG=3, AT+CEREG=3,
        # AT+C5GREG=3, AT+CGEREP=2,1, AT+CTZR=1, AT+CMGF=0, AT+CSCS="GSM",
        # AT+CNMI=2,1, and anything else atc.sh's run_at.gcom calls that we
        # don't special-case: plain OK.
        self.echo_and_reply(cmd, "\r\nOK\r\n")

    def _after_cfun_on(self):
        # Registration + operator-name URCs, spaced out like a real modem.
        time.sleep(0.2)
        self.send(f"\r\n{R.CEREG_SEARCHING}\r\n")
        time.sleep(0.2)
        self.send(f"\r\n{R.CEREG_REGISTERED_HOME}\r\n")
        time.sleep(0.2)
        self.send(f"\r\n{R.CTZV_URC}\r\n")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--scenario", required=True, choices=["ok", "nosim", "cgact_error", "slow_boot"])
    p.add_argument("--link", required=True, help="path to (re-)create as a symlink to the pty slave")
    p.add_argument("--transcript", required=True, help="file to append received AT command lines to")
    p.add_argument("--apn", default="internet.telekom")
    p.add_argument("--pdp", default="IP")
    p.add_argument("--boot-silence", type=float, default=0.0,
                    help="seconds to stay silent after opening before answering the first command "
                         "(used by the slow_boot scenario)")
    args = p.parse_args()

    modem = FakeModem(args.scenario, args.link, args.transcript, args.apn, args.pdp, args.boot_silence)
    modem.open()
    log(f"scenario={args.scenario} apn={args.apn} pdp={args.pdp} boot_silence={args.boot_silence}")
    modem.run()


if __name__ == "__main__":
    main()
