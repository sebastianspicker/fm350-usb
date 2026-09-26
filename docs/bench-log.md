# Bench log

Dated lab notebook for bench-testing the Fibocom FM350-GL / Dell DW5931e over
USB on a Mac. Entries are in the order we ran them, warts and dead ends
included. For the distilled, reader-facing version of what we found, see
[dell-dw5931e-usb.md](dell-dw5931e-usb.md).

This log is the project's evidence record: every measurement, AT response
and conclusion below is kept, including the ones that turned out to be
wrong or incomplete. It is written for the technically curious and for
anyone who wants to check a claim made elsewhere in the docs against the raw
data it came from.

## In short

**Outcome, so you don't have to read the whole log:** the module spent most
of a day registering no cell on any RAT, with everything in software ruled
out (FCC lock, DIPC mode, W_DISABLE#, SAR/antenna tuner, bands, APN,
thermal). The cause was a pair of defective antenna pigtails. After we
swapped them, the module registered within 30 s and saw 10 cells.

In plain terms: this was a long process of elimination. Almost a full day
went into showing, one suspect at a time, that the SIM, the firmware
settings, the FCC lock, the antenna tuner and the rest of the software
configuration were fine. The fault was outside all of that: a pair of
defective antenna cables (pigtails) between the module and the case. If
your module reports "no cells" like this, check the pigtails early; on our
unit, that was the answer.

### Timeline of the day

| Entry | What we learned | Where it's used elsewhere |
|---|---|---|
| Dongle on MacBook, no SIM | USB enumeration, firmware version and basic module state are all fine; no FCC lock; macOS has no RNDIS driver | README status table; Hardware (USB layout); Compatibility (FCC lock) |
| No-SIM RNDIS experiments | Measured USB/codec throughput ceilings; the modem's TX queue only accepts 3 writes without a bearer; polling the control channel without a notification crashes the modem firmware | macOS design (Async USB I/O); fm350mac README (`--io async/sync`) |
| `up --loopback` live (fake modem) | The utun/route/ping path works end to end against an in-process fake modem; a busy-spin bug caused high latency and was fixed | macOS design (GIL note); fm350mac README (`--loopback`) |
| Privilege separation live | The root-helper split works, including automatic cleanup when the unprivileged process is killed with `SIGKILL` | macOS design (Privilege separation); fm350mac README (Setup, `kill -9` section) |
| First test with a SIM | SIM detected and radio on, but no cell measured on any RAT; DIPC mode found locked in an OEM "PCIe Advance Mode"; FCC lock ruled out | Dell guide; Compatibility (FCC lock, DIPC) |
| Deep dive, Dell DW5931e | Took a full ADB root-shell backup; changed and then ruled out DIPC mode as the cause; ruled out the Dell FCC challenge/response; corrected an earlier reading of `AT+ERAT` persistence | Dell guide (DIPC background, ADB backup); Diagnostics (backup, DIPC edit); AT commands (`ERAT`) |
| Outdoor test (open sky) | Even outdoors, with a clear view of the sky, no cell and no GNSS fix; the GNSS receiver's AGC barely changes with location | Dell guide (troubleshooting, "no cells" ruled-out table) |
| Cable swap — cause found | Replacing the antenna pigtails fixed everything: registered within 30 s, 9 neighbour cells seen | README (headline lesson); Dell guide (pigtail troubleshooting); Hardware ("buy decent pigtails") |

## How to read this log

Entries are in the order we actually ran them, on 2026-09-25, not
reorganised by topic. That matters here: several early conclusions in this
log were provisional, and later entries either confirm, refine or correct
them. Two are worth knowing before you read on:

- Early on, `AT+ERAT=3` looked like it did **not** survive a reset. Later in
  the same day, under "Deep dive part 2", the log recorded the opposite:
  `AT+ERAT` **does** persist across `AT+CFUN=15`, contrary to the manual.
  Both readings are kept below, in place, because this is a notebook; where
  other documents state this behaviour, they use the corrected reading,
  "persists".
- Through most of the day, the working hypothesis was a defective **RF
  receive path in the module or the adapter**. The final entry, "Cable
  swap", narrows that down: the module and adapter were fine, and the fault
  was specifically the antenna pigtails. Earlier conclusions that point at
  "the module" or "the adapter" are marked below as later refined.

The final cause is at the end of the log, in "Cable swap".

## 2026-09-25: dongle on MacBook (macOS), no SIM

*What this entry showed: the hardware enumerates correctly over USB and the*
*module reports no FCC lock — the basic USB/AT plumbing works, well before*
*the "no cells" problem shows up.*

Setup: FM350-GL in the Waveshare USB TO M.2 B KEY, connected through a GenesysLogic USB 3.2 hub. No auxiliary power plug. **No SIM inserted.**

AT access: [`tools/fm350_at.py`](../tools/fm350_at.py) talks to the AT interface over raw USB through libusb, because macOS has no driver for the FM350 serial ports:

```sh
brew install libusb   # once
uv run --with pyusb tools/fm350_at.py 'ATI' 'AT+CPIN?'
```

### USB enumeration: OK

*What this entry showed: USB 3 SuperSpeed and the expected interface layout*
*both work, with no auxiliary power plug attached.*

| Item | Result |
|---|---|
| USB ID | `0e8d:7127` ("Fibocom Wireless Inc." / "FM350-GL"), mode 41 |
| Link | **SuperSpeed 5 Gbps** (the adapter's USB 3 lines work) |
| Boot time | Appears about 10–60 s after power-on (not listed on the first check) |
| Power | Reported sink allocation 896 mA (USB 3 default); no brown-out without the auxiliary plug while idle |
| Interfaces | 0 RNDIS Communication, 1 RNDIS Data, 2–4 COM, 5 ADB, 6–9 COM (matches the documented mode-41 layout) |
| AT port | Interface 6, bulk OUT 0x06 / IN 0x87 |
| macOS network | No interface appears: macOS ships only ECM/NCM drivers and the FM350 only offers RNDIS modes (`AT+GTUSBMODE=?` returns `(40,41)`) |

### Module state

*What this entry showed: firmware version, FCC status and radio-on state,*
*all read without a SIM inserted.*

| Command | Response | Meaning |
|---|---|---|
| `ATI` / `AT+CGMR` | `81600.0000.00.29.20.22`, SVN 05 | Firmware 29.20.22 (older than the 29.23.06 in the upgrade notes; no upgrade planned unless needed) |
| `AT+GTUSBMODE?` | `41` | Default composition |
| `AT+GTFCCEFFSTATUS?` | `0,1` | **No FCC lock**: no unlock script needed |
| `AT+CFUN?` | `1` | Radio on |
| `AT+GTFMODE?` | `1,0` | Hardware W_DISABLE#/GNSS pin control setting (radio is on regardless) |
| `AT+CPIN?` | `+CME ERROR: SIM not inserted` | Expected, no SIM |
| `AT+GTDUALSIM?` | `0,"SUB1","NO SERVICE"` | SIM1 slot selected (the slot the Waveshare board wires) |
| `AT+MSMPD?` | `1` | SIM hot-plug detection on (default). Left unchanged |
| `AT+GTACT?` | `20,6,3,…` all bands | All LTE (B1–B71) and NR (n1–n79) bands enabled |
| `AT+CGDCONT?` | (empty) | No PDP contexts defined yet |
| `AT+COPS=?` | `operation not allowed` | The modem won't scan without a SIM |
| `AT+GTCCINFO?` / `AT+CESQ` | empty / all 99/255 | No cell info without a SIM |
| `AT+GTSENRDTEMP=0` | 25–27.6 °C (values in m°C) | Cool at idle |

### Conclusions

- **The hardware combination works at the USB level.** The adapter passes USB 3 SuperSpeed, and the module enumerates with the expected layout.
- **The FCC lock risk is gone.** This module is not locked.
- Still unverified: SIM detection in the Waveshare slot, network registration, antenna quality, the data path, and power draw under load.

### Next test (needs a SIM)

1. Power off, insert the nano-SIM, reconnect, and wait about 60 s.
2. `AT+CPIN?` should return `READY`. If it still says "SIM not inserted", the adapter's SIM-detect line may not match what the module expects. Try `AT+MSMPD=0` (persistent; turns off hot-plug detection), then `AT+CFUN=15`.
3. `AT+COPS?`, `AT+CEREG?`, `AT+C5GREG?`, `AT+GTCCINFO?`: note the band and RSRP/SINR.
4. `AT+CGDCONT=1,"IP","<apn>"`, `AT+CGACT=1,1`, `AT+CGPADDR=1`, `AT+GTDNS=1`: if you get an IP, the go/no-go test has passed. On macOS, data needs a third-party user-space RNDIS driver (see below). The real throughput test happens on the router.

### Internet on macOS: possible with a user-space RNDIS driver (checked 2026-09-25)

*What this entry showed: no built-in macOS driver can talk to this modem's*
*RNDIS interface, but at least one third-party user-space option looked*
*promising on paper (not yet tested against this hardware).*

- macOS has no built-in RNDIS driver. Loaded drivers: `com.apple.driver.usb.cdc.ecm`, `com.apple.driver.usb.cdc.ncm`, `AppleUserECM.dext`. The FM350 can't switch to ECM or NCM (`AT+GTUSBMODE=?` returns `(40,41)`).
- **[TetherKit](https://github.com/XiaoMiku01/TetherKit)** (MIT; `brew install XiaoMiku01/tap/tetherkit-cli`) does RNDIS over libusb and creates a `feth` interface. It needs no kext and no SIP changes, and runs on Apple Silicon. The FM350's interface 0 descriptors are `02/02/ff` with ACM `bmCapabilities=0`, which matches TetherKit's `kControlSignatureMicrosoft` and passes its "real ACM modem" check, so it **should** be detected. Not tested yet. Avoid the 0-star forks (`XenOriginal/…`, `lefos13/…`).
- Alternative: [ReRNDIS](https://github.com/JellyBrick/ReRNDIS) (GPL-3, macOS 15+). HoRNDIS (kext) is effectively dead on Apple Silicon.
- Expected quirks: set a static IP from `AT+CGPADDR` (DHCP is unreliable on the FM350), and possibly add a static ARP entry for the gateway (the Linux scripts turn ARP off).
- Test plan: `tetherkit-cli --list` (no root) → `sudo tetherkit-cli` → open the data session with `tools/fm350_at.py` → `sudo ipconfig set feth0 MANUAL <ip> 255.255.255.0` → route/ping.

(This entry is about TetherKit as a possible alternative internet path; the
project's own driver, described in [macos-driver.md](macos-driver.md), takes
a different approach — libusb + `utun` — and is what was actually built and
tested in the later entries below.)

## 2026-09-25: no-SIM experiments on the RNDIS data path (Mac)

*What this entry showed: rough performance ceilings for the software stack,*
*and one firmware landmine (never poll the control channel blind) — all*
*measured before a SIM was available.*

Scripts: throwaway probes built on the `fm350mac` package (layer-2 probe, TX queue test, codec and USB benchmarks).

| Test | Result | Consequence |
|---|---|---|
| Send DHCPDISCOVER, IPv6 RS and ARP to the modem via RNDIS bulk OUT, listen 12 s | **No reply at all** (0 frames received) | Without a data session the modem doesn't act as a DHCP/ND/ARP peer. Its DHCP behaviour stays unknown until a SIM is in |
| Count bulk OUT writes accepted (after a USB reset) | **Exactly 3 accepted, then NAK/timeout on every write**. Reproduced; the queue survives RNDIS HALT/re-init and only clears on a USB reset | Uplink is only drained with an active bearer. The bridge must treat TX timeouts as "stalled" (drop, rate-limited log), not as fatal errors |
| Codec speed (pack+wrap / unpack+strip, pure Python, one core) | TX ~3.0 M pkt/s, RX ~1.7 M pkt/s | Python framing is not the bottleneck |
| USB control transfer round trip (pyusb/libusb, synchronous) | **106 µs** → ~9.4 k transfers/s per thread | Rough ceiling with one synchronous transfer per packet: ~9 k pkt/s per direction ≈ 100 Mbps at 1400 B. For more, use async libusb transfers with several in flight |
| AT command round trip (`tools/fm350_at.py` / `AtPort`) | 327 ms | Caused by our reader waiting out a 300 ms read timeout after `OK`. Fix: return on the terminator |
| 2000 back-to-back empty GET_ENCAPSULATED_RESPONSE, then QUERYs | **Modem firmware crashed**: control timeouts, dropped off USB, re-enumerated after ~60–90 s. All settings intact (CFUN 1, FCC 0,1, USBMODE 41, MSMPD 1) | Never poll the control channel without a notification. `up --supervise` must survive re-enumeration and rebuild the session |

## 2026-09-25: `fm350mac up --loopback` live on macOS (sudo, fake modem in-process)

*What this entry showed: the utun/route/ping path works end to end against*
*an in-process fake modem, and a busy-spin bug that inflated latency was*
*found and fixed.*

Run from a frozen code snapshot (the async rewrite was in progress).

| Check | Result |
|---|---|
| utun creation + address | `utun8`, `inet 192.0.2.2 --> 192.0.2.2`, MTU 1500 |
| Routes | only the host route `198.51.100.1 → utun8`; default route untouched |
| `ping 198.51.100.1` (56 B ×5, 1400 B ×3, 56 B ×50 @100 ms) | **0 % loss**, 58/58 replies |
| Teardown after Ctrl-C | host route removed, `utun8` gone, no 192.0.2.2 left |
| Latency | 6–25 ms RTT: worse than expected. Root cause: `LoopbackRndis.wait_notify()` returned immediately, so the control thread busy-spun and held the GIL, and each rx/tx handover waited out the 5 ms switch interval. Reproduced without root (Bridge + socketpair): median 18.3 ms; with switchinterval 0.5 ms: 1.9 ms; with a blocking `wait_notify`: **0.026 ms**. Fixed, with an anti-spin guard and a regression test added as part of the async rewrite. Real-modem path unaffected (libusb waits block and release the GIL) |

## 2026-09-25: privilege separation live (helper installed, `up --loopback` without sudo)

*What this entry showed: the root-helper split (main process vs.*
*`fm350mac-helper`) works as designed, including cleaning up fully after the*
*unprivileged process is killed outright.*

First live test of the root-helper split described in
[macos-driver.md](macos-driver.md#privilege-separation-decided-2026-09-25):
install the LaunchDaemon once, then run `up` as a normal user.

| Check | Result |
|---|---|
| `sudo .venv/bin/fm350mac helper install` | OK: helper root:wheel 0755, plist root:wheel 0644, `launchctl bootstrap` OK |
| `fm350mac helper status` | socket `/var/run/fm350mac-helper.sock` owner 502 mode 0600 (`SockPathOwner` works on macOS 27); hello OK |
| Process split | helper runs as root on `/Library/Developer/CommandLineTools/.../Python -I -S`; `fm350mac` runs as the calling user (Homebrew Python/.venv) |
| `up --loopback` without sudo | helper created `utun8` 192.0.2.2/MTU 1500 + host route 198.51.100.1; fd passed to the unprivileged process |
| Ping | 20/20 @100 ms + 3/3 @1400 B, 0 % loss; avg 1.2 ms (min 0.33 ms), previously 13–18 ms before the busy-spin fix |
| SIGKILL of the unprivileged process | helper auto-teardown worked: host route gone, utun8 gone, no 192.0.2.2 left; the real default route (192.168.8.1 via en0) untouched |
| Helper lifecycle | stays idle after the session (launchd on-demand start; it doesn't exit by itself). Holds no network changes |

Result: the split works as designed, including the SIGKILL case that
motivated it.

## 2026-09-25: first test with a SIM (no data; physical SIM is a voice SIM, data SIM is an eSIM elsewhere)

*What this entry showed: the SIM is detected and the radio is on, but the*
*module measures no cell at all on any radio access technology — the start*
*of the day's central problem.*

Setup: Mac, top-floor apartment next to a window, all 4 antennas connected. Tested on a GenesysLogic USB 3.2 hub (896 mA allocated) and on a powered Dell dock. No PDP context was opened, so no data was used.

| Check | Result | Meaning |
|---|---|---|
| `AT+CPIN?` / `AT+CLCK="SC",2` | `READY` / `0` | **SIM detected in the Waveshare slot**, no PIN |
| `AT+CIMI` / `AT+ICCID` / `AT+CPOL?` | 26202… / 894920… / Vodafone partner list | Vodafone DE SIM, file system readable |
| `AT+SIMTYPE?` / `AT+GTDUALSIM?` | `0` / `0,"SUB1","NO SERVICE"` | Physical SIM, slot 1 (not eSIM mode) |
| `AT+CFUN?` / `AT+EFUN?` / `AT+ESIMS?` | `1` / `1` / `1` | Radio on at both the 3GPP and MediaTek layers |
| `AT+GTACT?` / `AT+EPBSE?` / `AT+ERAT?` | `20,6,3,<all bands>` / all bands set / `255,0,21,0,0` | Auto 3G/LTE/NR, no band or cell lock; current RAT 255 = none |
| `AT+CEREG?` over 2–4 min | cycles between `2` (searching, ~10–20 s) and `0` (not searching) | Searches and finds nothing |
| `AT+CESQ` / `AT+ECSQ` / `AT+GTCCINFO?` | all 99/255 / all zeros / empty | **No cell is measured on any RAT** |
| `AT+COPS=?` | returns at once with an empty list | A real scan would take >30 s |
| `AT+CFUN=4` → `AT+CFUN=1` | no change | |
| `AT+GTFMODE=0,0` (ignore W_DISABLE#) + `AT+CFUN=15` | no change → **reverted to `1,0`** | W_DISABLE# (pin 8) is not the cause |
| Powered Dell dock instead of a bus-powered hub | no change | Probably not undervoltage (still a USB-A port, not externally measured) |
| `AT+EGMR=0,5` | (redacted) | Module serial number (for a possible return or RMA) |

### Conclusion

The software side and the SIM are fine. The RF front end receives nothing, which is not plausible at a top-floor window with 4 antennas. Remaining suspects: (1) a pin conflict between the FM350 and this board (the FM350 is not on Waveshare's verified list; pins 56/58/59/61 are antenna-tuner lines), (2) a defective or damaged RF path in the module or the pigtails, (3) not enough peak current. Next step: test the module in another host (a laptop M.2 WWAN slot or a different adapter) to tell the module and the board apart, or test the adapter with an RM520N-GL.

*(Suspect (2) named both "the module" and "the pigtails" as possible*
*locations for the RF fault; see Cable swap below — it was the pigtails.)*

### Follow-up: search for a software fix (2026-09-25)

*What this entry showed: OEM firmware settings (DIPC mode, FMODE) are*
*locked and configured for a laptop PCIe host, but ruling them out on paper*
*did not by itself find a fix.*

Sources: *FM350 AT Commands User Manual V2.10* and the *FM350-GL Hardware Guide V1.0.5* (FM350-GL-00). All values read; only the writes listed here were tried, and everything was restored.

| Check | Result | Meaning |
|---|---|---|
| `AT+GTPKGVER?` | `81600.0000.00.29.20.22_5025.0000.040.000.038_C69` | Customized image `5025.0000.040`, device data `5006.000C.0000_Default`: **OEM/laptop firmware** |
| `AT+GTCURCAR?` / `AT+GTLOCKCAR?` | `202,"Vodafone"` / `0,65535` | Carrier config loaded, no carrier lock |
| `AT+GTDIPCMODE?` | **`1,2,2,2,7,13`** (default `3,1,1,1,3,15`) | "PCIe Advance Mode", AT and logs routed to PCIe: configured for a laptop PCIe host |
| `AT+GTDIPCMODE=3,1,1,1,3,15` / `=3` / `=1,1,1,1,7,13` | `+CME ERROR: phone failure` every time | **Locked**: can't be changed by AT |
| `AT+GTFMODE?` | `1,0` (manual default `0,0`) | Changed by the OEM too; `0,0` + reset didn't help (tested earlier) |
| `AT+GTRXPATHEN?` | `3,15` | All 4 RX paths on |
| `AT+GTANTTUNINGEN/TUNEMODE/CTRLMODE/PROFILE?` | `1` / `0` / `0` / `0` | Tuner at default (GPO, HW control) |
| `AT+BODYSAREN?` / `AT+GTTASEN?` | `1` / `0` | Only affect TX power |
| `AT+GTFCCLOCKMODE?` / `STATE?` / `EFFSTATUS?` | `0` / `0` / `0,1` | No FCC lock |
| `AT+ECAL?` | `1` | RF calibration present |
| `AT+ETESTSIM?` / `AT+ECELL` / `AT+EMODCFG?` | `0` / `0` / `0,255` | Not a test SIM, no cell known |
| `AT+COPS=1,2,"26202",7` / `,12` / `AT+COPS=0` | `+CME ERROR: unknown` within ~3 s | **Network selection refuses every command**, it doesn't scan |
| `AT+CEREG=3` over 80 s | stat 0/2, cause type 0, cause **114** | 114 isn't a 3GPP EMM reject cause (MediaTek-internal); `AT+CEER: 0,NONE` → **no network rejection** |

Interpretation: the SIM, calibration, RF paths and antenna tuner are OK. The protocol stack never becomes ready for network selection, which fits an OEM firmware that runs in PCIe Advance Mode and expects a PCIe/MBIM host. That config can't be changed by AT. Remaining software path: flash generic Fibocom firmware (e.g. 29.23.x), which normally resets DIPC mode and the OEM config. The risk: it's a flash on an OEM module and needs the Fibocom flash tool (Windows).

*(This DIPC/firmware-flash hypothesis is examined and the DIPC part directly*
*tested — and ruled out — further down, in "Deep dive part 3". By the end of*
*the day a flash turned out not to be needed at all: see Cable swap.)*

### Follow-up: community sources checked (2026-09-25)

*What this entry showed: nobody else in the community had reported this*
*exact symptom, and one similar-sounding OEM lock (HP) turned out to work*
*fine over USB — weakening the DIPC hypothesis rather than confirming it.*

Read: the whole OpenWrt thread "Fibocom FM350-GL Support" (454 posts, #1–#467), mrhaav/openwrt `atc/fib-fm350_gl` (incl. `atc.sh` 2025.08.24, `FWupgrade.md`), koshev-msk/modemfeed (`xmm-modem`, the deleted `fm350-modem` from git history, `modeminfo` INTEL_FM350), and the kernel patch `7d5a7dd5a358` "net: wwan: t7xx: Split 64bit accesses" (PCIe driver only, not relevant for USB).

| Finding | Source | Relevance for us |
|---|---|---|
| **Nobody reports our symptom** (SIM READY, but no cell measured at all) | whole thread | No known fix |
| HP FM350 with `GTDIPCMODE: 1,2,2,2,5,13`, can't be changed ("errors"), "no solution with AT commands" | #346–#351 | Same OEM lock as ours. It worked over USB there (Telekom), so **PCIe Advance Mode alone does not block the radio**. That weakens the DIPC hypothesis |
| FM350 has two AT-capable interfaces (3 and 6); only 6 sends URCs; `COPS=0` without OK on the wrong port | #382–#393 | We use interface 6 (correct). Interface 3 gives no answer here within 40 s |
| mrhaav's `atc.sh` sets `AT+EIAAPN` + `AT+CGDCONT` before `CFUN=1` | atc.sh | **Tested:** `CGDCONT=1,"IPV4V6","web.vodafone.de"` + `EIAAPN=...` + `CFUN=4/1` → still no cell; CEREG now also stat 3 (cause 114, TAC FFFF) with no cell in view. Context/APN stay set (harmless) |
| Generic firmware 29.23.06 from the **Microsoft Update Catalog** ("Fibocom Wireless Inc. – Firmware – 3500.5003.2306.7"), flashed **over USB** with SP Flash Tool v6.2124 "Download Only"; also replaces `OEM_OTA`/`OP_OTA` | mrhaav FWupgrade.md, #114, #156, #425 | The only software path left. A crashed flash was recovered with SP Flash Tool (#114) |
| `AT+GTACT` isn't persistent (falls back to "all bands" after a reset) | #237–#240, #311 | Don't rely on band locks surviving a reboot |
| `AT+E5GOPT=5` (NSA only) as a workaround when SA gives no IPv4 | #172 | Our value: 7 (NSA+SA) |
| `AT+CGCONTRDP` crashes the modem, per one user report | #429 | `atc.sh` sends `AT+CGCONTRDP=1` after every connect; watch for this on the router |
| Laptop OEM FCC-unlock hashes: Lenovo `3df8c719`, Dell DW5931e `4909b5a4` | #217, #328 | Not needed (our unit isn't locked) |

## 2026-09-25: deep dive, Dell DW5931e (seller confirmed: Dell OEM, Latitude 7440/5531/9330/3571)

*What this entry showed: a full ADB root-shell backup was taken before any*
*further experiment, and it's the umbrella for every finding below down to*
*"Cable swap" — DIPC mode was changed and ruled out, the Dell FCC*
*challenge/response was ruled out, and an earlier `ERAT` reading was*
*corrected.*

### Backup (read-only via ADB, before any further experiment)

*What this entry showed: the module exposes a root ADB shell, and a full*
*raw backup of all 40 partitions was taken before changing anything.*

- The FM350 exposes **ADB** (USB interface 5) → root shell on the internal application processor: OpenWrt 19.07-SNAPSHOT, kernel 4.19.205, target `mt6880/k6880v1_mdot2_datacard`.
- `backups/fm350-<serial>-<timestamp>/`: tar archives of `nvram nvdata nvcfg protect_f protect_s mdota mdota2 mdota3` plus **raw images of all 40 MTD partitions** (`raw/`, 533 MB, `SHA256SUMS`). Every partition has its full size except `mtd4 expdb` (crash-log partition, 2 MB short, probably a bad block; not needed for a restore). The backup belongs to this unit (IMEI/calibration) and must not be shared.

### Findings from the modem's internal system

*What this entry showed: the internal Linux system confirms the radio is*
*genuinely on and the OEM lock is the PCIe Advance Mode DIPC setting, not an*
*FCC/W_DISABLE-style hardware lock.*

| Finding | Source | Meaning |
|---|---|---|
| `dipcd`: `dual_ipc_mode 1`, `md_at_interface 2`, reports this to a (missing) PCIe host | logread | Locked PCIe Advance Mode confirmed |
| `fibocom_modem_at_app`: `MIPC_NW_RADIO_STATE hw=1, sw=1` | logread | **The radio is on (hardware and software).** Not an FCC/W_DISABLE lock in the ModemManager sense ("hardware radio switch OFF") |
| `NW_REGISTER_STATE ps_state` alternates 2 (searching) ↔ 0 (detached), ~50× | logread | The modem core searches and finds nothing |
| `[FIBO IMEI CHECK]: OK` | logread | IMEI valid |
| MCF: OEM image `OEM_OTA_5025.0000.040` (built for 29.20.15, 2022-07-15), `BOARD_ID=0002`, DEV images type 1 (BodySAR), **type 2 (tunable antenna, 1.3 MB)**, X (WWAN config); merge file `m80_merge.mcfota`; check result 0 | `/mnt/vendor/mdota*`, `/data/mcf_cmd_log` | 5025 = Dell customer image. Images are encrypted/compressed |
| `ccci_mdinit: sbp=0`, `md_drdi_rf_set_idx 0x6` | dmesg | SBP 0; RF set index 6 (DRDI) comes from the device tree |
| `nvram_daemon: Bin Region Restore to NvRam Fail (map file size error)` | logread | Probably a first-boot artefact; watch it |
| Thermal: all 8 actuators at level 0, `GTTHMLTIMES 0` | AT | Thermal isn't throttling |
| SIM files: FPLMN = 262-07/03/01 (not Vodafone), LOCI = 262-02 "updated", HPLMNwAcT 26202 incl. E-UTRAN; IMEI Luhn OK | AT+CRSM | SIM side clean |

### Further tests (no effect)

*What this entry showed: three more possible causes tried and found to have*
*no effect — though the `ERAT` persistence reading here was later corrected*
*(see Deep dive part 2, C10).*

| Test | Persistent? | Current state |
|---|---|---|
| `AT+ERAT=3` (LTE only) | no | reset back to default by `CFUN=15` (not re-read) |
| `AT+GTANTTUNINGEN=0` + `CFUN=15` | yes | Stayed at 0 across the reset; restored to `AT+GTANTTUNINGEN=1` shortly after (see Deep dive part 2 below) |
| USB interfaces 2, 3, 4 as AT/GNSS port | – | no answer within 15–40 s |
| GNSS commands on the modem AT port | – | `CME ERROR: unknown` (they belong on the GNSS port) |

**Correction (C10):** the "no" for `AT+ERAT=3` above was based on a value
that wasn't re-read after the reset. Deep dive part 2, further down, found
the opposite when it actually re-read the value: `AT+ERAT` **does** persist
across `AT+CFUN=15`, contrary to the manual. Both readings are kept as
originally logged; other documents use the corrected reading, "persists".

After the modem reset, ADB reports the device as `offline`; AT still works.

### Dell FCC challenge/response

*What this entry showed: running Dell's own FCC unlock challenge/response*
*made no difference, which rules the FCC lock out as thoroughly as possible.*

- The procedure: `AT+GTFCCLOCKGEN` → response = first 4 bytes of `SHA-256(challenge ‖ SHA-256("DW5931EFCCLOCK")[0:4])` (prefix `4909b5a4`) → `AT+GTFCCLOCKVER=<decimal>`, without `AT+GTFCCLOCKMODE`. We rated this a low-probability fix, since `GTFCCEFFSTATUS` already read `0,1` and the radio state was `hw=1/sw=1` ("unlocked/on").
- We ran it: `AT+GTFCCLOCKGEN` → `0x00000000`, response `2065811812` → `+GTFCCLOCKVER: 1`, `GTFCCEFFSTATUS` stayed `0,1`. Still no cell afterwards (60 s). **The FCC lock is ruled out.**
- Dell research: the DW5931e works over USB-only adapters on Dell's own firmware (forum #156, #380 with 29.23.08). A Dell community thread reports package 6.0.3.76 broke cellular connectivity while 6.0.3.66 worked. Next candidate: Dell's current firmware package (driver ID `f34xk`) — see [firmware-reflash.md](firmware-reflash.md).

*(As Cable swap below shows, this reflash candidate was never needed — the*
*cause was the pigtails, not the firmware.)*

### Deep dive part 2 (ADB, read-only unless noted)

*What this entry showed: the `ERAT` persistence correction (C10, above),*
*plus confirmation that a modem reset needs a USB port reset to bring ADB*
*back, and an inconclusive indoor GNSS check.*

- Restored: `GTANTTUNINGEN=1`, `ERAT=21` (**ERAT persists across `CFUN=15`**, contrary to the manual).
- ADB after a modem reset showed `offline`; **a USB port reset (libusb `reset_device`) brings it back**.
- GNSS as an RF check: `fm_gnss` (factory test, 10 s) → `sv search FAIL(sv num:0)`; `mnld_test start c` (> 3 min indoors) → no fix. Inconclusive (short test / indoors); NMEA can't be read out on USB interfaces 2/3/4/7/8/9 without opening a port.
- AP identity: `/etc/vendor_info` → `AP_VERSION=FM350.C69`, `VERNO=gem-mp-1907-mp1.V1.26_FIBOCOM_1907MP1_T700_P6`, build 2022-06-30. `/etc` holds OEM switch tables `FwSwitchTable_5000/5001/5003/5006_01.xml`.
- USB composition: the AP gadget provides `acm.gs0–2` + `ffs.adb`; the remaining functions (RNDIS, AT, …) come from the modem core ("MD USB enumeration").
- **The DIPC mode is a plain file:** `/mnt/vendor/nvdata/md_cmn/dipc_config` (125 bytes: `dual_ipc_mode:1`, `ap_logging_interface:2`, `md_logging_interface:2`, `md_at_interface:2`, `ap_pcie_port_config:7`, `md_pcie_port_config:13`) + `dipc_config_verno`.
  - `/etc/init.d/usb.init` reads only `dual_ipc_mode`: **1 or 3 → start USB** (with no PCIe link); any other value → "DIPC mode with NO USB" (dangerous: no USB access).
  - `dipcd` checks the file (`check_dipc_config`, `fibo_check_dipc_config_verno`); if it's invalid, it logs "Reconstruct default dipc config".
  - At this point we decided not to change the file to `3,1,1,1,3,15` (the manual default) yet, given the brick risk described in [firmware-reflash.md](firmware-reflash.md) — see Deep dive part 3, where we did make the change.

### Deep dive part 3 (reversible configuration changes)

*What this entry showed: DIPC mode was actually changed to the manual*
*default and back — and made no difference, ruling it out directly rather*
*than by inference.*

| Step | Result |
|---|---|
| DIPC file changed: `/mnt/vendor/nvdata/md_cmn/dipc_config` `1,2,2,2,7,13` → `3,1,1,1,3,15`; original kept as `dipc_config.orig-dell` on the modem and in the backup | After `CFUN=15`: USB/AT/SIM OK, `AT+GTDIPCMODE?` → `3,1,1,1,3,15`, ADB OK. Still no cell (120 s), so **the DIPC mode is ruled out as the cause.** The setting stays at factory Dual Mode (sensible for USB); revert with `cp dipc_config.orig-dell dipc_config` + reboot |
| Port mapping via `lsof` | `gnss_adpd` → ttyGS1, `logread_agent` → ttyGS0, `meta_tst` → ttyGS2 (AP gadget: gs0, gs1, gs2, adb) |
| Board pins (debugfs gpio) | `rf_ver` hi/lo, `pcb_ver` lo/lo, `sar_config` hi, `bodysar_dpr_1/2` hi, `wwan_wake` hi: module-internal straps, nothing conspicuous |
| Kernel log after reboot | `md_rf_driver_init` OK, no modem exception (EE/assert) |
| GNSS cache `/etc/gnss/mtkgps.dat` after several minutes of GNSS at the window | Practically empty (168 non-trivial bytes, no ephemeris/position), meaning **GNSS receives nothing either** (ANT3). Not proof by itself (indoors, cold start without assistance) |
| Regulators (debugfs) | RF supplies are driven by the modem core, so the AP shows them as "unused" (`vsim1` does too, even though the SIM works); no conclusion possible |

**Current conclusion:** everything that can be configured in software is ruled out (FCC, W_DISABLE, DIPC, thermal, SAR/tuner, RAT/bands, APN, SIM, IMEI). The modem core searches with the radio on and hears nothing, not even via GNSS. **A defect in the RF receive path is now the most likely explanation** (module or adapter/pin conflict). Next discriminating test: GNSS under open sky (outdoors/balcony, 10 min). If GNSS gets satellites there, the RF chain works and the problem is in the cellular configuration (then Dell reflash). If it gets nothing, test the module in another host (laptop M.2 slot/other adapter) or return it.

*(later refined: see Cable swap — the RF defect was real, but it was in the*
*pigtails, not the module or the adapter board)*

### Deep dive part 4 (safe steps)

*What this entry showed: offline NVRAM analysis confirms calibration is*
*intact, and a longer, more careful GNSS test at the window still shows no*
*acquisition at all.*

| Step | Result |
|---|---|
| NVRAM analysis (offline, from the backup) | `CALIBRAT`: 135 LIDs (53× UL/UMTS, 30× LA/LTE, 10× NL/NR, …), 132 of them unchanged since the factory (2021-02-15); 3 rewritten on the OTA (`HL15_001`, `ML0D_000`, `TATP_009`). `SWCHANGE.TXT`: "OTA from 29.18.01 to 29.20.22 (2022/07/26)". **Calibration intact.** "Bin Region Restore Fail" = normal path when nvdata is intact |
| Passive listen in Dual Mode | Interface 3 = **GNSS port** (sends `+ELCSGNSSRST: "GNSS adaptor reboot done"`); interfaces 2/4: nothing; 7–9: `LIBUSB_ERROR_ACCESS` |
| `AT+GTGPSPOWER=1` on interface 3 | The daemon receives the command (log: `dump_buff … AT+GTGPSPOWER=1`) but doesn't answer (`gnssadp_epoll_at_client_hdlr() read() failed`) |
| GNSS debug log (`/etc/gnss/mnl.prop`: `debug.debug_nmea=1`, `debug.dbg2file=1`, removed again afterwards) + `mnld_test start c`, 150 s at the window | **0 GSV sentences, `NACQ,0`** (no satellite acquired), GGA fix 0 / 0 satellites. `$PMTKAGC` 4 channels ≈ 3300/3300/6400/6500 (no reference value from a working FM350). Log kept: `backups/…/gnss-debug-window-cold-150s.nma` |

Interpretation: cellular and GNSS both receive nothing, and the software/config side is ruled out, which is strong evidence for a defect in the RF receive path (module or adapter). The deciding test is outdoors (open sky): if GNSS/cellular get something there, it's the location (e.g. coated window glass); if not, it's hardware.

*(later refined: see Cable swap)*

### Outdoor test (open sky, 2026-09-25 19:04–19:08)

*What this entry showed: even outdoors, with an unobstructed view of the*
*sky, there is still no cell and no GNSS fix, and the GNSS AGC barely moves*
*— ruling out "the window glass is blocking the signal" as the explanation.*

| Measurement | Indoors (window) | Outdoors |
|---|---|---|
| Cellular (27 samples every 10 s) | no cell | **no cell** (`CESQ` all 99/255, `GTCCINFO` empty, CEREG 0/2 cause 114) |
| GNSS acquisition (`NACQ`) | 0 | **0** (0 GSV sentences, no fix) |
| GNSS AGC (`$PMTKAGC`, 4 channels) | ≈3300 / 3300 / 6400 / 6500 | ≈3150–3480 / 6050–6170 (practically unchanged) |

Logs: `backups/…/cell-outdoor.log`, `gnss-debug-outdoor.nma`. GNSS debug switch removed again.

**Conclusion:** even under open sky the module receives nothing, neither cellular nor GNSS, and the receiver's AGC doesn't react to the change of location. With every configuration and software cause ruled out, **the RF receive path is defective** (module, or adapter/pigtail chain). A Dell reflash won't help with that.

*(later disproved in part: see Cable swap — the module and adapter were*
*fine; the pigtail chain specifically was the fault)*

### Cable swap (2026-09-25, later): cause found

*What this entry showed: the fault was the antenna pigtails all along —*
*replacing them fixed cellular registration completely, on the first try.*

We replaced the antenna pigtails with new ones; module, adapter, SIM and config unchanged.

| Check | Result |
|---|---|
| `AT+CPIN?` / `CFUN?` | READY / 1 |
| `AT+CEREG?` | **`0,1` (registered, home)** |
| `AT+COPS?` | `0,2,"26202",13` (Vodafone DE, LTE) |
| `AT+CESQ` | `17,99,255,255,4,29,75,52,57` |
| `AT+GTCCINFO?` | serving LTE cell 262-02, EARFCN 100 (B1), PCI 42; **9 neighbour cells** on EARFCN 3200/3600/6300/9460 (B7/B8/B20/B28) |

**Conclusion:** the "RF receive path defective" diagnosis was right, but the fault was in the **old pigtails**, not the module. The module and Waveshare adapter are fine. Next: data SIM (T-Mobile eSIM), `connect`/`up` with fm350mac, iperf3 sync vs async. GNSS was not re-tested.

## Glossary

Terms used on this page, defined in the [shared glossary](glossary.md): [AT command](glossary.md#at-command), [AT port](glossary.md#at-port), [ADB](glossary.md#adb), [AGC](glossary.md#agc), [AP / MD](glossary.md#ap--md), [APN](glossary.md#apn), [Band / EARFCN / PCI](glossary.md#band--earfcn--pci), [Cell ID / TAC](glossary.md#cell-id--tac), [DIPC mode](glossary.md#dipc-mode), [EN-DC / 5G NSA](glossary.md#en-dc--5g-nsa), [FCC lock](glossary.md#fcc-lock), [GNSS](glossary.md#gnss), [IMEI / IMSI / ICCID](glossary.md#imei--imsi--iccid), [libusb](glossary.md#libusb), [LTE / NR](glossary.md#lte--nr), [MIMO](glossary.md#mimo), [NV partitions / calibration](glossary.md#nv-partitions--calibration), [OEM image](glossary.md#oem-image), [OpenWrt](glossary.md#openwrt), [PDP context / data session](glossary.md#pdp-context--data-session), [Pigtail](glossary.md#pigtail), [RAT](glossary.md#rat), [RNDIS](glossary.md#rndis), [RSRP / RSRQ / SINR](glossary.md#rsrp--rsrq--sinr), [SP Flash Tool](glossary.md#sp-flash-tool), [URC](glossary.md#urc), [USB mode 40 / 41](glossary.md#usb-mode-40--41), [utun](glossary.md#utun), [W_DISABLE#](glossary.md#w_disable).
