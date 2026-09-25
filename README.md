# 5g-failover

[![CI](https://github.com/sebastianspicker/5g-failover/actions/workflows/ci.yml/badge.svg)](https://github.com/sebastianspicker/5g-failover/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Platform: OpenWrt | macOS](https://img.shields.io/badge/platform-OpenWrt%20%7C%20macOS-lightgrey.svg)](#whats-in-the-repo)
[![Demo](https://img.shields.io/badge/demo-GitHub%20Pages-58a6ff.svg)](https://sebastianspicker.github.io/5g-failover/)

Tools and notes for running a **Fibocom FM350-GL** 5G modem over USB, including the **Dell DW5931e** that ships in Latitude laptops and turns up cheaply as a used part. The module sits in an M.2-to-USB adapter and serves as a backup WAN link for a home router.

The repo has three parts:

- **OpenWrt failover:** scripts that set up the modem as a second WAN on an OpenWrt router and switch traffic to it with `mwan3` when the wired line goes down.
- **`fm350mac`:** a user-space macOS driver. macOS has no driver for the modem's RNDIS network interface, so this talks to it over libusb and hands packets to a `utun` interface.
- **Dell DW5931e field guide:** what the Dell OEM firmware changes, how to reach the module's internal root shell, and how to switch it out of "PCIe Advance Mode", which AT commands refuse to do.

**Interactive demo:** https://sebastianspicker.github.io/5g-failover/

## Status

We tested this on one module on 2026-09-25.

| What | Status |
|---|---|
| USB 3 enumeration through the adapter, AT access, ADB | Verified on hardware |
| SIM detection, LTE registration (Vodafone DE, band 1) | Verified on hardware |
| Router scripts (install, uninstall, failover, failback) | Verified in Docker, a modem emulator, and OpenWrt 24.10.8 under QEMU |
| `fm350mac` data path | Verified in loopback mode (fake modem); 290 unit tests |
| Cellular data session, throughput | **Not tested yet** (waiting for a data SIM) |
| 5G NR | The cell offers EN-DC (5G NSA) and the modem measures an NR carrier; no NR data yet |

## Screenshot tour

Every image is real output from our bench, rendered to SVG by [`tools/screenshots.py`](tools/screenshots.py). IMEI, IMSI, ICCID, serial number, and the serving cell's ID and TAC are redacted.

**`fm350mac status --redact`**: SIM, registration, operator and access technology (here LTE with 5G NSA), serving cell with signal strength, the NR carrier, neighbour cells by band.

![fm350mac status](docs/assets/screenshots/status.svg)

**`fm350mac doctor`**: the checks from the Dell guide in one read-only run, with one OK/WARN/INFO line each.

![fm350mac doctor](docs/assets/screenshots/doctor.svg)

**Identity check**: firmware image (`_5025` = Dell), DIPC mode, FCC lock and SIM, in one AT round trip.

![identity check](docs/assets/screenshots/identity.svg)

**`fm350mac probe`**: RNDIS initialisation and device queries over libusb, no root needed.

![fm350mac probe](docs/assets/screenshots/probe.svg)

**`install.sh --dry-run`**: every `uci` change the router installer would make, run inside an OpenWrt 24.10 container.

![install.sh dry run](docs/assets/screenshots/install-dry-run.svg)

**`qemu-failover-test.sh`**: OpenWrt 24.10.8 under QEMU with real mwan3. It fails over when the wired link drops or the upstream dies, fails back, and handles both links down.

![QEMU failover test](docs/assets/screenshots/qemu-failover-summary.svg)

**`fm350mac --help`**: every subcommand.

![fm350mac help](docs/assets/screenshots/help.svg)

## What's in the repo

### OpenWrt failover ([`openwrt/`](openwrt/))

On a router running vanilla OpenWrt 24.10 or newer:

```sh
scp -r openwrt root@192.168.1.1:/root/
ssh root@192.168.1.1
cd /root/openwrt
./install.sh --apn <your-apn> --dry-run   # show the uci changes first
./install.sh --apn <your-apn>
```

This installs the FM350 protocol handler (mrhaav's `atc`, or modemfeed's `xmm` with `--proto xmm`), adds a `wwan` interface, and sets up an `mwan3` failover policy. It keeps your existing mwan3 settings: the stock catch-all rules get pointed at the failover policy, and `uninstall.sh` puts them back. It also installs a small watchdog that restarts `wwan` if the protocol handler gets stuck (a known `atc.sh` bug); `--no-watchdog` skips it. `fm350-status` shows decoded signal and cell info on the router. In the QEMU test, traffic moved to the modem 5 s after the wired link dropped and came back 4 s after it returned. When the link stayed up but the upstream died, it took 13 s and 16 s.

We built it for a GL.iNet Flint 2 (GL-MT6000). Nothing in it is specific to that router, but we haven't tried it on others. GL.iNet's stock firmware doesn't recognise the FM350; see [docs/compatibility-and-risks.md](docs/compatibility-and-risks.md). Full walkthrough: [docs/setup-guide.md](docs/setup-guide.md).

### `fm350mac`: macOS driver ([`fm350mac/`](fm350mac/))

```sh
brew install libusb
cd fm350mac && uv sync
uv run fm350mac status --redact        # SIM, registration, signal, cells (no root)
uv run fm350mac doctor                 # read-only checks for OEM/Dell modules
uv run fm350mac at 'AT+GTPKGVER?'      # raw AT commands (no root)
```

It's pure Python with a small ctypes binding to libusb, and has no kext or system extension. The part that needs root (creating the `utun` interface and routes) runs in a separate helper installed as a LaunchDaemon, so the driver itself runs as your user. See [fm350mac/README.md](fm350mac/README.md) and the design notes in [docs/macos-driver.md](docs/macos-driver.md).

If you only want an AT prompt, [`tools/fm350_at.py`](tools/fm350_at.py) works on its own:

```sh
uv run --with pyusb tools/fm350_at.py 'ATI' 'AT+CPIN?'
```

### Dell DW5931e guide ([`docs/dell-dw5931e-usb.md`](docs/dell-dw5931e-usb.md))

If `AT+GTPKGVER?` ends in `_5025…`, you have Dell's firmware. The short version:

- It already works over USB in an adapter. Dell's mode only turns USB off when the module has a live PCIe link.
- The mode is a text file on the module's internal Linux system, and the module offers a root shell over ADB. The guide shows how to back the module up and switch the mode safely.
- If the modem hears **no cells at all**, check the antenna pigtails before you touch the firmware. We lost a day to a bad set.

## Hardware we used

| Part | Notes |
|---|---|
| Fibocom FM350-GL (Dell DW5931e) | MediaTek T700, M.2 3052, firmware 29.20.22. USB ID `0e8d:7127` |
| Waveshare USB TO M.2 B KEY (SKU 23252) | USB 3 to M.2 B-key adapter with a nano-SIM slot and 4 SMA connectors. Waveshare doesn't list the FM350 as supported, but it worked |
| 4 antennas + MHF4-to-SMA pigtails | Buy decent pigtails. Ours were the problem |
| GL.iNet Flint 2 (GL-MT6000) | Router; USB 3 port, 5 V / 2 A |

Specs, power budget and band support are in [docs/hardware.md](docs/hardware.md).

## Documentation

| Document | What's in it |
|---|---|
| [docs/setup-guide.md](docs/setup-guide.md) | Step-by-step bring-up on OpenWrt, from `lsusb` to a working failover |
| [docs/dell-dw5931e-usb.md](docs/dell-dw5931e-usb.md) | Dell DW5931e over USB: DIPC mode, ADB, backup, troubleshooting |
| [docs/compatibility-and-risks.md](docs/compatibility-and-risks.md) | What's proven, what isn't, FCC lock, GL.iNet firmware support |
| [docs/at-commands.md](docs/at-commands.md) | FM350 AT command cheat sheet |
| [docs/hardware.md](docs/hardware.md) | The three parts, power, antennas, bands |
| [docs/macos-driver.md](docs/macos-driver.md) | How `fm350mac` works and why it's built that way |
| [docs/firmware-reflash.md](docs/firmware-reflash.md) | Untested reflash procedure for OEM units that won't register |
| [docs/bench-log.md](docs/bench-log.md) | Dated lab notes with every measurement |
| [docs/sources.md](docs/sources.md) | References |

## Development

```sh
cd fm350mac && uv run pytest -q && uvx ruff check .     # macOS driver
shellcheck openwrt/*.sh openwrt/tests/*.sh               # router scripts
openwrt/tests/docker-test.sh                             # install/uninstall in an OpenWrt rootfs (Docker)
openwrt/tests/atc-test.sh                                # protocol handler against a fake FM350
openwrt/tests/qemu-failover-test.sh                      # real mwan3 failover in OpenWrt under QEMU
python3 tools/screenshots.py                             # regenerate the README screenshots
```

## Not affiliated

This is an independent project, not affiliated with or endorsed by Fibocom, Dell, MediaTek, Waveshare or GL.iNet. Changing modem configuration can make a module unusable. The guides say what we did on our unit and what happened, and that's all we can promise.

## License

[MIT](LICENSE)
