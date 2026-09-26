# Glossary

Plain-language definitions of the technical terms used across this repo's documentation. Each page's own "Glossary" section links to the entries it uses.

Definitions of standard terms (APN, LTE, MIMO and so on) are general explanations added to help readers. They aren't findings from our bench. Entries marked *general definition* are only used in the macOS design notes and the bench log.

### ADB

*Android Debug Bridge.* A debugging connection. On this module it gives a root shell on the internal Linux system, with no password.

### AGC

*automatic gain control.* How much the receiver amplifies the incoming signal. If it doesn't change between indoors and outdoors, the receive chain is probably faulty.

### AP / MD

The module's two processors: the application processor (runs a small OpenWrt Linux; owns USB and ADB) and the modem core (the cellular radio stack).

### APN

*access point name.* The carrier setting that selects the data service, e.g. `internet.telekom`.

### ARP

Address Resolution Protocol — how a device on a local network segment asks "who has this IP address?" and gets back a hardware (MAC) address. fm350mac answers ARP itself in user space because the cellular link has no real Ethernet segment behind it. *(General definition.)*

### AT command

A short text command sent to the modem, e.g. `AT+CPIN?`. Commands ending in `?` only read; `=` usually sets.

### AT port

The modem's command port. USB interface 6 in mode 41 (usually `/dev/ttyUSB4` on Linux). Only one program can use it at a time.

### Band / EARFCN / PCI

The frequency range a cell uses / the exact channel number / the cell's physical ID.

### BROM / preloader

The first boot stages of a MediaTek chip, used by SP Flash Tool to write firmware.

### Cell ID / TAC

Identifiers of a cell tower and its area. Together with the operator code they can locate you to within a few hundred metres, so redact them before posting.

### DIPC mode

The module setting that decides whether the host talks to it over PCIe, USB or both. Stored as a small text file on the module.

### EN-DC / 5G NSA

5G non-standalone, where a 5G carrier is used alongside an LTE connection. SA (standalone) is 5G without LTE.

### Failover / failback

Moving traffic to the backup link when the main line fails / moving it back when the main line returns.

### FCC lock

A lock that laptop vendors set on these modules. The radio stays off until the host sends the right unlock response.

### GIL

The Global Interpreter Lock — in the standard Python interpreter, only one thread runs Python bytecode at a time. Calls into a C library such as libusb release the GIL while they block, which is why several Python threads can still make progress around blocking USB calls. *(General definition.)*

### GNSS

Satellite positioning (GPS, Galileo and others). Used here as an independent test of the radio receive path.

### IMEI / IMSI / ICCID

The modem's hardware identity / the SIM subscriber identity / the SIM card's serial number. Treat all three as private.

### kext / kernel extension

A driver that loads into the operating system kernel itself. Powerful, but a crash or bug there can crash the whole system, which is why Apple restricts and is phasing them out. *(General definition.)*

### LaunchDaemon

A macOS background service started by the system (`launchd`).

### libusb

A library that lets normal programs talk to USB devices directly, without a kernel driver.

### LTE / NR

4G / 5G radio technology.

### M.2 B-key

*3042/3052.* The slot type and card sizes used by laptop cellular modules.

### MHF4 / IPEX-4

The tiny antenna connectors on the module.

### MIMO

*4×4.* Using four antennas at once for more speed and a better signal.

### mwan3

The OpenWrt package that monitors several internet links and routes traffic over the one that works.

### NV partitions / calibration

Module storage holding the IMEI and the factory radio calibration. They can't be recreated if lost.

### OEM image

A laptop vendor's customised firmware. A `GTPKGVER` value ending in `_5025…` is Dell's.

### OpenWrt

Open-source Linux firmware for routers.

### PDP context / data session

The cellular data connection that gets an IP address from the network.

### Pigtail

The short cable from the module's MHF4 connector to the SMA antenna socket on the adapter case.

### Protocol handler

*`atc`, `xmm`.* The OpenWrt add-on that dials the modem and configures the interface.

### QEMU / Docker

Tools that run a virtual machine / an isolated container. Used to test the router scripts without a real router.

### RAT

Radio access technology (3G, LTE, 5G NR).

### RNDIS

A Microsoft USB networking protocol. It is the only data interface this modem offers over USB. Linux supports it; macOS doesn't.

### RSRP / RSRQ / SINR

Signal strength / signal quality / signal-to-noise ratio of a cell. Higher (less negative) is better.

### SIP

*System Integrity Protection.* A macOS security feature that blocks certain changes even for an administrator or root process, unless it is deliberately reduced. Some older driver approaches require doing that. *(General definition.)*

### SP Flash Tool

MediaTek's Windows program for writing firmware to the chip.

### uci

OpenWrt's command-line configuration tool.

### URB

USB Request Block — the unit of one in-flight USB transfer at the driver/library level. Keeping several submitted at once ("in flight") is what lets a fast USB device be kept busy instead of waiting for each transfer to finish before starting the next. *(General definition.)*

### URC

An unsolicited result code, a status message the modem sends on its own.

### USB mode 40 / 41

The modem's two USB layouts (`0e8d:7126` / `0e8d:7127`). Mode 41 is the default.

### utun

The virtual network interface type that macOS VPNs use; fm350mac uses one to hand packets to macOS.

### W_DISABLE#

A pin on the M.2 slot that a laptop can use to turn the radio off.

### Watchdog

*`fm350-watchdog`.* The repo's router service that restarts the `wwan` interface if it gets stuck connecting.
