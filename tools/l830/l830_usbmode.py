#!/usr/bin/env python3
"""Switch the Fibocom L830-EB USB composition with AT+GTUSBMODE (L8-Family AT manual V1.1.3, 4.2.1).

  l830_usbmode.py 0   3 CDC-ACM (2 AT + 1 trace) + NCM: works with macOS's built-in drivers
  l830_usbmode.py 1   restore: "MBIM only" (the Lenovo default; re-enumerates every ~15 s on macOS)

Mode 3 is refused: it left an L830-EB without any AT port in 2019. The setting is persistent.
Order: read -> set -> read back -> only if the read-back equals the target: AT+CFUN=15 (hardware reset),
then wait for the AT ports to come back and report the mode they show.
"""

import sys
import time

from l830_common import AtTimeoutError, TtyTransport, at_ports, log

LOGNAME = "l830_usbmode.log"


def ask(cmd: str, timeout: float = 10.0, retry: bool = True) -> str:
    """Send cmd on a freshly found AT port; retry once if the port drops (mode 1 re-enumerates).

    retry=False is for the reset: a port that drops after it is the expected outcome, and
    sending it again would reset the module a second time."""
    for attempt in (1, 2):
        deadline = time.monotonic() + 60
        while not (ports := at_ports()) and time.monotonic() < deadline:
            time.sleep(0.5)
        if not ports:
            sys.exit(f"no AT port within 60 s (before {cmd})")
        port = TtyTransport(ports[0])
        try:
            out = port.command(cmd, timeout=timeout)
            log(LOGNAME, f">>> {cmd} [{port.path}]\n{out}")
            return out
        except AtTimeoutError as exc:
            log(LOGNAME, f">>> {cmd}: timeout ({exc})")
            return ""
        except OSError as exc:
            log(LOGNAME, f">>> {cmd}: port lost (attempt {attempt}): {exc}")
            if not retry:
                return ""
        finally:
            port.close()
    sys.exit(f"{cmd} failed twice")


def main() -> None:
    if len(sys.argv) != 2 or sys.argv[1] not in ("0", "1"):
        sys.exit("usage: l830_usbmode.py 0|1")
    target = sys.argv[1]
    ask("AT+CMEE=2")
    if f"+GTUSBMODE: {target}" in ask("AT+GTUSBMODE?"):
        log(LOGNAME, f"already in mode {target}; nothing to do")
        return
    if "OK" not in ask(f"AT+GTUSBMODE={target}"):
        sys.exit("set command not accepted; NOT resetting")
    if f"+GTUSBMODE: {target}" not in ask("AT+GTUSBMODE?"):
        sys.exit("read-back does not show the target mode; NOT resetting")
    log(LOGNAME, "read-back OK -> AT+CFUN=15 (hardware reset)")
    ask("AT+CFUN=15", timeout=5, retry=False)  # the module may drop off USB before answering
    time.sleep(5)
    deadline = time.monotonic() + 120
    while not (ports := at_ports()) and time.monotonic() < deadline:
        time.sleep(1)
    log(LOGNAME, f"{len(ports)} AT port(s) after the reset: {ports}")
    for path in ports:
        port = TtyTransport(path)
        try:
            log(LOGNAME, f">>> AT+GTUSBMODE? [{path}]\n{port.command('AT+GTUSBMODE?', timeout=5)}")
        finally:
            port.close()


if __name__ == "__main__":
    main()
