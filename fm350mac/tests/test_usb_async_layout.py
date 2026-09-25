"""Checks usb_async.LibusbTransfer's field layout against a small C program
compiled against the real libusb.h, so a struct mismatch (wrong platform,
wrong libusb version) fails loudly instead of corrupting memory.

Skipped (with a reason) only if `cc` or the libusb-1.0 header is missing.
"""

import ctypes
import shutil
import subprocess
import sysconfig
import tempfile
from pathlib import Path

import pytest

from fm350mac import usb_async

LIBUSB_INCLUDE_DIR = "/opt/homebrew/include/libusb-1.0"
LIBUSB_HEADER = Path(LIBUSB_INCLUDE_DIR) / "libusb.h"

_FIELDS = [f[0] for f in usb_async.LibusbTransfer._fields_]

_C_SOURCE = """
#include <stdio.h>
#include <stddef.h>
#include <libusb.h>

int main(void) {
    printf("sizeof=%zu\\n", sizeof(struct libusb_transfer));
""" + "".join(
    f'    printf("{name}=%zu\\n", offsetof(struct libusb_transfer, {name}));\n' for name in _FIELDS
) + """
    return 0;
}
"""


def _compile_and_run() -> dict[str, int]:
    cc = shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "layout.c"
        exe = Path(tmp) / "layout"
        src.write_text(_C_SOURCE)
        subprocess.run(
            [cc, "-I", LIBUSB_INCLUDE_DIR, str(src), "-o", str(exe)],
            check=True, capture_output=True, text=True,
        )
        result = subprocess.run([str(exe)], check=True, capture_output=True, text=True)
    values = {}
    for line in result.stdout.strip().splitlines():
        key, _, value = line.partition("=")
        values[key] = int(value)
    return values


def _skip_reason() -> str | None:
    if shutil.which("cc") is None and shutil.which("gcc") is None and shutil.which("clang") is None:
        return "no C compiler (cc/gcc/clang) found"
    if not LIBUSB_HEADER.is_file():
        return f"libusb.h not found at {LIBUSB_HEADER}"
    return None


@pytest.mark.skipif(_skip_reason() is not None, reason=_skip_reason() or "")
def test_libusb_transfer_layout_matches_the_real_header():
    values = _compile_and_run()

    assert ctypes.sizeof(usb_async.LibusbTransfer) == values["sizeof"]
    for name in _FIELDS:
        ctypes_offset = getattr(usb_async.LibusbTransfer, name).offset
        assert ctypes_offset == values[name], f"field {name!r}: ctypes offset {ctypes_offset} != C offset {values[name]}"


@pytest.mark.skipif(sysconfig.get_platform().split("-")[0] != "macosx", reason="only meaningful on macOS")
def test_platform_is_lp64_as_assumed():
    assert ctypes.sizeof(ctypes.c_void_p) == 8
    assert ctypes.sizeof(ctypes.c_long) == 8
