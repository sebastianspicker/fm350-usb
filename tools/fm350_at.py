#!/usr/bin/env python3
"""Send AT commands to a Fibocom FM350-GL over raw USB (no kernel serial driver needed).

Useful on macOS, which has no driver for the FM350's vendor-specific serial interfaces.
Run with: uv run tools/fm350_at.py [--iface N] 'AT+CGMR' 'AT+CPIN?' ...

Thin wrapper around fm350mac.at.AtPort (see ../fm350mac/), imported from the
sibling source tree, so no install and no third-party package is needed: the
system libusb (brew install libusb) is enough.
"""
import argparse
import sys
from pathlib import Path

# Make the sibling fm350mac/src tree importable without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "fm350mac" / "src"))

from fm350mac.at import AtPort  # noqa: E402
from fm350mac.redact import redact_pin  # noqa: E402


def _main_fm350mac(args) -> None:
    port = AtPort(iface_override=args.iface)
    print(
        f"# 0e8d:{port.usb_device.pid:04x} AT interface {port.iface_num} "
        f"OUT=0x{port.ep_out:02x} IN=0x{port.ep_in:02x}"
    )
    try:
        for cmd in args.commands:
            print(f">>> {redact_pin(cmd)}")
            print(redact_pin(port.command(cmd)))
    finally:
        port.close()


def _iface_arg(value: str) -> int:
    """argparse ``type=``: an interface number in 0..31 (same bound as ``fm350mac at --iface``)."""
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid int value: {value!r}") from None
    if not 0 <= number <= 31:
        raise argparse.ArgumentTypeError(f"{number} is out of range (0..31)")
    return number


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--iface", type=_iface_arg, help="override AT interface number (0..31)")
    ap.add_argument("commands", nargs="+")
    _main_fm350mac(ap.parse_args())


if __name__ == "__main__":
    main()
