# Hardware

Specs for the three parts of this setup, drawn from vendor datasheets and our own bench measurements. Useful if you're sourcing the same parts, checking a substitute, or trying to figure out why something doesn't fit.

## In short

- Three parts make up this setup: the router (GL.iNet Flint 2), the USB-to-M.2 adapter (Waveshare; since 2026-11-05 the USB 3.2 Gen1 5G DONGLE, earlier the USB TO M.2 B KEY), and the modem (Fibocom FM350-GL, including the Dell DW5931e OEM variant).
- The FM350-GL isn't on Waveshare's list of tested modules for this adapter, but it worked in our tests [Bench log].
- Power the adapter from two sources. On the 5G DONGLE, feed the DC jack as well as USB-C: with USB-C alone, sustained uploads made the module's supply sag and the whole module reset; with the DC jack also fed, the supply stayed flat and 1.5 GB of load passed [Bench log, 2026-11-05/06]. The earlier adapter has an auxiliary power plug for the same purpose.
- Connect all 4 antennas: 5G NR uses 4×4 MIMO for download, and missing antennas lower both the reported signal quality and the throughput.
- Buy decent antenna pigtails. A defective pair cost us most of a day chasing "no cells" as if it were a firmware or configuration problem [Bench log, Cable swap].

## GL.iNet Flint 2 (GL-MT6000)

Source: GL.iNet datasheet `mt6000_datasheet_20251103.pdf`, product page, firmware page.

| Item | Value |
|---|---|
| SoC | MediaTek MT7986 (Filogic 830), quad-core Cortex-A53 @ 2.0 GHz |
| RAM / storage | 1 GB DDR4 / 8 GB eMMC |
| Ethernet | 2× 2.5 GbE (WAN, WAN/LAN), 4× 1 GbE LAN |
| USB | 1× USB 3.0 Type-A, 5 V / 2 A |
| Power input | 12 V / 4 A, DC 5521 barrel; consumption < 20 W |
| Operating temp | 0–40 °C |
| Stock firmware | 4.9.1, OpenWrt 21.02 base, kernel 5.4 (MTK SDK) |
| Alternative GL firmware | 4.9.0-op24, OpenWrt 24.10 base, kernel 6.6 |
| Vanilla OpenWrt | Supported as `glinet_gl-mt6000` (target `mediatek/filogic`) |

GL.iNet firmware supports multi-WAN failover and load balancing across Ethernet, repeater, tethering and cellular interfaces.

## Waveshare USB 3.2 Gen1 5G DONGLE

The adapter on the bench since 2026-11-05. It's a different product from the USB TO M.2 B KEY below. We took these facts from our own bench use; we haven't copied a datasheet.

| Item | Value |
|---|---|
| Host interface | USB 3.2 Gen1 (USB-C). The FM350 enumerated at USB 3 (5 Gbps) |
| Power | From USB-C, plus a 5-12 V DC jack (3.5×1.35 mm; a USB-A to DC cable is included, no power supply). There is no dual-plug cable. **Feed the DC jack for sustained traffic**: USB-C alone is not enough (see below) |
| SIM | SIM1 is a nano-SIM slot. SIM2 is wired to an onboard eSIM chip |
| Result | SIM1, all RF paths and registration worked; data ran at up to 132 Mbit/s down |

**USB-C alone can't hold the module's supply under sustained uploads.** On USB-C only, 5 of 8 uploads of 300-500 MB reset the whole module, 28-249 MB in. The module's own supply reading (the PMIC's `BATADC`, read about 430 times a second) dropped from about 3.92 V to 3.495 V in the last 0.1 s before the reset, while it never went below 3.91 V during a 500 MB download. With the DC jack also fed (from a second port of the same Mac, through the included USB-A to DC cable), a 500 MB upload and a 500 MB download plus 500 MB upload both passed, and the supply never went below 4.099 V, on a weaker cell (−106 dBm) than the crash runs. Because both feeds came from the same Mac, the limit is the single USB-C path (port, cable or the dongle's USB-C input), not the Mac's total supply. Details: [bench-log.md](bench-log.md#sustained-upload-resets-on-the-5g-dongle-2026-11-0506).

## Waveshare USB TO M.2 B KEY (SKU 23252), earlier adapter

Source: product page and [wiki](https://www.waveshare.com/wiki/USB_TO_M.2_B_KEY).

| Item | Value |
|---|---|
| Host interface | USB 3.1 Type-A (the USB 3.x lines of the M.2 slot are connected) |
| Module slot | M.2 B-key, 3042 / 3052 (FM350-GL is 3052, so it fits) |
| Protocol | USB only. PCIe-only modules do not work |
| SIM | 1× nano-SIM |
| Antennas | 4× SMA via IPEX-4 (MHF4) pigtails, 4 antennas included |
| Cooling | Aluminium case, thermal pad included |
| LEDs | Power; network (blinks when data is flowing) |
| Power | From USB; the included USB 3.0 extension cable has two male plugs (data+power and power only) for extra current |
| Officially tested | Quectel RM500Q-GL / RM502Q-AE / RM520N-GL / RM530N-GL / RM500U-CN, SIMCom SIM82xx, Fibocom FM650-CN / FM160-EAU. Not FM350 |

Notes from the wiki:
- Waveshare warns that the host's USB port may not supply enough power for 5G and recommends the dual-plug cable or an external 5 V / 3 A supply.
- The IPEX-4 connectors are fragile. Pull them straight up with a gentle side-to-side wiggle.
- Connect all 4 antennas. 5G NR uses 4×4 MIMO for download, and missing antennas lower the signal quality readings the modem reports and the throughput.
- Before debugging the router side, check that the SIM works in a phone.

## Fibocom FM350-GL

Sources: Fibocom AT manual V2.10, Linux `option` driver patch (June 2024), 4gltemall spec sheet, OpenWrt forum.

| Item | Value |
|---|---|
| Chipset | MediaTek T700 (plus MT6880 RF) |
| Form factor | M.2 3052 (30 × 52 × 2.3 mm) |
| Host interfaces | PCIe Gen3 ×1 (laptop default), USB 3.x / 2.0. Fibocom calls USB a "debug" interface, but it carries data fine |
| Supply | 3.135–4.4 V (typ. 3.3 V) from M.2 |
| 5G | Sub-6 only, SA + NSA, NR CA, 4×4 DL MIMO; up to 4.67 Gbps DL / 1.25 Gbps UL (theoretical) |
| NR bands | n1, n2, n3, n5, n7, n8, n20, n25, n28, n30, n38, n40, n41, n48, n66, n71, n77, n78, n79 |
| LTE bands | B1–5, 7, 8, 12–14, 17–20, 25, 26, 28, 29, 30, 32, 34, 38–43, 46, 48, 66, 71 |
| WCDMA | 1, 2, 4, 5, 8 |
| GNSS | GPS, GLONASS, BeiDou, Galileo, QZSS |
| Operating temp | −10 to 55 °C |
| SIM | Dual SIM (one can be eSIM on some variants); the earlier Waveshare adapter only wires one SIM slot, the 5G DONGLE wires SIM2 to an onboard eSIM |

### USB personality

| `AT+GTUSBMODE` | USB ID | Interfaces | AT port |
|---|---|---|---|
| 40 | `0e8d:7126` | RNDIS + AT + AP(GNSS) + META + DEBUG + NPT + ADB | USB interface 4 |
| 41 (default) | `0e8d:7127` | as 40 + AP(LOG) + AP(META) | USB interface 6 |

The mode-41 layout, interface by interface: 0/1 RNDIS, 2–4 serial, 5 ADB, **6 AT (the only interface that sends URCs, unsolicited status messages)**, 7–9 serial; interface 3 is the GNSS port [Dell guide, Step 1].

- Interfaces 0–1 are RNDIS and use the `rndis_host` driver (`kmod-usb-net-rndis`).
- The other interfaces are vendor-specific serial ports and use the `option` driver (`kmod-usb-serial-option`). The FM350 IDs were added to mainline `option` in June 2024 (in 6.6+ and backported to 5.10.222). **Kernel 5.4 (GL stock) does not have them**; there you add them at runtime with `new_id`.
- There is no MBIM or QMI in USB mode. **ModemManager does not handle it.** Use AT-command scripts instead.
- The PCIe path (`kmod-mtk-t7xx`) exists, but the Waveshare board is USB-only, and forum users report the t7xx path is unstable.

### German mobile networks

We assumed the router runs in Germany. Deutsche Telekom, Vodafone and O2/Telefónica mainly use LTE B1, B3, B7, B8, B20, B28, B32 and NR n1, n3, n7, n28, n78, all of which the FM350-GL supports.

## Power budget

- Flint 2 USB port: 5 V / 2 A = 10 W.
- The FM350-GL datasheet does not give a figure we could verify. 5G modules draw short current spikes when attaching to the network and during uplink bursts. Waveshare recommends 5 V / 3 A for its 5G boards.
- Measured on the 5G DONGLE (2026-11-05/06): USB-C alone is not enough for sustained uploads, USB-C plus the DC jack is. See [the dongle section](#waveshare-usb-32-gen1-5g-dongle).
- Our plan: plug the dongle's USB-C (or the earlier adapter's data+power plug) into the Flint 2, and feed the DC jack (or the earlier adapter's power-only plug) from a separate 5 V ≥ 2 A USB charger. The port is powered even when the router has no working uplink, which is exactly when failover is needed, so keep the charger on the same UPS or circuit as the router.

## Thermal

The FM350 runs hot under sustained 5G load. Use the included thermal pad between the module and the aluminium lid, and don't enclose the dongle. Watch `AT+GTSENRDTEMP` during load tests (see [at-commands.md](at-commands.md)).

## Glossary

Terms used on this page, defined in the [shared glossary](glossary.md): [ADB](glossary.md#adb), [GNSS](glossary.md#gnss), [LTE / NR](glossary.md#lte--nr), [M.2 B-key](glossary.md#m2-b-key), [MHF4 / IPEX-4](glossary.md#mhf4--ipex-4), [MIMO](glossary.md#mimo), [OpenWrt](glossary.md#openwrt), [Pigtail](glossary.md#pigtail), [RNDIS](glossary.md#rndis), [URC](glossary.md#urc), [USB mode 40 / 41](glossary.md#usb-mode-40--41).
