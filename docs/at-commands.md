# FM350-GL AT command cheat sheet

A quick reference for the AT commands used elsewhere in this repo to identify the module, check registration, bring up a data session, and read its FCC-lock status. It's not the full command set — see the *Fibocom FM350 AT Commands User Manual V2.10* (344 pages) for anything not listed here. Standard 3GPP commands work as usual; Fibocom-specific commands start with `+GT`.

## In short

- Send commands over the AT port (see below for how to reach it). Commands ending in `?` only read a value; commands with `=` usually set one.
- Some settings survive a module reset. Where the sources say a command's effect persists or doesn't, it's marked in the tables below: `AT+GTUSBMODE` is persistent, `AT+CFUN=15` resets the module, `AT+GTFCCLOCKMODE=0` makes an unlock persistent, `AT+ERAT` persists across a reset even though the manual says otherwise [Dell guide, Other pitfalls], and `AT+GTACT` does **not** persist [Bench log].
- This isn't the full command set — see the linked manual for anything not listed here.

## How to reach the AT port

AT port: USB interface 6 (mode 41) or 4 (mode 40), usually `/dev/ttyUSB4`. Only one program may hold the port at a time.

- **Linux / OpenWrt:** `picocom -b 115200 --echo /dev/ttyUSB4` — see [setup guide, step 2](setup-guide.md#2-check-that-the-modem-enumerates-any-firmware).
- **macOS** (no serial driver for these interfaces): `uv run --with pyusb tools/fm350_at.py 'ATI' 'AT+CPIN?'` — see [dell-dw5931e-usb.md, step 2](dell-dw5931e-usb.md#step-2-get-an-at-shell).

## Identity and status

| Command | Purpose | Persists? |
|---|---|---|
| `ATI` | Model information | |
| `AT+CGMR` | Firmware version | |
| `AT+CGSN` | IMEI | |
| `AT+CIMI` / `AT+ICCID` | IMSI / SIM ICCID | |
| `AT+CPIN?` | SIM status (`READY`) | |
| `AT+CFUN?` / `AT+CFUN=1` / `AT+CFUN=4` | Radio state / on / airplane | |
| `AT+CFUN=15` | Reset the module (`<fun>` 15 = reset in the manual) | Resets the module |

## Registration and signal

| Command | Purpose | Persists? |
|---|---|---|
| `AT+COPS?` | Current operator and access technology | |
| `AT+CEREG?` / `AT+C5GREG?` | LTE / NR registration (`,1` home, `,5` roaming) | |
| `AT+CESQ` | Signal quality (RSRQ/RSRP, encoded) | |
| `AT+GTCCINFO?` | Serving and neighbour cell info (band, PCI, RSRP, SINR) | |
| `AT+GTCAINFO?` | Carrier aggregation status | |
| `AT+GTACT?` / `AT+GTACT=?` | Read / list RAT and band selection | **Not persistent**: falls back to all bands after a reset [Bench log] |
| `AT+ERAT?` / `AT+ERAT=<n>` | Read / set the RAT mode (`3` = LTE only, `21` = default on our unit) | **Persists across `AT+CFUN=15`**, contrary to the AT manual; restore it after experiments [Dell guide, Other pitfalls] |

## Data session

| Command | Purpose | Persists? |
|---|---|---|
| `AT+CGDCONT=1,"IP","<apn>"` | Define PDP context 1 (`IPV4V6` for dual stack) | |
| `AT+CGAUTH=1,<type>,"<user>","<pass>"` | APN authentication, if needed | |
| `AT+CGACT=1,1` | Activate context 1 | |
| `AT+CGPADDR=1` | IP address assigned by the network, used as a static address on the RNDIS interface | |
| `AT+GTDNS=1` | Primary and secondary DNS for context 1 | |
| `AT+CGACT=0,1` | Deactivate | |

## Hardware and USB

| Command | Purpose | Persists? |
|---|---|---|
| `AT+GTUSBMODE?` | USB composition: 40 = `0e8d:7126`, 41 = `0e8d:7127` (default) | **Persistent**; takes effect after reset |
| `AT+GTFMODE?` | Hardware W_DISABLE# / GNSS pin control (check if the radio won't turn on in the adapter) | |
| `AT+GTSENRDTEMP=<id>` | Thermal sensor reading | |
| `AT+GTDUALSIM?` / `AT+GTDUALSIM=0` | Active SIM slot (0 = SIM1; the Waveshare board only wires one slot) | |

## FCC lock

| Command | Purpose | Persists? |
|---|---|---|
| `AT+GTFCCEFFSTATUS?` | `<mode>,<status>`. Mode 0 none / 1 one-time / 2 every power-up. Status 1 = unlocked | |
| `AT+GTFCCLOCKGEN` | Get the challenge | |
| `AT+GTFCCLOCKVER=<response>` | Send the response (see `fm350_fcc_unlock.sh`) | |
| `AT+GTFCCLOCKMODE=0` | Make the unlock persistent (after a successful unlock) | **Persistent** |

Commands the FM350 does **not** support (community reports): `AT+GTRNDIS`, `AT+GTRAT`, all Quectel `AT+QCFG` / `AT+QNWPREFCFG` / `AT+QENG`.

## Glossary

Terms used on this page, defined in the [shared glossary](glossary.md): [APN](glossary.md#apn), [AT command](glossary.md#at-command), [AT port](glossary.md#at-port), [Band / EARFCN / PCI](glossary.md#band--earfcn--pci), [FCC lock](glossary.md#fcc-lock), [IMEI / IMSI / ICCID](glossary.md#imei--imsi--iccid), [PDP context / data session](glossary.md#pdp-context--data-session), [RAT](glossary.md#rat), [RSRP / RSRQ / SINR](glossary.md#rsrp--rsrq--sinr), [W_DISABLE#](glossary.md#w_disable).
