# FM350-GL AT command cheat sheet

A quick reference for the AT commands used elsewhere in this repo to identify the module, check registration, bring up a data session, and read its FCC-lock status. It's not the full command set — see the *Fibocom FM350 AT Commands User Manual V2.10* (344 pages) for anything not listed here. Standard 3GPP commands work as usual; Fibocom-specific commands start with `+GT`.

AT port: USB interface 6 (mode 41) or 4 (mode 40), usually `/dev/ttyUSB4`. Only one program may hold the port at a time.

## Identity and status

| Command | Purpose |
|---|---|
| `ATI` | Model information |
| `AT+CGMR` | Firmware version |
| `AT+CGSN` | IMEI |
| `AT+CIMI` / `AT+ICCID` | IMSI / SIM ICCID |
| `AT+CPIN?` | SIM status (`READY`) |
| `AT+CFUN?` / `AT+CFUN=1` / `AT+CFUN=4` | Radio state / on / airplane |
| `AT+CFUN=15` | Reset the module (`<fun>` 15 = reset in the manual) |

## Registration and signal

| Command | Purpose |
|---|---|
| `AT+COPS?` | Current operator and access technology |
| `AT+CEREG?` / `AT+C5GREG?` | LTE / NR registration (`,1` home, `,5` roaming) |
| `AT+CESQ` | Signal quality (RSRQ/RSRP, encoded) |
| `AT+GTCCINFO?` | Serving and neighbour cell info (band, PCI, RSRP, SINR) |
| `AT+GTCAINFO?` | Carrier aggregation status |
| `AT+GTACT?` / `AT+GTACT=?` | Read / list RAT and band selection |

## Data session

| Command | Purpose |
|---|---|
| `AT+CGDCONT=1,"IP","<apn>"` | Define PDP context 1 (`IPV4V6` for dual stack) |
| `AT+CGAUTH=1,<type>,"<user>","<pass>"` | APN authentication, if needed |
| `AT+CGACT=1,1` | Activate context 1 |
| `AT+CGPADDR=1` | IP address assigned by the network, used as a static address on the RNDIS interface |
| `AT+GTDNS=1` | Primary and secondary DNS for context 1 |
| `AT+CGACT=0,1` | Deactivate |

## Hardware and USB

| Command | Purpose |
|---|---|
| `AT+GTUSBMODE?` | USB composition: 40 = `0e8d:7126`, 41 = `0e8d:7127` (default). Persistent; takes effect after reset |
| `AT+GTFMODE?` | Hardware W_DISABLE# / GNSS pin control (check if the radio won't turn on in the adapter) |
| `AT+GTSENRDTEMP=<id>` | Thermal sensor reading |
| `AT+GTDUALSIM?` / `AT+GTDUALSIM=0` | Active SIM slot (0 = SIM1; the Waveshare board only wires one slot) |

## FCC lock

| Command | Purpose |
|---|---|
| `AT+GTFCCEFFSTATUS?` | `<mode>,<status>`. Mode 0 none / 1 one-time / 2 every power-up. Status 1 = unlocked |
| `AT+GTFCCLOCKGEN` | Get the challenge |
| `AT+GTFCCLOCKVER=<response>` | Send the response (see `fm350_fcc_unlock.sh`) |
| `AT+GTFCCLOCKMODE=0` | Make the unlock persistent (after a successful unlock) |

Commands the FM350 does **not** support (community reports): `AT+GTRNDIS`, `AT+GTRAT`, all Quectel `AT+QCFG` / `AT+QNWPREFCFG` / `AT+QENG`.
