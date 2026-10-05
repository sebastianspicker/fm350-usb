# Dell DW5931e (Fibocom FM350-GL) over USB: field guide

How to run a **Dell DW5931e**, the Dell OEM version of the Fibocom FM350-GL that comes out of Latitude laptops, as a USB modem in an M.2-to-USB adapter. The guide also covers how to switch the module from Dell's "PCIe Advance Mode" to the stock USB/PCIe dual mode, which AT commands (text commands sent to the modem) refuse to do.

Tested on one unit on 2026-09-25, in a Waveshare USB TO M.2 B KEY (SKU 23252) connected to a Mac. The full raw notes are in [bench-log.md](bench-log.md).

## In short

- **A DW5931e in a USB adapter works over USB out of the box** (`0e8d:7127`) — no flashing, unlock or reconfiguration needed. Dell's "PCIe Advance Mode" only turns USB off when the module has a live PCIe link, and a USB adapter never provides one.
- The module's interface setting (**DIPC mode**) can't be changed by AT command on this unit (`AT+GTDIPCMODE=...` fails with `+CME ERROR: phone failure`), but it's stored in a plain text file on the module's internal Linux system. You edit it over a root shell reached through ADB (Android Debug Bridge) — see [Step 4](#step-4-optional-switch-from-pcie-advance-mode-to-dual-mode). You don't need to do this just to use the module over USB.
- **If the modem sees no cells at all, check the antenna cables (pigtails) first.** We spent a day ruling out every firmware and configuration cause before finding a pair of defective pigtails. After swapping them, the module registered within 30 s and saw 10 cells.
- Biggest caveat: this is one unit, tested once, and a data session was only verified later (macOS, `fm350mac`, Telekom DE SIM, 2026-10-05; see the [bench log](bench-log.md)).

## Is this your module?

| Check | Our DW5931e | Meaning |
|---|---|---|
| Label | Dell DW5931e | Came from Latitude 7440 / 5531 / 9330 / 3571 |
| USB ID in an adapter | `0e8d:7127` ("Fibocom Wireless Inc." / "FM350-GL") | Mode 41 (RNDIS + serial ports + ADB) |
| `AT+GTPKGVER?` | `81600.0000.00.29.20.22_5025.0000.040.000.038_C69` | **`_5025`** = Dell customer image; firmware 29.20.22 |
| `AT+GTDIPCMODE?` | `1,2,2,2,7,13` | PCIe Advance Mode (stock default is `3,1,1,1,3,15`) |
| `AT+GTFCCEFFSTATUS?` | `0,1` | No FCC lock |
| `AT+GTFMODE?` | `1,0` | Stock default is `0,0`. The radio came up without changing this |

Other OEM builds exist: Lenovo units use FCC unlock hash `3df8c719`, and HP units report a similar locked DIPC mode (`1,2,2,2,5,13`, [OpenWrt forum](https://forum.openwrt.org/t/fibocom-fm350-gl-support/142682) posts #346–#351). Most of this guide probably applies to them too, but we tested only the Dell unit.

## Background: what "DIPC mode" is and why it looks like a USB lock

The FM350 contains two processors:

- **AP** (application processor): runs an OpenWrt 19.07 build (`mt6880/k6880v1_mdot2_datacard`, kernel 4.19). It owns the USB gadget, ADB, the GNSS daemon and the DIPC daemon.
- **MD** (modem core): the actual 5G/LTE stack.

"DIPC" (dual IPC) decides whether the host talks to the module over PCIe, USB or both. The setting lives in `/mnt/vendor/nvdata/md_cmn/dipc_config`, a 125-byte text file:

```text
dual_ipc_mode:1
ap_logging_interface:2
md_logging_interface:2
md_at_interface:2
ap_pcie_port_config:7
md_pcie_port_config:13
```

`AT+GTDIPCMODE?` reports these six values in that order. The init script `/etc/init.d/usb.init` reads only `dual_ipc_mode`:

```sh
if [[ "$dipc_mode" != "1" && "$dipc_mode" != "3" ]]; then
    echo "DIPC mode with NO USB ($dipc_mode)"; exit 0
else
    if [[ "$dipc_mode" == "1" && "$pcie_link_state" != "0" ]]; then
        echo "PCIE Adv. with PCIE link success ($pcie_link_state)"; exit 0
    fi
fi
echo "Start USB ..."
```

| `dual_ipc_mode` | In a laptop (PCIe link up) | In a USB adapter (no PCIe link) |
|---|---|---|
| `1` (Dell default, "PCIe Advance") | USB off | **USB on** |
| `3` (stock default, dual) | USB on | USB on |
| anything else | **USB off: no USB access at all** | **USB off: no USB access at all** |

So a Dell unit already works over USB in an adapter. Switching to mode 3 makes USB unconditional and routes AT and logging to USB (`1`) instead of PCIe (`2`). On our unit, the AT port on USB interface 6 answered in both modes.

The `dipcd` daemon validates the file at boot (`check_dipc_config`, `fibo_check_dipc_config_verno`). If the file is invalid, it logs "Reconstruct default dipc config" and writes a default. We never triggered that path, so we don't know which default it writes on a Dell image. **Write only the exact file contents shown in this guide.**

## Hardware notes

- **Adapter:** Waveshare USB TO M.2 B KEY (3042/3052, nano-SIM slot 1, 4× SMA). Waveshare doesn't list the FM350 as supported, but in our tests the adapter passed USB 3 SuperSpeed (5 Gbps) and the SIM slot and all four RF paths worked.
- **Antenna cables (pigtails):** our original IPEX/MHF4-to-SMA pigtails were defective. With them, the module measured nothing: `AT+CESQ` returned all 99/255, `AT+GTCCINFO?` was empty, and GNSS acquired 0 satellites even under open sky. The GNSS AGC (automatic gain control) levels didn't change between indoors and outdoors. That last sign is the useful one: if moving outdoors doesn't change the receiver gain, the problem is in the RF chain, not in the configuration. See [Troubleshooting](#no-cells-at-all-cesq-all-99255-gtccinfo-empty).
- **Power:** idle worked on a bus-powered USB 3 hub (896 mA allocated) [Bench log]. For real traffic, use the adapter's second, power-only USB plug. See the [Hardware guide](hardware.md) for the full power budget [Hardware].

## Step 1: check that it enumerates

Wait 10–60 s after power-on.

```sh
lsusb | grep 0e8d                        # Linux: expect 0e8d:7127
ioreg -p IOUSB -w0 | grep FM350          # macOS
```

USB interfaces in mode 41: 0/1 RNDIS, 2–4 serial, **5 ADB**, **6 AT (the only port that sends URCs)**, 7–9 serial. Interface 3 is the GNSS port.

## Step 2: get an AT shell

**Linux / OpenWrt:**

```sh
echo "0e8d 7127 ff" > /sys/bus/usb-serial/drivers/option1/new_id   # only if no ttyUSB appears
ls -d /sys/bus/usb/devices/*:1.6/ttyUSB*                          # usually /dev/ttyUSB4
picocom -b 115200 --echo /dev/ttyUSB4
```

**macOS** (no serial driver for these interfaces): use [`tools/fm350_at.py`](../tools/fm350_at.py), which talks to interface 6 directly through libusb (a library that lets normal programs talk to USB devices without a kernel driver):

```sh
brew install libusb
uv run --with pyusb tools/fm350_at.py 'ATI' 'AT+GTPKGVER?' 'AT+GTDIPCMODE?'
```

Read the current state:

```text
AT+CMEE=2
AT+GTPKGVER?
AT+GTDIPCMODE?
AT+GTFCCEFFSTATUS?
AT+GTFMODE?
AT+CFUN?
AT+CPIN?
```

## Step 3: get the ADB root shell and make a backup

The AP exposes ADB on interface 5, with **no authentication, as root**.

```sh
brew install --cask android-platform-tools   # or: apt install adb
adb devices          # "(no serial number)  device"
adb shell id         # uid=0(root)
```

**Back up before you change anything.** The NV partitions hold the IMEI and the RF calibration, and there's no replacement if they're lost.

```sh
B=backup-$(date +%Y%m%d-%H%M); mkdir -p "$B" && cd "$B"
for d in nvram nvdata nvcfg protect_f protect_s mdota mdota2 mdota3; do
  adb exec-out "tar -C /mnt/vendor -cf - $d 2>/dev/null" > $d.tar
done
adb exec-out 'cat /proc/mtd' > proc_mtd.txt
# Optional: full raw image of every MTD partition (~530 MB, 40 partitions)
for n in $(seq 0 39); do adb exec-out "cat /dev/mtdblock$n" > mtd$n.img; done
shasum -a 256 * > SHA256SUMS
```

The backup contains your IMEI and calibration. **Don't publish it**, and don't restore it onto a different unit.

If `adb devices` shows **`offline`** after a modem reset (`AT+CFUN=15`), `adb kill-server` alone doesn't fix it. A USB port reset does (for example libusb `libusb_reset_device`, or unplug and replug), followed by `adb start-server`.

## Step 4 (optional): switch from PCIe Advance Mode to dual mode

This step isn't needed to use the module over USB (see [Background](#background-what-dipc-mode-is-and-why-it-looks-like-a-usb-lock)). Do it if you want the stock Fibocom behaviour, where USB is unconditional and AT/logging go over USB, for example because you want to use the module in a PCIe laptop slot and still reach it over USB.

**Risk:** if `dual_ipc_mode` ends up as anything other than `1` or `3`, the module disables USB. From then on you can reach it only over PCIe, or by reflashing with SP Flash Tool. Copy the command exactly as written.

```sh
adb exec-out 'cd /mnt/vendor/nvdata/md_cmn \
  && cp -p dipc_config dipc_config.orig-dell \
  && printf "dual_ipc_mode:3\nap_logging_interface:1\nmd_logging_interface:1\nmd_at_interface:1\nap_pcie_port_config:3\nmd_pcie_port_config:15\n" > dipc_config \
  && sync && cat dipc_config.orig-dell && echo --- && cat dipc_config'
```

Check that the second block reads exactly `dual_ipc_mode:3`, `…:1`, `…:1`, `…:1`, `…:3`, `…:15`, one value per line. Then reset the modem:

```text
AT+CFUN=15
```

After re-enumeration (up to about 60 s), the USB ID is unchanged (`0e8d:7127`), and AT and ADB work again (ADB may need the USB reset described above). Then check:

```text
AT+GTDIPCMODE?     → +GTDIPCMODE: 3,1,1,1,3,15
```

The change survives reboots. **To revert:**

```sh
adb exec-out 'cd /mnt/vendor/nvdata/md_cmn && cp -p dipc_config.orig-dell dipc_config && sync'
# then AT+CFUN=15
```

Mode 3 had **no effect on cellular reception** on our unit (see below). It changes which interface the host uses, not the radio.

## Step 5: SIM and registration

```text
AT+CPIN?                       → READY
AT+CGDCONT=1,"IPV4V6","<apn>"
AT+CEREG?                      → 0,1 (home) or 0,5 (roaming)
AT+COPS?                       → +COPS: 0,2,"<mcc><mnc>",13   (7 = LTE, 13 = LTE with 5G NR dual connectivity)
AT+CESQ
AT+GTCCINFO?                   → serving cell + neighbours
```

Our result after the cable swap (Vodafone DE SIM, indoors): `+CEREG: 0,1`, `+COPS: 0,2,"26202",13`, serving LTE cell on EARFCN 100 (band 1), and 9 neighbour cells on bands 7, 8, 20 and 28. `+COPS` access technology 13 means EN-DC (5G non-standalone: LTE plus a 5G carrier together) per 3GPP TS 27.007, and `+CESQ` also reported an NR carrier (SS-RSRP about −105 dBm, SS-SINR 5–8 dB). So 5G NSA should be available once data works, but we haven't seen an NR data leg yet.

On macOS, `fm350mac doctor` runs the checks from this guide in one go, and `fm350mac status --redact` gives a readable version of the above. On the router, `fm350-status -x` does the same.

The data session (the cellular connection that gives you an IP address) over RNDIS is covered in [setup-guide.md](setup-guide.md) (OpenWrt: `xmm-modem` or `atc-fib-fm350_gl`) and [macos-driver.md](macos-driver.md) (macOS: `fm350mac`). A data session was verified later, on macOS with `fm350mac` and a Telekom DE SIM on 2026-10-05 (ping, HTTPS and a 1 MB download; see the [bench log](bench-log.md) and [macos-driver.md](macos-driver.md)). It has not been run on OpenWrt on this unit.

## Troubleshooting

### No cells at all (`CESQ` all 99/255, `GTCCINFO` empty)

Symptoms on our unit: `CPIN: READY`, `CFUN: 1`, `CEREG` cycles between 2 (searching) and 0, `COPS=?` returns an empty list at once, and `COPS=1,...` fails within 3 s. The internal log shows `MIPC_NW_RADIO_STATE hw=1, sw=1`, which means the radio is on and searching.

Check in this order:

1. **Antenna cables and connectors.** Swap the pigtails and check that the MHF4 connectors on the module are fully seated. Ours were the cause.
2. GNSS (satellite positioning) as an independent RF check: under open sky it should acquire satellites within a few minutes. If both cellular and GNSS get nothing and the AGC doesn't react to the location, suspect the RF chain before you suspect the configuration.
3. Only then look at software: see the next table.

[`tools/fm350_diag.py read`](diagnostics.md) runs these checks by itself and samples for a minute. Its `experiment` stage repeats the software tests below, and restores each setting afterwards.

### Ruled out on our unit (none of these fixed "no cells")

| Suspect | Test | Result |
|---|---|---|
| FCC lock | `AT+GTFCCEFFSTATUS?` → `0,1`. Also ran the Dell challenge/response (`AT+GTFCCLOCKGEN` → response = first 4 bytes of `SHA-256(challenge ‖ 4909b5a4)` → `AT+GTFCCLOCKVER=<decimal>` → `1`) | Not locked, unlock changed nothing |
| W_DISABLE# | `AT+GTFMODE=0,0` + `CFUN=15` | No change (restored to `1,0`) |
| DIPC mode | File edit to `3,1,1,1,3,15` (Step 4) | No change in reception |
| Antenna tuner / SAR | `AT+GTANTTUNINGEN=0`, BodySAR state | No change (restored to `1`) |
| RAT/bands | `AT+ERAT=3` (LTE only), `AT+GTACT?` all bands | No change |
| APN | `AT+EIAAPN` + `AT+CGDCONT` | No change |
| Thermal | `AT+GTTHERMAL?`, all actuators level 0 | Not throttling |
| Power | Bus-powered hub vs powered dock | No change |
| Calibration | `AT+ECAL?` → `1`; NVRAM `CALIBRAT` 132/135 LIDs unchanged since the factory | Intact |

### Other pitfalls

- `AT+GTDIPCMODE=<anything>` → `+CME ERROR: phone failure`: expected on OEM images. Use the file (Step 4).
- `AT+ERAT` persists across `AT+CFUN=15`, contrary to the AT manual. Restore it (`AT+ERAT=21`) after experiments. `AT+GTACT` does **not** persist.
- Never poll the RNDIS control channel in a tight loop: about 2000 back-to-back `GET_ENCAPSULATED_RESPONSE` requests crashed the modem firmware (it re-enumerated after 60–90 s; settings survived).
- One community report says `AT+CGCONTRDP` crashes the modem ([forum](https://forum.openwrt.org/t/fibocom-fm350-gl-support/142682) #429). Watch for this with `atc-fib-fm350_gl`.
- Without an active data bearer, the RNDIS uplink accepts exactly 3 frames and then NAKs every write until a USB reset. That's normal, not a defect.
- **Security:** ADB is root without authentication on the USB bus. Treat anything with USB access to the module as root on the modem.

## What we didn't need

- **Reflashing.** The Dell firmware (driver package `f34xk`) or generic Fibocom firmware 29.23.x via SP Flash Tool is what the community recommends for OEM units that won't register. On ours, the problem was the hardware, and the Dell image 29.20.22 / 5025 works. The procedure is in [firmware-reflash.md](firmware-reflash.md) (untested).
- **An FCC unlock.** Our unit was already unlocked, but other DW5931e units may be locked. If `AT+GTFCCEFFSTATUS?` doesn't return `x,1`, use mrhaav's [`fm350_fcc_unlock.sh`](https://github.com/mrhaav/openwrt/blob/master/atc/fib-fm350_gl/fm350_fcc_unlock.sh) with `VENDOR_ID_HASH=4909b5a4`.

## Status and open points

| Item | Status |
|---|---|
| USB enumeration, AT, ADB | Verified |
| DIPC mode switch via file | Verified (mode 3 persists across resets) |
| SIM + LTE registration | Verified (Vodafone DE, band 1) |
| 5G NR (NSA/SA) | EN-DC offered by the cell, NR carrier measured (SS-RSRP ≈ −105 dBm); no NR data yet |
| Data session + throughput | **Pending** (data SIM) |
| GNSS fix after the cable swap | Not re-tested |
| Registration while still in mode 1 | Not tested (we switched to mode 3 before we found the cable fault). We expect it to work, because the HP unit in forum #346–#351 registered in PCIe Advance Mode |

## Sources

The full list of external references, with retrieval dates, is in [sources.md](sources.md).

- Our raw measurements: [bench-log.md](bench-log.md)

## Glossary

Terms used on this page, defined in the [shared glossary](glossary.md): [ADB](glossary.md#adb), [AGC](glossary.md#agc), [AP / MD](glossary.md#ap--md), [APN](glossary.md#apn), [AT command](glossary.md#at-command), [AT port](glossary.md#at-port), [DIPC mode](glossary.md#dipc-mode), [EN-DC / 5G NSA](glossary.md#en-dc--5g-nsa), [FCC lock](glossary.md#fcc-lock), [GNSS](glossary.md#gnss), [IMEI / IMSI / ICCID](glossary.md#imei--imsi--iccid), [libusb](glossary.md#libusb), [LTE / NR](glossary.md#lte--nr), [M.2 B-key](glossary.md#m2-b-key), [MHF4 / IPEX-4](glossary.md#mhf4--ipex-4), [NV partitions / calibration](glossary.md#nv-partitions--calibration), [OEM image](glossary.md#oem-image), [OpenWrt](glossary.md#openwrt), [PDP context / data session](glossary.md#pdp-context--data-session), [Pigtail](glossary.md#pigtail), [RAT](glossary.md#rat), [RNDIS](glossary.md#rndis), [RSRP / RSRQ / SINR](glossary.md#rsrp--rsrq--sinr), [SP Flash Tool](glossary.md#sp-flash-tool), [URC](glossary.md#urc), [USB mode 40 / 41](glossary.md#usb-mode-40--41), [W_DISABLE#](glossary.md#w_disable).
