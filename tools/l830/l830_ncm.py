#!/usr/bin/env python3
"""Fibocom L830-EB (USB mode 0) data session over NCM: the AT side, no sudo.

  l830_ncm.py up [--apn APN]   define cid 2, activate it, bind it to NCM 0 (en7), write state/l830_ncm_session.json
  l830_ncm.py down             deactivate cid 2
  l830_ncm.py status           read-only: CPIN, CFUN, CEREG, COPS, CGACT, CGCONTRDP, XCESQ
  l830_ncm.py selftest

Sequence adapted from mrhaav/openwrt atc-fib-l8x0_gl (L850/L860, profile 0). The L830 firmware only
accepts cid 1-20 and keeps the network's IMS bearer on cid 1, so cid 2 is used:
  AT+CGDCONT=2,"IP",<apn>; AT+XDNS=2,1; AT+CGACT=1,2; AT+CGCONTRDP=2;
  AT+XDATACHANNEL=1,1,"/USBCDC/0","/USBHS/NCM/0",2,2; AT+CGDATA="M-RAW_IP",2  (-> CONNECT)
Down: AT+CGACT=0,2 (-> OK, NO CARRIER). The L850 teardown (AT+XDATACHANNEL=0, AT+CGDATA=0) is not
supported on the L830. The macOS side (address, route, firewall) is l830_net.sh.
Never sends AT+XLCSLSTR?, AT+XLCSAETTA? or AT+XIPCONFIG?: they crash this firmware.
"""

import argparse
import ipaddress
import json
import os
import re
import select
import sys
import time

from l830_common import STATE, log, open_at_port

SESSION = STATE / "l830_ncm_session.json"
LOGNAME = "l830_ncm.log"
CID = 2


def subnet_calc(ip: str) -> tuple[int, str]:
    """Smallest subnet in which ip is neither network nor broadcast address; gateway = the other low host.

    Same rule as mrhaav's subnet_calc (atc.sh)."""
    d = int(ip.split(".")[3])
    x, y = 1, 4
    while d % y in (0, y - 1):
        x, y = x + 1, y * 2
    gw = (d // y) * y + (2 if d % y == 1 else 1)
    return 31 - x, ".".join(ip.split(".")[:3] + [str(gw)])


def parse_contrdp(out: str):
    """(ip, reported gateway or None, [dns]) from +CGCONTRDP for CID, or None."""
    m = re.search(r'\+CGCONTRDP:\s*%d,\d+,"[^"]*","([^"]*)"(?:,"([^"]*)")?(?:,"([^"]*)")?(?:,"([^"]*)")?' % CID, out)
    if not m:
        return None
    ip = ".".join(m.group(1).split(".")[:4])  # "a.b.c.d.m.m.m.m": address plus mask
    ipaddress.IPv4Address(ip)  # raises on garbage
    gw = m.group(2) if m.group(2) and m.group(2).count(".") == 3 else None
    dns = [d for d in m.groups()[2:] if d and d.count(".") == 3]
    return ip, gw, dns


def selftest() -> None:
    assert subnet_calc("10.0.0.1") == (30, "10.0.0.2")
    assert subnet_calc("10.0.0.2") == (30, "10.0.0.1")
    assert subnet_calc("10.0.0.4") == (29, "10.0.0.1")  # .4 is a /30 network address -> /29
    assert subnet_calc("10.0.0.7") == (28, "10.0.0.1")  # .7 is a broadcast in /30 and /29 -> /28
    assert subnet_calc("100.64.12.250") == (30, "100.64.12.249")
    real = ('+CGCONTRDP: 2,6,"internet.telekom.mnc001.mcc262.gprs","10.25.136.192.255.0.0.0","10.25.136.193",'
            '"10.74.210.210","10.74.210.211","0.0.0.0","0.0.0.0",0')  # 2026-11-08 17:45
    assert parse_contrdp(real) == ("10.25.136.192", "10.25.136.193", ["10.74.210.210", "10.74.210.211"])
    assert parse_contrdp("+CGCONTRDP: 1,5,\"ims\",\"1.2.3.4\"") is None  # other cid
    print("selftest ok")


def ask(port, cmd: str, timeout: float = 10.0, must_ok: bool = True) -> str:
    out = port.command(cmd, timeout=timeout)
    log(LOGNAME, f">>> {cmd}\n{out}")
    if must_ok and not re.search(r"(?m)^OK\s*$", out):
        sys.exit(f"{cmd} did not return OK; stopping")
    return out


def wait_connect(port, cmd: str, timeout: float = 30.0) -> str:
    """AT+CGDATA answers CONNECT, which fm350mac's final-result regex does not know."""
    os.write(port.fd, (cmd + "\r").encode())
    buf, end = "", time.monotonic() + timeout
    while time.monotonic() < end:
        ready, _, _ = select.select([port.fd], [], [], 0.2)
        if ready:
            buf += os.read(port.fd, 4096).decode(errors="replace")
            if re.search(r"(?m)^(CONNECT|OK|ERROR|NO CARRIER|\+CME ERROR:.*)\s*$", buf):
                break
    log(LOGNAME, f">>> {cmd}\n{buf.strip()}")
    return buf


def cmd_status(port) -> None:
    for c in ("AT+CPIN?", "AT+CFUN?", "AT+CEREG?", "AT+COPS?", "AT+CGACT?", f"AT+CGCONTRDP={CID}", "AT+XCESQ?"):
        ask(port, c, must_ok=False)


def cmd_up(port, apn: str) -> None:
    ask(port, "AT+CMEE=2")
    if "READY" not in ask(port, "AT+CPIN?", must_ok=False):
        sys.exit("SIM not READY; stopping")
    if "+CFUN: 1" not in ask(port, "AT+CFUN?"):
        sys.exit("radio not on (CFUN != 1); not changing it, stopping")
    for _ in range(30):
        if re.search(r"\+CEREG: \d,[15]\b", ask(port, "AT+CEREG?")):
            break
        time.sleep(2)
    else:
        sys.exit("not registered within 60 s; stopping")
    ask(port, f'AT+CGDCONT={CID},"IP","{apn}"')
    ask(port, f"AT+XDNS={CID},1", must_ok=False)
    ask(port, f"AT+CGACT=1,{CID}", timeout=60)
    parsed = parse_contrdp(ask(port, f"AT+CGCONTRDP={CID}"))
    if not parsed:
        sys.exit("no IPv4 address in AT+CGCONTRDP; stopping (run 'down' to clean up)")
    ip, reported_gw, dns = parsed
    prefix, gw = subnet_calc(ip)
    # the L830 reports its gateway (mrhaav's L850 script ignores the field); prefer it when inside the subnet
    if reported_gw and ipaddress.IPv4Address(reported_gw) in ipaddress.IPv4Network(f"{ip}/{prefix}", strict=False):
        gw = reported_gw
    ask(port, f'AT+XDATACHANNEL=1,1,"/USBCDC/0","/USBHS/NCM/0",2,{CID}')
    if "CONNECT" not in wait_connect(port, f'AT+CGDATA="M-RAW_IP",{CID}'):
        sys.exit("AT+CGDATA did not answer CONNECT; run 'down' to clean up")
    session = {"ip": ip, "prefix": prefix, "gateway": gw, "dns": dns, "cid": CID, "apn": apn,
               "iface": "en7", "at_port": port.path, "time": time.strftime("%Y-%m-%d %H:%M:%S")}
    SESSION.write_text(json.dumps(session, indent=1))
    log(LOGNAME, f"session up: {session}")


def cmd_down(port) -> None:
    ask(port, f"AT+CGACT=0,{CID}", timeout=30, must_ok=False)
    SESSION.unlink(missing_ok=True)
    log(LOGNAME, "session down")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["up", "down", "status", "selftest"])
    ap.add_argument("--apn", default="internet.telekom")
    a = ap.parse_args()
    if a.action == "selftest":
        return selftest()
    port = open_at_port()
    log(LOGNAME, f"# {a.action} on {port.path}")
    try:
        if a.action == "up":
            cmd_up(port, a.apn)
        elif a.action == "down":
            cmd_down(port)
        else:
            cmd_status(port)
    finally:
        port.close()


if __name__ == "__main__":
    main()
