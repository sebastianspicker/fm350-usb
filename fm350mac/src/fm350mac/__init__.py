"""fm350mac: a user-space macOS data path for a Fibocom FM350-GL 5G modem.

USB RNDIS (libusb, via our own ctypes binding -- no pyusb) bridged to a
macOS utun interface. See docs/macos-driver.md in the repo root for the
architecture.
"""

__version__ = "0.1.0a1"
