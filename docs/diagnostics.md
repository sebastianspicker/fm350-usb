# Diagnostics: `tools/fm350_diag.py`

A diagnostics tool for a Fibocom FM350-GL (including the Dell DW5931e) in a Waveshare USB TO M.2 B KEY adapter, or any similar USB-to-M.2 adapter. It runs the checks from our bench log by itself, reads the results, and tells you what to look at next. It works on macOS (through libusb) and on Linux (through the serial port or libusb).

## In short

- **Start with `python3 tools/fm350_diag.py read`** (stage 0). It only sends read commands to the modem (AT commands, short text commands) and can't change anything, and it's enough for most problems.
- The tool works in stages: stage 0 reads only; stages 1 and 2 change settings and restore them automatically; stage 3 edits a file on the module that can turn USB off if it goes wrong. The higher stages repeat changes we made while debugging our own module (none damaged it), and each one tells you what it's about to do and asks you to confirm first.
- If no cell is measured at all, the tool reports a FAIL and tells you to check the antenna cables (pigtails) first — that was the cause on our own module. See [Stage 0](#stage-0-read).
- **Stage 3 (`dipc`) carries real risk**: a wrong value locks you out of USB until you reach the module over PCIe or reflash it. You don't need it just to use the module over USB.
- Tested on one module (a Dell DW5931e) on 2026-09-25. Your module may behave differently.

## Safety note and disclosure

Please read this before you run anything above stage 0.

- **We tested this on one module.** It's a Dell DW5931e (FM350-GL, firmware 29.20.22, Dell OEM image `5025`) in a Waveshare USB TO M.2 B KEY, tested on 2026-09-25. We ran every stage-1 and stage-2 action on it, and did the stage-3 edit by hand. The module still works. Your module may have different firmware or an OEM image from another vendor, and it may not react the same way.
- **Stage 0 is read-only by construction.** The tool keeps a fixed list of every AT command it's allowed to send, with a stage number for each one. It refuses to send any command that isn't on the list or that belongs to a higher stage than the one you picked. Stage 0 only sends queries (`?`), plain reads such as `AT+CESQ`, and `AT+GTSENRDTEMP=0`, which selects a temperature sensor to read. It never sends a command that sets anything.
- **Stages 1 and 2 restore what they change.** Stage 1 only changes things that reset when the module restarts. Stage 2 changes settings the module keeps. Before it changes one, it reads the current value and writes the exact commands to put it back into `restore.txt`. It restores the value when the experiment ends, even if you press Ctrl-C or something fails, and then reads it back to check. If that check fails, it stops and prints the commands for you to run by hand.
- **Stage 3 can lock you out of USB.** It edits a file on the module's internal system. If that file ends up with the wrong value, the module turns USB off, and after that only PCIe or a reflash with SP Flash Tool can reach it. The tool only writes one exact file content, checks it before and after writing, and refuses to run without a verified backup. It still carries that risk. You don't need it to use the module over USB.
- **What the tool never does:** flash firmware, change the USB mode (`GTUSBMODE`), change SIM detection (`MSMPD`), make an FCC unlock permanent (`GTFCCLOCKMODE`), change the IMEI or serial number (`EGMR`), set an APN, or change the DIPC mode with AT commands. We either didn't need these or didn't try them, so the tool doesn't offer them.
- **No warranty.** The tool is MIT-licensed and provided as is. We're not affiliated with Fibocom, Dell, MediaTek or Waveshare. Changing modem settings can void a warranty and, in the worst case, make a module unusable. You decide what to run, and you're responsible for the result.

## Stages at a glance

| Stage | Command | Changes on the module | Kept after a restart? | Confirmation |
|---|---|---|---|---|
| 0 | `read` | Nothing | – | None |
| – | `backup` | Nothing (it only reads the module over ADB and writes files on your computer) | – | None |
| 1 | `volatile` | Error format, registration reports, a network scan, radio off/on, optional module or USB reset | No | Type `yes`, or pass `--yes` |
| 2 | `experiment <name>` | One setting per experiment, restored at the end | Yes, until it's restored | Type the experiment name, or pass `--accept-risk` |
| 3 | `dipc set-dual` / `dipc revert` | The DIPC config file on the module | Yes | Type `CHANGE DIPC` (there's no flag to skip this) |

`plan <stage>` prints every command a stage can send and what each one changes, without touching the module. `--dry-run` does the same for a single run.

## Requirements

- Python 3.11 or newer. The tool uses only the standard library and the `fm350mac` code in this repo. You don't need to install anything with pip.
- **macOS:** `brew install libusb`. The tool talks to the AT port (USB interface 6) directly. You don't need root.
- **Linux:** if the kernel's `option` driver has bound the modem, the tool finds the AT port (`/dev/ttyUSB*` on interface 6) on its own. You can also pass `--tty /dev/ttyUSB4`. If no `ttyUSB` device appears, run `echo "0e8d 7127 ff" > /sys/bus/usb-serial/drivers/option1/new_id` as root. Your user needs access to the device (for example, membership in the `dialout` group).
- **Optional: `adb`** (`brew install --cask android-platform-tools` or `apt install adb`). You need it for `read --adb`, `backup`, and `dipc`. See [Step 3 of the Dell guide](dell-dw5931e-usb.md#step-3-get-the-adb-root-shell-and-make-a-backup) for what ADB gives you on this module. It's a root shell with no password.
- Close anything else that's using the AT port first, such as `fm350mac up`, ModemManager, the OpenWrt `atc`/`xmm` handlers, or `picocom`. Only one program can use the port at a time.

On an OpenWrt router without Python, use [`openwrt/fm350-status.sh`](../openwrt/fm350-status.sh). It's read-only and covers the most important part of stage 0.

## Quick start

```sh
git clone https://github.com/sebastianspicker/fm350-usb && cd fm350-usb
python3 tools/fm350_diag.py read            # stage 0, samples for 60 s
python3 tools/fm350_diag.py read --adb      # also reads the module's internal state over ADB
```

Each run creates a folder named `fm350-diag-<date>-<time>/` containing:

| File | Contents |
|---|---|
| `report.md` | The results as OK/INFO/WARN/FAIL lines, followed by the suggested next step |
| `report.json` | The same results in a format scripts can read |
| `transcript.txt` | Every command sent and every response |
| `journal.jsonl` | Only from stage 1 up: each change, logged before and after it was sent |
| `restore.txt` | Only for stage 2: the commands that restore the original values, written before anything is changed |

The exit code is 0 when everything is OK, 1 when there's at least one WARN or FAIL, 2 when no modem was found, and 3 when a restore failed.

**Sharing a report.** Reports hide the IMEI, IMSI, ICCID and serial number, as well as the tracking area code and cell ID of every cell. `--no-redact` turns that off, so only use it for your own records. Skim the file before you post it anywhere. Your operator and the bands you use still show.

## Stage 0: `read`

The tool first checks whether the modem is on the USB bus at all (`0e8d:7127`, or `0e8d:7126` in mode 40). If it is, it takes a snapshot of the module's state and then samples the cells and registration every 10 s for 60 s (`--duration`, `--interval`). It needs several samples because one sample can show "searching" on a module that's working fine.

What it checks, and what the result means:

| Check | Commands | What it tells you |
|---|---|---|
| USB presence | `ioreg` / sysfs | Not found: wait 10 to 60 s after power-on, plug in the adapter's power-only connector, and try a different port or hub. |
| Firmware and OEM image | `ATI`, `AT+CGMR`, `AT+GTPKGVER?` | An image ending in `_5025…` is Dell's; see the [Dell guide](dell-dw5931e-usb.md). Any other 4-digit code after the underscore is another laptop vendor's OEM image. |
| DIPC mode | `AT+GTDIPCMODE?` | `1,…` means PCIe Advance Mode (the Dell default). USB still works in an adapter because there's no PCIe link. `3,…` is the stock dual mode. |
| FCC lock | `AT+GTFCCEFFSTATUS?`, `AT+GTFCCLOCKMODE?` | If the second value isn't `1`, the module is locked and the radio stays off. See [stage 2](#stage-2-experiment-name) for the fix. |
| Radio | `AT+CFUN?`, `AT+GTFMODE?`, `AT+GTRXPATHEN?`, `AT+GTANTTUNINGEN?`, `AT+BODYSAREN?`, `AT+ECAL?` | `CFUN` should be `1`. The antenna tuner should be `1`; a `0` is usually left over from an experiment. `ECAL` `1` means the RF calibration is there. |
| SIM | `AT+CPIN?`, `AT+SIMTYPE?`, `AT+GTDUALSIM?`, `AT+MSMPD?`, `AT+CIMI`, `AT+ICCID` | `READY` in slot 0. The Waveshare board only wires SIM slot 1, which the modem reports as slot 0. |
| Access technology and bands | `AT+GTACT?`, `AT+ERAT?`, `AT+E5GOPT?` | A RAT mode other than 21 is unusual. It's usually left over from an earlier experiment, because `ERAT` survives a module reset. |
| Cells and registration (sampled) | `AT+CEREG?`, `AT+C5GREG?`, `AT+COPS?`, `AT+CESQ`, `AT+GTCCINFO?`, `AT+CEER` | See the next section. |
| Temperature | `AT+GTSENRDTEMP=0` | Above 70 °C gets a WARN. |
| APN | `AT+CGDCONT?` | Empty just means no APN has been set yet. |
| ADB (`--adb`) | `adb devices`, `id`, `/etc/vendor_info`, `dipc_config`, and the radio lines from `logread` | `MIPC_NW_RADIO_STATE hw=1, sw=1` means the radio is on at both levels. `offline` in `adb devices` after a module reset is fixed with `volatile --usb-reset`. |

### If no cell is measured at all

If **none** of the samples shows a cell (`AT+CESQ` returns only 99/255 and `AT+GTCCINFO?` is empty), the tool reports a FAIL and tells you to **check the antenna pigtails and connectors first.**

This exact symptom cost us a full day of ruling out software causes before we found a pair of defective pigtails [Bench log, Cable swap]. The full story is in the [Dell guide's troubleshooting section](dell-dw5931e-usb.md#no-cells-at-all-cesq-all-99255-gtccinfo-empty).

Check these in order:

1. Replace the pigtails, and press each MHF4 connector down until it clicks onto the module.
2. Repeat `read` near a window or outdoors. If nothing changes at all, not even the GNSS AGC levels (see the [Dell guide](dell-dw5931e-usb.md#no-cells-at-all-cesq-all-99255-gtccinfo-empty)), the fault is in the RF chain, not in a setting.
3. Only after that, look at stages 1 and 2.

### If cells are measured but the module doesn't register

That usually means a SIM, contract or operator problem, not a hardware one. Run stage 1. It asks the network why it rejected the module (`AT+CEREG=3` reports the reject cause) and runs a full network scan.

## Backup (before stage 2 or 3)

```sh
python3 tools/fm350_diag.py backup            # NV partitions, a few MB
python3 tools/fm350_diag.py backup --raw      # also a raw image of every flash partition (~530 MB)
```

This only reads from the module, over ADB. It writes a `tar` archive of each NV directory (`nvram nvdata nvcfg protect_f protect_s mdota mdota2 mdota3`) and `/proc/mtd`, plus a checksum of every file in `SHA256SUMS`, to `backups/fm350-<date>-<time>/`. Git ignores that folder.

**The backup contains your IMEI and your module's RF calibration.** Don't share it, and never restore it onto a different module. Stage 3 won't run without it.

## Stage 1: `volatile`

```sh
python3 tools/fm350_diag.py volatile                   # asks before it starts
python3 tools/fm350_diag.py volatile --reset --yes     # also restarts the module, no prompt
```

Stage 1 runs a short stage 0 first, then:

| Step | Commands | Effect | How it's undone |
|---|---|---|---|
| Readable errors | `AT+CMEE=2` | Errors come back as text instead of numbers | Resets on restart |
| Reject cause | `AT+CEREG=3`, then samples for 60 s | Registration reports include the network's reject cause | Set back to the previous value, and resets on restart anyway |
| Network scan | `AT+COPS=?` (up to 180 s) | Lists every network the module can hear. Registration pauses during the scan | Registration resumes by itself |
| Radio off and on | `AT+CFUN=4`, 5 s wait, `AT+CFUN=1`, then samples for 60 s | Starts the network search again from scratch | Always ends with the original `CFUN` value |
| `--reset` | `AT+CFUN=15` | Restarts the module. It drops off USB and comes back within about 60 s | – |
| `--usb-reset` | libusb port reset (macOS/libusb only) | Fixes ADB showing `offline` after a module restart | – |

What we saw on our module: an instant, empty `AT+COPS=?` meant the module wasn't really scanning. A real scan takes more than 30 s. Reject cause `114` isn't a standard 3GPP cause (it's MediaTek-internal), and `AT+CEER: 0,NONE` meant the network hadn't rejected anything. An unexpected crash is also possible: when we sent about 2000 USB control requests back to back, the module's firmware crashed. It came back by itself 60 to 90 s later, with all settings intact. Stage 1 doesn't do that, but if the module drops off USB during stage 1, wait 90 s before you unplug it.

## Stage 2: `experiment NAME`

Each experiment tests one setting that we suspected on our module. It reads the current value, writes `restore.txt`, changes the setting, restarts the module if needed, measures for 90 s (`--measure`), restores the original value, and reads it back to check. The report compares cells and registration before and during the experiment.

| Experiment | Change | Restart? | Why you'd try it | What happened on our module |
|---|---|---|---|---|
| `fmode` | `AT+GTFMODE=0,0` (ignore the W_DISABLE# and GNSS-disable pins) | Yes | The radio stays off in an adapter because the board holds W_DISABLE# low | No change. Restored to `1,0` |
| `anttuner` | `AT+GTANTTUNINGEN=0` (antenna tuner off) | Yes | Suspected antenna-tuner pin conflict with the adapter | No change. Restored to `1` |
| `rat-lte` | `AT+ERAT=3` (LTE only) | No | Rules out 5G/NR search problems | No change. Restored to `21`. **`ERAT` survives a module reset**, contrary to the AT manual |
| `fcc-unlock-dell` | `AT+GTFCCLOCKGEN`, then `AT+GTFCCLOCKVER=<response>` with Dell's key `4909b5a4` | No | `GTFCCEFFSTATUS` shows the module is locked (second value not `1`) | Accepted (`+GTFCCLOCKVER: 1`). Our module wasn't locked, so it changed nothing |

Notes:

- `fcc-unlock-dell` only runs on a Dell image (`_5025`) unless you pass `--force-oem`, and it skips modules that are already unlocked unless you pass `--even-if-unlocked`. It never sends `AT+GTFCCLOCKMODE`, so the unlock doesn't become permanent and there's nothing to restore. For other vendors, see mrhaav's [`fm350_fcc_unlock.sh`](https://github.com/mrhaav/openwrt/blob/master/atc/fib-fm350_gl/fm350_fcc_unlock.sh). The Lenovo key is `3df8c719`.
- Experiments that restart the module need it to come back on USB. If it doesn't come back within 120 s, the tool stops and prints `restore.txt`. Unplug the adapter, plug it back in, wait 60 s, then run the commands in `restore.txt` with `tools/fm350_at.py`.
- We didn't include the APN test (`AT+EIAAPN`/`AT+CGDCONT`) as an experiment. Setting an APN is configuration, not diagnostics. Use `fm350mac connect` or the router scripts for that.

## Stage 3: `dipc`

```sh
python3 tools/fm350_diag.py dipc status                                  # read-only
python3 tools/fm350_diag.py dipc set-dual --backup backups/fm350-… --dry-run
python3 tools/fm350_diag.py dipc set-dual --backup backups/fm350-…
python3 tools/fm350_diag.py dipc revert
```

This switches a Dell or other OEM module from "PCIe Advance Mode" (`1,2,2,2,7,13`) to the stock Fibocom dual mode (`3,1,1,1,3,15`) by editing `/mnt/vendor/nvdata/md_cmn/dipc_config` over ADB. It's the only way to change this setting, because `AT+GTDIPCMODE=…` returns `phone failure` on OEM images. Read [the background in the Dell guide](dell-dw5931e-usb.md#background-what-dipc-mode-is-and-why-it-looks-like-a-usb-lock) first.

**You probably don't need this.** In an adapter, USB already works in PCIe Advance Mode. On our module, the switch had no effect on reception. It's only useful if you want USB to stay on while the module sits in a laptop's PCIe slot.

**Why it's risky:** the module's init script only turns USB on when `dual_ipc_mode` is `1` or `3`. With any other value, USB stays off from the next boot onwards, and a USB-only user can't reach the module to fix it.

What the tool does to reduce that risk:

1. It refuses to run without `--backup DIR`, where DIR contains a non-empty `nvdata.tar` and a `SHA256SUMS` file that checks out. It also refuses if the current file can't be parsed or its mode isn't 1 or 3.
2. It asks you to type `CHANGE DIPC`, and refuses if it isn't running in an interactive terminal.
3. It copies the current file to your computer, and to `dipc_config.orig` on the module. It never overwrites an existing `.orig` file, and it also recognises the `.orig-dell` name from our manual run. (The Dell guide's manual [Step 4](dell-dw5931e-usb.md#step-4-optional-switch-from-pcie-advance-mode-to-dual-mode) saves its copy as `dipc_config.orig-dell`; the tool's own copy is `dipc_config.orig`. Both hold the original file.)
4. It uploads the new content to a temporary file on the module, reads it back, and compares it byte for byte. Only then does it copy the content into place, keeping the file's owner and permissions. Every write on the module first checks that its source file exists and isn't empty. A failed copy therefore never leaves `dipc_config` empty.
5. It reads the file back again. If the content doesn't match, it restores the original immediately and stops.
6. It doesn't restart the module itself. The new mode takes effect at the next restart: run `volatile --reset --skip-scan --skip-cfun`, or unplug the adapter and plug it back in. Then run `dipc status`, which should show `3,1,1,1,3,15`.

`dipc revert` puts the original file back the same way. You can also do it by hand:

```sh
adb exec-out 'cd /mnt/vendor/nvdata/md_cmn && cp -p dipc_config.orig dipc_config && sync'
# then AT+CFUN=15
```

On our module, mode 3 has survived every restart since, and USB, AT, ADB and the SIM all kept working.

## Troubleshooting the tool

| Symptom | Fix |
|---|---|
| `FM350 not found` | Stage 0 exits with code 2. Check USB presence as described in [Stage 0](#stage-0-read). |
| `LIBUSB_ERROR_ACCESS` / `BUSY` on Linux | The `option` driver has claimed the interface. Use `--tty` (the tool normally finds it on its own). |
| `LIBUSB_ERROR_ACCESS` on macOS | Another program has the port open, for example `fm350mac up`. Close it. |
| AT commands time out | Another program is using the port, or the module is still booting. Wait 60 s. |
| ADB shows `offline` | `volatile --usb-reset`, or unplug and replug the adapter. `adb kill-server` alone doesn't fix it. |
| `RESTORE FAILED` | Run the commands in `restore.txt` with `uv run --with pyusb tools/fm350_at.py '<command>'`. Every change is listed in `journal.jsonl`. |
| A Linux `ttyUSB` number changed after a restart | Use the stable name under `/dev/serial/by-id/` for `--tty`. |

## Reporting results

If you run the tool on a different module, such as another OEM image, other firmware or another adapter, we'd like to hear how it went. Open an issue with the redacted `report.md`, and name the stage you ran and your hardware. A report from a module that behaved differently from ours is especially useful.

## See also

- [dell-dw5931e-usb.md](dell-dw5931e-usb.md): the field guide these stages are based on
- [bench-log.md](bench-log.md): the dated raw measurements behind every "what happened on our module"
- [at-commands.md](at-commands.md): AT command cheat sheet
- [firmware-reflash.md](firmware-reflash.md): reflash procedure. The tool doesn't do this, and we haven't tested it

## Glossary

Terms used on this page, defined in the [shared glossary](glossary.md): [ADB](glossary.md#adb), [AGC](glossary.md#agc), [APN](glossary.md#apn), [AT command](glossary.md#at-command), [AT port](glossary.md#at-port), [Cell ID / TAC](glossary.md#cell-id--tac), [DIPC mode](glossary.md#dipc-mode), [FCC lock](glossary.md#fcc-lock), [GNSS](glossary.md#gnss), [IMEI / IMSI / ICCID](glossary.md#imei--imsi--iccid), [libusb](glossary.md#libusb), [LTE / NR](glossary.md#lte--nr), [MHF4 / IPEX-4](glossary.md#mhf4--ipex-4), [NV partitions / calibration](glossary.md#nv-partitions--calibration), [OEM image](glossary.md#oem-image), [OpenWrt](glossary.md#openwrt), [Pigtail](glossary.md#pigtail), [Protocol handler](glossary.md#protocol-handler), [RAT](glossary.md#rat), [SP Flash Tool](glossary.md#sp-flash-tool), [USB mode 40 / 41](glossary.md#usb-mode-40--41), [W_DISABLE#](glossary.md#w_disable).
