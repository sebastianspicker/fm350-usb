#!/usr/bin/env python3
"""Send AT commands to a Fibocom FM350-GL over raw USB (no kernel serial driver needed).

Useful on macOS, which has no driver for the FM350's vendor-specific serial interfaces.
Run with: uv run --with pyusb tools/fm350_at.py [--iface N] 'AT+CGMR' 'AT+CPIN?' ...

Thin wrapper around fm350mac.at.AtPort (see ../fm350mac/); falls back to a
self-contained implementation if the fm350mac package isn't importable.
"""
import argparse
import re
import sys
from pathlib import Path

# Make the sibling fm350mac/src tree importable without installing the
# package, so this script keeps working standalone via
# `uv run --with pyusb tools/fm350_at.py ...`.
_FM350MAC_SRC = Path(__file__).resolve().parent.parent / "fm350mac" / "src"
if _FM350MAC_SRC.is_dir():
    sys.path.insert(0, str(_FM350MAC_SRC))

try:
    from fm350mac.at import AtPort
except ImportError:
    AtPort = None


def _main_fm350mac(args) -> None:
    port = AtPort(iface_override=args.iface)
    print(
        f"# 0e8d:{port.usb_device.pid:04x} AT interface {port.iface_num} "
        f"OUT=0x{port.ep_out:02x} IN=0x{port.ep_in:02x}"
    )
    try:
        for cmd in args.commands:
            print(f">>> {cmd}")
            print(port.command(cmd))
    finally:
        port.close()


def _main_standalone(args) -> None:
    import time

    import usb.core
    import usb.util

    VID = 0x0E8D
    PIDS = {0x7127: 6, 0x7126: 4}  # USB product id -> AT interface number

    def find_libusb_backend():
        import usb.backend.libusb1 as libusb1

        for path in ("/opt/homebrew/lib/libusb-1.0.dylib", "/usr/local/lib/libusb-1.0.dylib"):
            backend = libusb1.get_backend(find_library=lambda _name, p=path: p)
            if backend:
                return backend
        return libusb1.get_backend()

    def open_at_port(iface_override=None):
        backend = find_libusb_backend()
        for pid, default_iface in PIDS.items():
            dev = usb.core.find(idVendor=VID, idProduct=pid, backend=backend)
            if dev:
                break
        else:
            sys.exit("FM350 not found (0e8d:7126/7127)")
        iface_num = default_iface if iface_override is None else iface_override
        cfg = dev.get_active_configuration()
        intf = cfg[(iface_num, 0)]
        usb.util.claim_interface(dev, iface_num)
        ep_out = usb.util.find_descriptor(
            intf, custom_match=lambda e: usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_OUT)
        ep_in = usb.util.find_descriptor(
            intf, custom_match=lambda e: usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_IN
            and usb.util.endpoint_type(e.bmAttributes) == usb.util.ENDPOINT_TYPE_BULK)
        print(f"# 0e8d:{pid:04x} AT interface {iface_num} OUT=0x{ep_out.bEndpointAddress:02x} IN=0x{ep_in.bEndpointAddress:02x}")
        return dev, iface_num, ep_out, ep_in

    def drain(ep_in, timeout_ms=200):
        out = b""
        while True:
            try:
                out += bytes(ep_in.read(ep_in.wMaxPacketSize * 8, timeout=timeout_ms))
            except usb.core.USBTimeoutError:
                return out

    def send(ep_out, ep_in, cmd, total_timeout=240.0):
        ep_out.write((cmd + "\r").encode())
        buf = b""
        deadline = time.time() + total_timeout
        while time.time() < deadline:
            buf += drain(ep_in, 300)
            text = buf.decode(errors="replace")
            if _has_final_result(text):
                break
        return buf.decode(errors="replace").strip()

    dev, iface_num, ep_out, ep_in = open_at_port(args.iface)
    try:
        drain(ep_in)  # discard unsolicited output
        for cmd in args.commands:
            print(f">>> {cmd}")
            print(send(ep_out, ep_in, cmd))
    finally:
        usb.util.release_interface(dev, iface_num)
        usb.util.dispose_resources(dev)


# Same terminator rule as fm350mac.at: the line must be complete (a read can end
# mid-line, e.g. "+CME ERROR: operation not al"), and NO CARRIER is final too.
_FINAL_RESULT_RE = re.compile(r"^(OK|ERROR|NO CARRIER|\+CME ERROR:.*|\+CMS ERROR:.*)\n", re.MULTILINE)


def _has_final_result(text: str) -> bool:
    """True once a whole line is a final result code (not just a substring)."""
    return _FINAL_RESULT_RE.search(text.replace("\r\n", "\n").replace("\r", "\n")) is not None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--iface", type=int, help="override AT interface number")
    ap.add_argument("commands", nargs="+")
    args = ap.parse_args()
    if AtPort is not None:
        _main_fm350mac(args)
    else:
        _main_standalone(args)


if __name__ == "__main__":
    main()
