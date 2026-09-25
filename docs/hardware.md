# Hardware

Specs for the three parts of this setup, drawn from vendor datasheets and our own bench measurements. Useful if you're sourcing the same parts, checking a substitute, or trying to figure out why something doesn't fit.

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

## Waveshare USB TO M.2 B KEY (SKU 23252)

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
| SIM | Dual SIM (one can be eSIM on some variants); the Waveshare board only wires one SIM slot |

### USB personality

| `AT+GTUSBMODE` | USB ID | Interfaces | AT port |
|---|---|---|---|
| 40 | `0e8d:7126` | RNDIS + AT + AP(GNSS) + META + DEBUG + NPT + ADB | USB interface 4 |
| 41 (default) | `0e8d:7127` | as 40 + AP(LOG) + AP(META) | USB interface 6 |

- Interfaces 0–1 are RNDIS and use the `rndis_host` driver (`kmod-usb-net-rndis`).
- The other interfaces are vendor-specific serial ports and use the `option` driver (`kmod-usb-serial-option`). The FM350 IDs were added to mainline `option` in June 2024 (in 6.6+ and backported to 5.10.222). **Kernel 5.4 (GL stock) does not have them**; there you add them at runtime with `new_id`.
- There is no MBIM or QMI in USB mode. **ModemManager does not handle it.** Use AT-command scripts instead.
- The PCIe path (`kmod-mtk-t7xx`) exists, but the Waveshare board is USB-only, and forum users report the t7xx path is unstable.

### German mobile networks

We assumed the router runs in Germany. Deutsche Telekom, Vodafone and O2/Telefónica mainly use LTE B1, B3, B7, B8, B20, B28, B32 and NR n1, n3, n7, n28, n78, all of which the FM350-GL supports.

## Power budget

- Flint 2 USB port: 5 V / 2 A = 10 W.
- The FM350-GL datasheet does not give a figure we could verify. 5G modules draw short current spikes when attaching to the network and during uplink bursts. Waveshare recommends 5 V / 3 A for its 5G boards.
- Our plan: plug the data+power plug into the Flint 2 and the power-only plug into a separate 5 V ≥ 2 A USB charger. The port is powered even when the router has no working uplink, which is exactly when failover is needed, so keep the charger on the same UPS or circuit as the router.

## Thermal

The FM350 runs hot under sustained 5G load. Use the included thermal pad between the module and the aluminium lid, and don't enclose the dongle. Watch `AT+GTSENRDTEMP` during load tests (see [at-commands.md](at-commands.md)).
