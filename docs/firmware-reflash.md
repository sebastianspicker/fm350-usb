# Firmware reflash: FM350-GL to generic Fibocom firmware

A hobbyist-assembled procedure for reflashing an FM350-GL/DW5931e from its
OEM laptop firmware to generic Fibocom firmware, over USB, with MediaTek SP
Flash Tool (SP Flash Tool: MediaTek's Windows program for writing firmware
to the chip) on Windows. It's for anyone whose module genuinely won't
register on any network after ruling out the hardware causes in
[dell-dw5931e-usb.md](dell-dw5931e-usb.md#no-cells-at-all-cesq-all-99255-gtccinfo-empty).

## Status: never run, and not needed on our unit

- **We have not run this procedure.** It is untested, end to end, on any module in this project.
- **Not needed for our unit.** We wrote it while DIPC mode (the module setting that decides whether the host talks to it over PCIe, USB or both) looked like the likely cause of our module registering no cell. It turned out to be a pair of defective antenna pigtails instead (see [bench-log.md](bench-log.md) and [dell-dw5931e-usb.md](dell-dw5931e-usb.md)); once we replaced them, the module registered fine on its original OEM firmware.
- **Brick risk, with no documented recovery.** No source found for this document describes a confirmed way to recover a module left with no working firmware after a failed or interrupted flash — see [Known failure modes](#known-failure-modes-from-sources) and [Rollback/recovery](#rollbackrecovery).
- **Who should even consider this:** only someone with an OEM FM350-GL/DW5931e that still won't register *after* the hardware is confirmed good — antenna cables and connectors first, per the [Dell guide's troubleshooting](dell-dw5931e-usb.md#no-cells-at-all-cesq-all-99255-gtccinfo-empty).
- We're keeping the procedure here, untested, as reference for OEM FM350-GL/DW5931e units that still won't register after the hardware is confirmed good. Read [Known failure modes](#known-failure-modes-from-sources) and [Sources](#sources) before starting, and re-check the Microsoft Update Catalog listing yourself — see [Open questions on the firmware package](#open-questions-on-the-firmware-package).

## In short

- **Decision: probably don't.** This is untested end to end, assembled from hobbyist guides and a Microsoft driver package never intended for standalone use, on a module class with no vendor recovery path.
- **If you must, flash a second FM350-GL unit first**, if one is available — don't experiment on your only sample.
- **Only ever use `Download Only` mode** in SP Flash Tool. `Format All + Download` erases the IMEI; `Firmware Upgrade`'s partition handling is undocumented and risky (see [Flashing](#flashing)).
- **Back up first.** NV partitions (module storage holding the IMEI and factory radio calibration) can't be recreated if lost.
- **What's untested:** the whole procedure, whether `Download Only` actually resets DIPC mode (the thing this flash is meant to prove), and any recovery path if something goes wrong.

## Why this procedure exists

Our module (see [bench-log.md](bench-log.md), 2026-09-25) registered no cell
on any RAT despite a valid SIM, a full antenna connection, and no FCC lock.
It carries OEM laptop firmware (`AT+GTPKGVER?` →
`81600.0000.00.29.20.22_5025.0000.040.000.038_C69`) locked into
`AT+GTDIPCMODE?` = `1,2,2,2,7,13` ("PCIe Advance Mode"), which can't be
changed with AT commands (`+CME ERROR: phone failure` on every write
attempt). An HP unit with the same kind of lock (`1,2,2,2,5,13`, OpenWrt
forum #346–#351) also couldn't change it with AT, though that thread doesn't
show it working over USB — so DIPC mode was a plausible suspect, not a
confirmed one.

The seller then confirmed the module is a **Dell DW5931e** (OEM image 5025).
Dell units are reported working over USB-only adapters **on Dell's own
firmware** (forum #156 via Dell's driver package, #380 on 29.23.08). Dell's
firmware is also version-sensitive: a Dell community thread reports package
6.0.3.76 broke cellular connectivity while 6.0.3.66 worked. That made
**Dell's current firmware package (driver ID `f34xk`)** the preferred
target — it keeps Dell's board ID and SAR/antenna configuration — with the
generic Microsoft Update Catalog image below as the fallback.

Before touching anything, we took a **full raw backup of all 40 flash
partitions** (plus file-level archives of nvram/nvdata/protect/mdota) over
the module's ADB port: `backups/fm350-<serial>-<timestamp>/` (SHA-256
recorded; `expdb` short by 2 MB, not needed). This includes `mtd0`
(preloader), needed for the hardware-variant check in [Hardware/preloader
variant matching](#hardwarepreloader-variant-matching).

We then ruled out in software: the Dell FCC unlock (accepted, challenge
`0x00000000`, no effect), thermal throttling, SIM files, IMEI, tunable
antenna off, LTE-only, and the attach APN — the same checks, with their
results, are logged in full in the [bench log](bench-log.md). The module's
own log showed the radio on (`hw=1 sw=1`) and the modem core searching
without finding a cell — which is when we started drafting this procedure.
The actual cause, found afterwards, is the pigtails (see the note at the
top of this page).

**This is a firmware flash on a module that is not on any vendor's supported
list for this use case, using files, tools and steps assembled by hobbyists
from a Microsoft driver package never intended for standalone use.** Risks,
in descending order of severity:

## Risks

- **Brick.** No source in this document's research describes a confirmed
  recovery path for a module left with no working firmware after a failed
  or interrupted flash (see [Known failure modes](#known-failure-modes-from-sources)).
  One forum report (#114) recovered a *crashed* (not bricked) module by
  reflashing with SP Flash Tool — that is not the same as recovering a
  module bricked *by* SP Flash Tool.
- **Loss of IMEI, calibration data (`AT+ECAL?`) or the module serial**
  (`AT+EGMR=0,5`) if the wrong SP Flash Tool mode is used (`Format All +
  Download` or `Firmware Upgrade` — see [Flashing](#flashing)). `Download
  Only` mode, which every source recommends, is reported not to touch these
  partitions, but this is not independently verified against our exact unit.
- **Wrong preloader for the hardware variant.** The firmware package bundles
  multiple preloader files for different FM350-GL sub-variants (see
  [Preparing the firmware folder](#preparing-the-firmware-folder)). Sources
  disagree on how certain the match needs to be, and getting it wrong risks
  a partition-layout mismatch that only a full-format flash can fix — which
  is itself the highest-risk mode.
- **No RMA / warranty path.** This module is already an OEM pull of unknown
  provenance; there is no vendor support to fall back on.

Do not attempt this on the only sample of the module without a plan for
what happens if it doesn't come back. If a second FM350-GL unit is
available, flash that one first.

## Prerequisites

| Item | Detail |
|---|---|
| Windows PC | Windows 10/11 x64. All sources tested on Windows only; SP Flash Tool has no macOS/Linux build (an unofficial Linux mtkclient exists but is not what these sources used and is not covered here). |
| USB connection | The Waveshare "USB TO M.2 B KEY" adapter, module installed, connected directly to a PC USB port (avoid hubs during flashing — a drop-out mid-write is exactly the failure mode to avoid). |
| SP Flash Tool | **v6.2124** exactly. Sources are explicit that v5 does not work (older download-agent protocol) and versions newer than v6.2124 use an incompatible protocol with this module. Download: https://spflashtools.com/windows/sp-flash-tool-v6-2124 (third-party mirror; SP Flash Tool is not distributed by MediaTek to the public — this is the same download link all three sources point to). |
| MediaTek USB drivers | "MediaTek USB VCOM" / preloader drivers, needed so Windows recognises the module in BROM/preloader (the first boot stages of a MediaTek chip, used by SP Flash Tool to write firmware) download mode. One source (the Chinese WLGH01 guide) links `https://mtkdriver.com/mtk-driver-v5-2307`. Not independently verified against this exact module; generic MediaTek VCOM driver packages are widely mirrored (e.g. from XDA/thecustomdroid) if that link is unavailable. Install before connecting the module in flash mode. Expect a Windows "driver not signed" warning (Code 10) on first install on some Windows versions — sources don't document a fix beyond reinstalling/enabling test-signing; not verified here. |
| ADB (optional, for backup) | Android Platform-Tools (`adb.exe`) if you attempt the ADB-based backup below. The FM350 exposes an ADB (Android Debug Bridge; here it gives a root shell on the modem's internal Linux system, with no password) interface in USB mode 41 (confirmed in [bench-log.md](bench-log.md): "5 ADB"; also documented in the FM350 AT Commands manual's `AT+GTUSBMODE` mode list, which spells out mode 41 as "RNDIS+AT+AP(GNSS)+META+DEBUG+NPT+ADB+AP(LOG)+AP(META)"). **ADB root access itself is verified** — on macOS, this module gives an unauthenticated root ADB shell over USB [Dell guide, Step 3]. What's **not verified** is whether Windows will attach a normal ADB driver to that same interface, and whether that holds for OEM images other than this Dell unit. |
| Firmware package | See [Preparing the firmware folder](#preparing-the-firmware-folder) — downloaded from the Microsoft Update Catalog, not from Fibocom directly. |
| A hex editor | For the preloader hardware-ID check (e.g. https://hexed.it/, used by one source; any offline hex editor works). |
| Windows `certutil` | Built in, used for the checksum step below. |

## Before you start: record current state

Run every command below and save the raw output before touching anything.
These are the values you compare against after flashing, and some (IMEI,
serial) are close to irreplaceable if something goes wrong.

You can run these from macOS right now, from the repo root, without any
Windows setup, using the existing tool:

```sh
fm350mac/.venv/bin/fm350mac at 'ATI' 'AT+CGMR' 'AT+GTPKGVER?' 'AT+GTCUSTPACKVER?' \
  'AT+GTCFGELEMVER?' 'AT+GTCUSTDATAVER?' 'AT+CGSN' 'AT+EGMR=0,5' 'AT+ECAL?' \
  'AT+GTDIPCMODE?' 'AT+GTFMODE?' 'AT+GTUSBMODE?' 'AT+GTFCCEFFSTATUS?' \
  'AT+GTACT?' 'AT+CGDCONT?'
```

| Command | What it records | Why it matters after flashing |
|---|---|---|
| `ATI` | Manufacturer/model/revision block | Baseline identity string |
| `AT+CGMR` | Firmware revision (e.g. `81600.0000.00.29.20.22`, SVN) | Confirms the flash actually changed the firmware version |
| `AT+GTPKGVER?` | Full package version incl. OEM custom image/data tags | Confirms OEM customisation is gone (or not) post-flash |
| `AT+GTCUSTPACKVER?` | OEM customisation pack version | Same |
| `AT+GTCFGELEMVER?` | Config element version | Same |
| `AT+GTCUSTDATAVER?` | Device data version | Same |
| `AT+CGSN` | IMEI | **Must be identical before and after.** If it changes or reads blank, stop and do not proceed to any further step that could make it worse |
| `AT+EGMR=0,5` | Module serial (ours: redacted) | Should survive a `Download Only` flash |
| `AT+ECAL?` | RF calibration present flag | Should survive a `Download Only` flash; if it flips to "not calibrated" post-flash, the module may need factory calibration you cannot perform yourself |
| `AT+GTDIPCMODE?` | DIPC mode (ours: locked at `1,2,2,2,7,13`) | The value you're hoping the flash resets to the generic default (`3,1,1,1,3,15` per bench-log) — **not confirmed by any source that flashing OEM_OTA/OP_OTA actually resets this; that is an inference, verify empirically after flashing** |
| `AT+GTFMODE?` | Hardware pin-control mode | Baseline |
| `AT+GTUSBMODE?` | USB composition mode | Baseline (expect 41) |
| `AT+GTFCCEFFSTATUS?` | FCC lock status | Confirms it's still unlocked (`0,1`) after flashing |
| `AT+GTACT?` | Band table | Baseline — not persistent across resets anyway per bench-log, low priority |
| `AT+CGDCONT?` | PDP contexts | Baseline, expected empty |

Save this output to a text file outside the repo (it contains the IMEI and
serial — do not commit it).

## Backup

This repo already has its own ADB-based backup method, used for the much
smaller, reversible DIPC edit rather than a full reflash — see [Dell guide,
Step 3](dell-dw5931e-usb.md#step-3-get-the-adb-root-shell-and-make-a-backup)
and [Diagnostics, Backup](diagnostics.md#backup-before-stage-2-or-3). The
method below is specific to this flashing procedure, comes from a different
source (WLGH01), and pulls different paths (`/dev/mtd0`, `/dev/mtd`) than
those two.

**SP Flash Tool's own Readback function cannot be relied on for the
partitions that matter.** The WLGH01 (Chinese) guide states plainly that
attempting to read protected partitions like `nvdata` with SP Flash Tool
fails ("Security deny" errors), and gives no memory addresses for a
Readback of those partitions — so this document doesn't invent any. If a
source had documented working Readback addresses, they would be listed
here; none did.

The same guide instead backs up over ADB (see the caveats on Windows ADB
drivers and shell access in [Prerequisites](#prerequisites)):

```text
adb pull /dev/mtd0 C:\FM350\mtd0
adb pull /dev/mtd  C:\FM350\mtd
```

`mtd0` is the preloader partition (also used for the hardware-ID check
below); `/dev/mtd` is pulled as a directory of the other MTD-backed
partitions. The source's own caveats, carried over here unverified against
our unit:

- Check that the pulled files are non-empty and not filled with `0xFF`
  before trusting them as a backup, especially the `nv*` partitions.
- Try it, but treat "ADB backup didn't work" as informative, not
  blocking — it doesn't mean the flash itself will fail.
- A successful backup does **not** mean a documented restore procedure
  exists. No source describes writing these files back. Treat it as a
  forensic/last-resort artefact, not a guaranteed rollback.

If ADB access fails, proceed without a partition backup — this matches
what most of the community reports appear to have done, given `Download
Only` mode's design (see [Flashing](#flashing)).

## Preparing the firmware folder

### Which package

All primary sources point at the same Microsoft Update Catalog entry:

> **`Fibocom Wireless Inc. - Firmware - 3500.5003.2306.7`**
> https://www.catalog.update.microsoft.com/Search.aspx?q=Firmware+3500*

Downloading it produces the resulting module firmware
`81600.0000.00.29.23.06`. One separate, less detailed OpenWrt forum report
(#425) mentions ending up on `81600.0000.00.29.23.28` after flashing "an
image from the Microsoft Update Catalog", without stating the exact package
name/version used to get there — so a newer `3500.5003.2306.x` revision
than `.7` plausibly exists, but this document did not manage to confirm one.
**See [open questions](#open-questions-on-the-firmware-package) below —
search the catalog yourself at flash time and record exactly what you
downloaded.**

### File layout

Working from the mrhaav/digtvbg walkthrough (word-for-word identical between
those two, both crediting the same 4pda post):

1. Download the `.cab` from the catalog link above.
2. Extract the `.cab` (7-Zip or Windows' built-in extractor), then extract
   the `FwPackage.flz` inside it (also a 7-Zip-openable archive).
3. Create a working folder, e.g. `C:\FM350\81600.0000.00.29.23.06\`.
4. Copy the `download_agent` folder into it (contains `flash.xml` and
   `auth_sv5.auth`, used later by SP Flash Tool itself — not copied into the
   firmware folder, kept where they are).
5. Copy `Scatter.xml` into the working folder.
6. Copy `OEM_OTA_5000.0002.018.img` and `OP_OTA_302.011.img` into the working
   folder, then rename:
   - `OEM_OTA_5000.0002.018.img` → `OEM_OTA.img`
   - `OP_OTA_302.011.img` → `OP_OTA.img`
7. Copy everything from the `FM350.F09` folder into the working folder.
8. From `FM350.F09_preloader`, copy `FM350.F09_loader_ext-verified_00.10.img`
   and `FM350.F09_preloader_35001CF8_00.10.bin` into the working folder,
   then rename:
   - `FM350.F09_loader_ext-verified_00.10.img` → `loader_ext-verified.img`
   - `FM350.F09_preloader_35001CF8_00.10.bin` → `preloader_k6880v1_mdot2_datacard.bin`
9. If a `DEV_OTA.img` file is not present in the package, that's expected —
   it isn't in every package build (per the WLGH01 guide). You will uncheck
   its row in SP Flash Tool rather than add it (see [Flashing](#flashing)).

The exact filenames above (`OEM_OTA_5000.0002.018.img`,
`FM350.F09_preloader_35001CF8_00.10.bin`, etc.) are **version-specific to
the `3500.5003.2306.7` package**. If you end up with a different/newer
catalog package, the version numbers embedded in the filenames will differ
— match by the *pattern*, and cross-check every name against `Scatter.xml`
inside your own downloaded package, not against this list. The WLGH01 guide
independently found the mechanism for this: open `FwPackageInfo.xml` inside
the extracted package and look for `Subsysid="default"` to see which
subfolders and files that scatter file actually wants — treat that as the
authoritative list for whatever package you actually have.

### Hardware/preloader variant matching

The firmware package bundles **more than one preloader binary**, for
different underlying FM350-GL hardware sub-variants. This document found
two different naming schemes in different package snapshots:

- `3500.5003.2306.7` (mrhaav/digtvbg): folder `FM350.F09_preloader`, single
  preloader file `FM350.F09_preloader_35001CF8_00.10.bin` referenced (no
  alternatives shown in that walkthrough — either that package snapshot only
  contained one preloader for the "F09" folder, or the guide's author didn't
  need to choose).
- An older/different package snapshot (WLGH01, referencing package
  `81600.0000.00.29.22.06`, one version behind ours): folder
  `FM350.E09_preloader`, and a **separate** `FM350.E41_preloader_*` set with
  three variants — `8A3A103C`, `35001CF8`, `8914103C` — that must be told
  apart.

**How the WLGH01 guide matches the variant** (their exact procedure): open
your own backed-up `mtd0` (the preloader partition, pulled via ADB per
[Backup](#backup) above — this is why that backup step matters, not just as
a safety net but as an input to this step) in a hex editor, go to offset
`0x40100`, read the 4 bytes there, and reverse their byte order. Example
given in the source: bytes `3C 10 14 89` at that offset reverse to
`8914103C`, which is the hardware ID — pick the preloader file whose
filename contains that hex string and rename it to
`preloader_k6880v1_mdot2_datacard.bin`.

**This offset (`0x40100`) and this procedure come from a single source**
(WLGH01's Chinese-language guide) and are **not corroborated** by the other
two sources (mrhaav/digtvbg, and the fragments recovered from 4pda via
web search). Treat `0x40100` as documented-but-single-source, not as an
established fact. If your package (whatever version you actually download)
contains only one preloader file for the relevant model folder — as the
`3500.5003.2306.7` walkthrough appears to — this step may not apply; if it
contains several, do the hex check before flashing rather than guessing.

Our own module's `ATI`/`AT+CGMR` output (`81600.0000.00.29.20.22`, SVN 05)
does **not** document a hardware ID or preloader variant in any
human-readable form — no source in this research showed a way to read the
preloader hardware ID over AT commands. The only documented way to learn it
is reading the raw `mtd0` partition, which itself requires the ADB access
noted in [Backup](#backup) as unverified for Windows. If ADB access doesn't
work and your downloaded package offers multiple preloader candidates, you
do not have a documented way to pick the right one — that is a real,
unresolved gap in this procedure, not an oversight.

### Checksums

None of the sources describe a checksum step — record hashes yourself so
you have a reference for the exact bytes you flashed, and can diff against
a re-download if something looks wrong later:

```text
certutil -hashfile flash.xml SHA256
certutil -hashfile auth_sv5.auth SHA256
certutil -hashfile Scatter.xml SHA256
certutil -hashfile OEM_OTA.img SHA256
certutil -hashfile OP_OTA.img SHA256
certutil -hashfile loader_ext-verified.img SHA256
certutil -hashfile preloader_k6880v1_mdot2_datacard.bin SHA256
```

Run this for every file in the working folder (adjust names to match what
you actually have) and save the output text alongside your pre-flash AT
command log.

## Flashing

**Read this whole section before clicking anything.** SP Flash Tool offers
three modes; only one of them is what every source recommends:

| Mode | What it does | Source consensus |
|---|---|---|
| **Format All + Download** | Fully erases all memory, repartitions, writes firmware. | **Never use.** Explicitly documented (WLGH01) to erase IMEI and all module identity. |
| **Firmware Upgrade** | Saves "important" partitions to the PC, formats everything, repartitions, writes firmware, restores the saved partitions. | **Do not use unless you have already verified this specific behaviour elsewhere.** WLGH01 calls this mode dangerous/brick-risk too, and does not confirm which partitions actually get saved/restored ("(？)" — the source itself is unsure). |
| **Download Only** | Formats *only* the partitions actually included in the selected file list (the scatter/xml you loaded); does not touch partitions like `nvdata` that aren't in that list. | **This is what every source uses.** Described as "relatively safe" (WLGH01) specifically because IMEI/calibration live outside the set of partitions this firmware package writes. |

1. Close any AT/serial connection to the module (only one program can hold
   the port).
2. Physically disconnect the module from the PC.
3. Install the MediaTek USB VCOM drivers if not already installed (see
   [Prerequisites](#prerequisites)).
4. Launch SP Flash Tool v6.2124 **as Administrator**.
5. Go to the **Download** tab.
6. Load:
   - **Download-Agent / Download-XML**: `download_agent\flash.xml`
   - **Authentication File**: `download_agent\auth_sv5.auth`
7. SP Flash Tool will populate the file/partition list from `Scatter.xml` in
   your working folder — verify it, and if a `DEV_OTA.img` row exists but
   you don't have that file, **uncheck that row** rather than leaving it
   pointed at a missing file.
8. Set the mode selector to **Download Only**. Do not select Format All or
   Firmware Upgrade.
9. Double-check every checked row's path resolves into your working folder
   (not the extracted package's original scattered subfolders) — this is
   where a missed rename from [Preparing the firmware folder](#preparing-the-firmware-folder)
   would show up as a red/missing-file row.
10. Click **Download**. The tool will show a "waiting for device" state
    (this is the intended way BROM/preloader mode gets entered on an M.2
    module with no physical buttons — SP Flash Tool arms itself first, then
    the USB (re)connect event itself is what the module needs; none of the
    sources describe a button combination, only "connect the modem to your
    computer" at this point).
11. Connect the module to the PC now (plug in the USB cable, or if it was
    already connected, replug it). Windows should briefly enumerate a
    MediaTek preloader/BROM USB device, which the VCOM driver should claim.
12. SP Flash Tool should detect the device and start writing automatically.
    None of the sources describe expected progress-bar colours, timing, or
    a "success" screenshot in enough detail to reproduce here — **this is
    not documented in sources.** In general MediaTek SP Flash Tool
    convention (not confirmed for this exact module/build) a green
    checkmark/circle indicates success and red indicates failure; treat any
    error dialog as a stop condition, not something to retry blind.
13. On success, **do not immediately disconnect** — let the tool finish and
    show its final state. Then disconnect, wait a few seconds, and power
    the module normally (reconnect to the Waveshare adapter's normal USB
    port / reboot the host if it was in a router already).
14. If flashing produces an error, **read `host.log`** in the SP Flash Tool
    installation folder before retrying — the WLGH01 guide specifically
    points at this file as the place to see the actual
    module communication, and warns that an error commonly means the
    selected firmware's partition layout doesn't match the module's current
    one, which only a full-format flash (the mode you're trying to avoid)
    can resolve. Do not escalate to Format All/Firmware Upgrade without
    understanding what specifically failed.

## After flashing: verification

Reconnect and re-run the same AT command set as
[Before you start](#before-you-start-record-current-state):

```sh
fm350mac/.venv/bin/fm350mac at 'ATI' 'AT+CGMR' 'AT+GTPKGVER?' 'AT+GTCUSTPACKVER?' \
  'AT+GTCFGELEMVER?' 'AT+GTCUSTDATAVER?' 'AT+CGSN' 'AT+EGMR=0,5' 'AT+ECAL?' \
  'AT+GTDIPCMODE?' 'AT+GTFMODE?' 'AT+GTUSBMODE?' 'AT+GTFCCEFFSTATUS?' \
  'AT+GTACT?' 'AT+CGDCONT?'
```

Expected/hoped-for results, each flagged by how well-supported it is:

| Check | Expectation | Confidence |
|---|---|---|
| `AT+CGMR` changed to `81600.0000.00.29.23.06` (or whatever version your package produces) | Firmware version updated | High — this is the documented point of the whole procedure |
| `AT+CGSN` (IMEI) **unchanged** from the pre-flash value | `Download Only` doesn't touch IMEI storage | Medium — stated by sources, not independently verified against this unit |
| `AT+EGMR=0,5` (serial) **unchanged** | Same reasoning | Medium, same caveat |
| `AT+ECAL?` still reports calibration present | Same reasoning | Medium, same caveat |
| `AT+GTPKGVER?` / `AT+GTCUSTPACKVER?` / `AT+GTCFGELEMVER?` / `AT+GTCUSTDATAVER?` now show generic/default values instead of the `5025.0000.040...` OEM strings | OEM customisation pack replaced | Medium — the files (`OEM_OTA.img`, `OP_OTA.img`) are exactly the ones that hold this data, so overwriting them should change these version strings; no source states this outcome explicitly |
| `AT+GTDIPCMODE?` returns the generic default (`3,1,1,1,3,15` per our own manual, or is at least no longer locked against writes) | The actual hypothesis under test | **Not documented in sources at all.** This is the single most important unknown this flash is meant to resolve, and nothing in the research confirms it either way |
| `AT+GTFCCEFFSTATUS?` still `0,1` (no FCC lock introduced) | Flashing shouldn't introduce a lock that wasn't there | Low-risk, not explicitly documented either way |
| With a SIM inserted, `AT+CEREG?`/`AT+C5GREG?`/`AT+GTCCINFO?` now show a real cell | The actual goal | Unknown — if DIPC mode wasn't the root cause, this will still fail, and you're back to the hardware-fault hypothesis in bench-log.md |

## Rollback/recovery

**This document could not find a confirmed recovery procedure for a module
left with no working firmware after a failed flash.** Be explicit with
yourself about this before starting: if `Download Only` fails partway
through, or you're forced into a full-format flash to fix a partition
layout mismatch and that also fails, the sources reviewed here do not
describe how to get the module working again.

What sources do document:

- **A crashed (not bricked) module was recovered** in one report (OpenWrt
  forum #114) by using "SP Flash Tool and the firmware in the Windows
  driver package" — i.e. the same tool/technique, applied to a module that
  had crashed (dropped off USB, stuck in a bad state) rather than one with
  no firmware at all. This is weak evidence that SP Flash Tool can recover
  *some* bad states, but it is not evidence that it can recover from every
  failure mode of the procedure above.
- **BROM mode itself should still be reachable** even with corrupted/blank
  firmware, since BROM is a mask ROM stage that runs before the preloader —
  this is general MediaTek SoC design, not something any of the FM350-GL
  sources state explicitly for this module. If SP Flash Tool can still see
  the device in BROM after a failed flash, re-running the same
  `Download Only` procedure from a known-good, re-verified (checksum-checked)
  copy of the firmware folder is the only "next step" implied by the
  sources, not a documented guarantee.
- If the module stops enumerating at all after a failed flash, none of the
  sources offer a next step. Do not assume a JTAG/EDL-style hardware
  recovery mode exists just because other MediaTek platforms have one —
  **not documented in sources** for the FM350-GL specifically.

## Known failure modes from sources

- **"All FW upgrades involve a risk of bricking the device"** — stated
  as-is at the top of the primary walkthrough (mrhaav/digtvbg), with no
  further qualification.
- **Partition-layout mismatch on `Download Only`**: WLGH01 states that an
  error during update most likely means the selected firmware doesn't match
  the module's current partition layout, or the firmware folder was
  assembled incorrectly, and that fixing a layout mismatch requires a
  full-format flash — the exact mode this whole procedure exists to avoid.
- **Wrong preloader variant**: see
  [Hardware/preloader variant matching](#hardwarepreloader-variant-matching)
  above. WLGH01 explicitly did not know whether getting this wrong beyond
  "it won't flash" has any deeper consequence ("我不知道这是否会影响任何东西" —
  "I don't know if this affects anything").
  If no variant matches, that source's own fallback was to flash whichever
  preloader file was filled with `00 00 00 00` — i.e. a wildcard/unset
  value — which is explicitly a "give up and hope" fallback, not a
  documented-safe option.
- **`DEV_OTA.img` uncertainty**: not present in every package build, its
  purpose is not documented in any source, and the WLGH01 guide's default
  advice is to leave that partition (`mcf3`) unchecked if unsure.
  Mismatched OEM_OTA/OP_OTA filenames must be renamed exactly per
  `Scatter.xml`'s expectations; several `-verified` suffixed files exist
  in the package that should **not** be renamed — SP Flash Tool tries the
  `-verified` file first and falls back automatically (WLGH01), so leave
  those names alone.
- **Small periodic freezes after flashing** — OpenWrt forum post #425
  reports "small freezes for some seconds" after flashing a Microsoft
  Update Catalog image, worsening with uptime, resolved by power-cycling.
  Not explained, not reproduced elsewhere in the sources reviewed.
- **Tool version sensitivity**: v5 SP Flash Tool doesn't work at all
  (older download-agent protocol) and tool versions newer than v6.2124
  reportedly use an incompatible protocol — stated by two independent
  sources (mrhaav/digtvbg and WLGH01), treated here as reasonably solid,
  but still worth re-confirming against whatever SP Flash Tool build you
  actually obtain, since exact version boundaries in unofficial mirrors
  are easy to get wrong.

## Open questions on the firmware package

Live-checking the Microsoft Update Catalog during this research
(2026-09-25) was unreliable — searches for `Fibocom FM350`, `3500.5003`,
and `3500.5003.2306` each returned "no results" from the catalog's own
search box in this session, while a broader `Fibocom` search returned
generic driver entries not obviously FM350-specific, and a specific
update-ID link found via web search turned out to be an unrelated, older
(2018) Fibocom firmware package. This is most likely a search-indexing/UI
quirk of the Update Catalog's ASP.NET search (it's known to be picky about
exact substrings) rather than evidence the package is gone, but it means
**this document cannot hand you a verified, live, current catalog link.**
At flash time:

1. Go to https://www.catalog.update.microsoft.com/Search.aspx and search
   `Firmware 3500` (the wildcard search the primary source itself links).
2. Look for `Fibocom Wireless Inc. - Firmware - 3500.5003.2306.x`, and
   prefer the **highest `.x` build number** with the newest "Last Updated"
   date over the specific `.7` this document's primary sources describe —
   a newer point release is plausible (see the `29.23.28` result mentioned
   above) and would supersede `29.23.06` without changing the overall
   procedure.
3. Whatever you download, verify the file layout against your own
   package's `Scatter.xml` and `FwPackageInfo.xml` (`Subsysid="default"`)
   rather than trusting the exact filenames listed in
   [Preparing the firmware folder](#preparing-the-firmware-folder) — those
   are documented as correct for `3500.5003.2306.7` specifically.

## Sources

- mrhaav/openwrt, `atc/fib-fm350_gl/FWupgrade.md` (primary walkthrough;
  fetched verbatim for this document):
  https://github.com/mrhaav/openwrt/blob/master/atc/fib-fm350_gl/FWupgrade.md
- digtvbg.com mirror of the same walkthrough, word-for-word identical
  (both credit the same original source below):
  https://digtvbg.com/files/fibocom-fm350-gl-5g-esim/FLASH-FW.txt
- Original source credited by both of the above, on 4PDA (Russian; fetched
  via a cached/summarized view during this research, not independently
  re-verified against the raw page — treat details attributed to this
  source alone with extra caution):
  https://4pda.to/forum/index.php?showtopic=1057776&st=420#entry128299931
- WLGH01/rockchip_openwrt, `FM350-GL 固件刷写.md` (Chinese-language guide;
  fetched and translated verbatim for this document; source of the
  preloader hardware-ID offset, the three-mode SP Flash Tool comparison,
  and the ADB backup method):
  https://github.com/WLGH01/rockchip_openwrt/blob/main/FM350-GL%20%E5%9B%BA%E4%BB%B6%E5%88%B7%E5%86%99.md
- OpenWrt forum, "Fibocom FM350-GL Support", post #114 (SP Flash Tool used
  to recover a crashed, not bricked, module; firmware from a Windows driver
  package, result `81600.0000.00.29.20.30_GC`):
  https://forum.openwrt.org/t/fibocom-fm350-gl-support/142682/114
- Same thread, post #156 (Dell DW5931e firmware pulled from Dell's driver
  package, USB update mentioned as working but not detailed by the poster):
  https://forum.openwrt.org/t/fibocom-fm350-gl-support/142682/156
- Same thread, post #425 (Microsoft Update Catalog image flashed via SP
  Flash Tool, result reported around `81600.0000.00.29.23.28`; periodic
  freezes noted post-flash):
  https://forum.openwrt.org/t/fibocom-fm350-gl-support/142682/425
- blog.siriling.com FM350-GL post — **could not be fetched** during this
  research (connection reset both attempts); not used as a source for any
  claim in this document:
  https://blog.siriling.com:1212/2023/04/12/5g-modem-fibocom-fm350-gl/
- Microsoft Update Catalog (package search, unreliable in this session —
  see [Open questions](#open-questions-on-the-firmware-package)):
  https://www.catalog.update.microsoft.com/Search.aspx?q=Firmware+3500*
- SP Flash Tool v6.2124 download (unofficial mirror, linked by every
  primary source):
  https://spflashtools.com/windows/sp-flash-tool-v6-2124
- MediaTek USB VCOM / MTK driver package (linked by the WLGH01 guide;
  not independently verified against this module):
  https://mtkdriver.com/mtk-driver-v5-2307
- This repo's own bench findings that motivate this document:
  [bench-log.md](bench-log.md) (OEM firmware version, DIPC lock, no-cell
  symptom), [compatibility-and-risks.md](compatibility-and-risks.md)
  (§6, prior note that this is a last-resort option), and the AT command
  reference in [at-commands.md](at-commands.md).

## Glossary

Terms used on this page, defined in the [shared glossary](glossary.md): [ADB](glossary.md#adb), [BROM / preloader](glossary.md#brom--preloader), [DIPC mode](glossary.md#dipc-mode), [FCC lock](glossary.md#fcc-lock), [IMEI / IMSI / ICCID](glossary.md#imei--imsi--iccid), [M.2 B-key](glossary.md#m2-b-key), [NV partitions / calibration](glossary.md#nv-partitions--calibration), [OEM image](glossary.md#oem-image), [Pigtail](glossary.md#pigtail), [SP Flash Tool](glossary.md#sp-flash-tool).
