# Fibocom L830-EB in the Waveshare USB TO M.2 B KEY on macOS: investigation report

| | |
|---|---|
| Date | 2026-11-08, 15:55–18:02 CET |
| Host | Apple Silicon Mac (`AppleT8132USBXHCI`), macOS (Darwin 27.0.0), Genesys Logic USB 3 hub in between |
| Module | used Lenovo ThinkPad **Fibocom L830-EB** (`2cb7:0210`), FW `18300.1002.00.01.01.25` (2020-05-12), Intel XMM7260 |
| Adapter | **Waveshare USB TO M.2 B KEY** (SKU 23252, schematic V1.02) |
| SIM | Telekom DE (removed 17:05–17:44; back in for the data session) |
| Status | **Working on macOS**. USB mode 0 runs stably (3 serial ports + 4 NCM interfaces, native drivers). A data session over NCM works (`en7`, static ARP entry). Load test: 346 MB download at 49 Mbit/s and 259 MB upload at 8.7 Mbit/s, both **without a module fault**; the iperf3 server cut every run short. Slow single TCP streams: packet loss on the path, the FM350 shows the same. Tools in `tools/l830/`; the O9 fix (no Bonjour/DHCP over the mobile link) is in the code but not hardware-tested |

Every statement carries one of three tags:

- **[M]** measured on this unit in this session (raw data: [Raw data](#raw-data))
- **[D]** documented, with the source linked
- **[H]** hypothesis, not yet tested

## 1. Summary

**Problem.** In the state it came in from the ThinkPad, the L830-EB **re-enumerated on USB every 15.36 s** [M], with or without a SIM and with or without an open port. AT commands worked between the drops, but no lasting connection was possible.

**Cause.** The module was in **USB mode 1 ("MBIM only, end user configuration for Win8+ OS")** [D, L8 AT manual V1.1.3, 4.2.1]. It offers a Windows MBIM interface, for which macOS has no driver [M]. Two things together explain why the module ended up in this mode:
- The Lenovo firmware also picks MBIM when the M.2 pin 44 (`GNSS_IRQ`) is high or unconnected [D, Lenovo HW manual 3.7.2].
- On the Waveshare adapter that pin is unconnected, because resistor R26 is not fitted [D, schematic].

**[H]** The 15 s cycle is most likely a firmware mechanism that resets the USB connection when no MBIM host shows up. That fits the undocumented setting `+GTSET: "USBAROUND",2,15`. The exact trigger was not tested further, because the fix makes it irrelevant.

**Fix (software, no soldering).** `AT+GTUSBMODE=0` and then `AT+CFUN=15` (hardware reset) [D, L8 AT manual 4.2.1]. Result [M]:
- The 15 s cycle is gone: a single USB session for 14 min 45 s after the switch, where mode 1 would have meant about 58 re-enumerations.
- The setting **survives a power cycle** (unplug/replug at 17:38). No script is needed after plugging in.
- The new USB layout has **3 CDC-ACM serial ports** (`/dev/cu.usbmodem11301`, `11303`, `11305`) and **4 CDC-NCM network interfaces** (`en7`–`en10`).
- macOS binds both with its built-in drivers (`AppleUSBACM*`, `AppleUSBNCM*`), so no additional software is needed.
- `11303` streams data on its own: the trace port.
- Undo: `AT+GTUSBMODE=1` + `AT+CFUN=15`.

**Side findings:**
- [M] Three read queries crash the module firmware: `AT+XLCSLSTR?`, `AT+XLCSAETTA?` and `AT+XIPCONFIG?`. They raised the exception counter from 5 to 9, and the module restarts through its boot stage `2cb7:0203`. Never send them.
- [D]+[M] The adapter cannot reset the module: RESET#, W_DISABLE and CONFIG are not driven, and per the manual all of them may float. It does feed the module rail at 3.93 V (divider; labelled 4.2 V), which is within the module's 3.135–4.4 V supply range. FULL_CARD_POWER_OFF# is pulled up to the same rail through 100 kΩ, above the 3.3 V logic maximum of 3.465 V.
- [M] The radio works in both modes: registered on Telekom.de LTE (B3/B7 cells). There was no FCC or Lenovo lock. RSRP was −84 to −106 dBm in mode 1 (16:30) and −101 to −108 dBm in mode 0 (17:44–18:00).

**Data path and load test** ([section 2a](#2a-data-session-over-ncm-on-macos-17451800-with-sim)) [M]:
- In mode 0 a data session over macOS's NCM interface `en7` works. The AT sequence is cid 2 + `AT+XDATACHANNEL` + `AT+CGDATA="M-RAW_IP"`.
- The module **does not answer ARP**, so macOS needs a static ARP entry for the gateway.
- With 4 parallel TCP streams: **346 MB download at 49.4 Mbit/s** and **259 MB upload at 8.7 Mbit/s** (13–17 Mbit/s once stable). **No USB event, no firmware exception.** The module stays stable under sustained load in both directions.
- Single TCP streams collapse to 1–4 Mbit/s: **cause is packet loss on the path**, which the FM350 shows the same way (section 2b). The iperf3 server ended all three load runs on its own (O8).

## 2. Procedure: switching the USB mode

Prerequisites: the module is in the adapter and connected, macOS shows `/dev/cu.usbmodem*`. **No SIM needed.** The tools are in [`tools/l830/`](../tools/l830/). The investigation used earlier local copies, which are functionally the same.

```
python3 tools/l830/l830_usbmode.py 0    # to mode 0 (3 ACM + NCM); "1" restores MBIM
```

The script works through these steps in order:
1. `AT+CMEE=2`, `AT+GTUSBMODE?` (expected: `1`), `AT+GTUSBMODE=?` (expected: `(0-3)`)
2. `AT+GTUSBMODE=0` → `OK`
3. `AT+GTUSBMODE?` → must read exactly `0`. Otherwise it stops **without** a reset.
4. `AT+CFUN=15` → `OK`. The module goes through the boot stage `2cb7:0203` (about 5 s) and comes back as `2cb7:0210`.

Notes:
- **Never use `AT+GTUSBMODE=3`.** Undocumented; in 2019 an L830-EB in mode 3 had no AT port left and could not be recovered ([libmbim-devel](https://web.archive.org/web/20231208194205/https://lists.freedesktop.org/archives/libmbim-devel/2019-January/001056.html)).
- In mode 0, one of the three ports (here `usbmodem11303`) is the **trace port** and streams continuously. Tools that read "until the port is quiet" hang there. `tools/fm350_diag.py`'s `TtyTransport` is one of them: it drains without a time limit on open, which is fine for the FM350 but not here. The step-4 wait in `l830_usbmode.py` hung on it; the switch itself had already gone through.
- Never send `AT+XLCSLSTR?`, `AT+XLCSAETTA?` or `AT+XIPCONFIG?` (firmware crash, see 5.2).
- Data session afterwards: see [section 2a](#2a-data-session-over-ncm-on-macos-17451800-with-sim).

## 2a. Data session over NCM on macOS (17:45–18:00, with SIM)

Tools in [`tools/l830/`](../tools/l830/):
- `l830_ncm.py up|down|status`: the AT side, no sudo
- `l830_net.sh up|down SERVER…`: the macOS side, sudo, including the O9 fix
- `l830_bench.py`: the iperf3 bench with SIM ledger, telemetry and USB recorder; `--budget`/`--prior-mb` instead of fixed campaign values
- `usb_transitions.sh`: the passive USB recorder
- `l830_common.py`: AT port discovery and the bounded drain against the trace port

The one-off diagnostics (`l830_netdiag*.sh`, `l830_tcpdiag.sh`, `fm350_udp_ab.sh`) stay local in `backups/l830-eb-20261108/scripts/`. All measurements in this section were made with the earlier local versions: `l830_net.sh up` + `probe`, without the O9 firewall/service step.

```
python3 tools/l830/l830_ncm.py up                     # cid 2, XDATACHANNEL, CGDATA -> state/l830_ncm_session.json
sudo tools/l830/l830_net.sh up 185.150.27.2           # pf anchor, service v4/v6 off, address, host route, static ARP, leak check
python3 tools/l830/l830_bench.py --bytes 5M --parallel 4 --budget 6000 --prior-mb 921
sudo tools/l830/l830_net.sh down 185.150.27.2         # undoes everything (service back to DHCP/automatic, pf token released)
python3 tools/l830/l830_ncm.py down                   # AT+CGACT=0,2
```

**AT sequence** [M], adapted from [mrhaav/openwrt atc-fib-l8x0_gl](https://github.com/mrhaav/openwrt/tree/master/atc/fib-l8x0_gl) (L850/L860, profile 0). On the L830 only cid 1–20 are allowed, and cid 1 holds the network's IMS bearer, so **cid 2** is used:

```
AT+CGDCONT=2,"IP","internet.telekom"   OK
AT+XDNS=2,1                             OK
AT+CGACT=1,2                            OK
AT+CGCONTRDP=2  -> +CGCONTRDP: 2,6,"internet.telekom.mnc001.mcc262.gprs","10.25.136.192.255.0.0.0","10.25.136.193","10.74.210.210","10.74.210.211",...
AT+XDATACHANNEL=1,1,"/USBCDC/0","/USBHS/NCM/0",2,2   OK
AT+CGDATA="M-RAW_IP",2                  OK, CONNECT
teardown: AT+CGACT=0,2  -> OK, NO CARRIER
          (the L850 teardown AT+XDATACHANNEL=0 / AT+CGDATA=0 fails on the L830: "+CME ERROR: unknown" / "operation not supported")
```

The module reports a **gateway** in `CGCONTRDP` (`.193`). The L850 package ignores this field and computes one (that would have been `.129`); we use the reported one.

**macOS side** [M]:
- `ifconfig en7 inet 10.25.136.192 netmask 255.255.255.128`
- a **host route** to the iperf server via `.193` (never a default route)
- **The module does not answer ARP.** At 17:46: 5 `who-has 10.25.136.193` unanswered, `Ipkts 0`, `ping: No route to host`.
- **Fix: a static ARP entry** `arp -s 10.25.136.193 00:00:11:12:13:15 ifscope en7`. After that the server answered 5/5 pings (RTT 33–48 ms), and the module's replies come **from** `00:00:11:12:13:15`.
- The gateway itself does not answer ping.

**Side effects** [M]:
- macOS treats `en7` as a normal network. Unprompted it sent mDNS/Bonjour (computer name, AirPlay records) and DHCP requests through it: 57 KB before the first ping.
- Without root, `netstat` shows the MAC as `02:00:00:00:00:00` and the TCP statistics as 0 (privacy masking).

**Measurements** [M] (server `185.150.27.2:5201`, LTE Telekom.de, RSRP about −101 to −108 dBm):

| Time | Test | Result | SIM |
|---|---|---|---|
| 17:47 | ping server | 5/5, 33–48 ms | < 0.1 MB |
| 17:49 | TCP down, 1 stream, 250 MB target | 15–19 Mbit/s for 2 s, then **1–4 Mbit/s**; server ended after 13.4 MB / 26 s | 14.2 MB |
| 17:51 | MTU pings DF 1300–1472 | all pass (MTU 1500 OK) | – |
| 17:51 | UDP down 10 Mbit/s, 5 s | **0 / 4,353 lost**, jitter 2.7 ms | 6.5 MB |
| 17:52 | UDP down 30 Mbit/s, 3 s | 0.2 % lost (15/7,988), 21–24 Mbit/s per second | |
| 17:52 | TCP down, **4 streams**, 4 s | **40.8 Mbit/s** total (one stream only 1.3 Mbit/s) | 37.3 MB (both) |
| 17:54 | TCP down, 4 streams, 500 MB target | **345.9 MB in 56 s = 49.4 Mbit/s**, steady 45–55 Mbit/s; then "the server has terminated" | 363.4 MB |
| 17:56 | TCP up, 4 streams, 500 MB target | **258.9 MB in 237 s = 8.7 Mbit/s**; 0–14 Mbit/s with stalls for the first ~140 s, then steady 13–17 Mbit/s; **8,903 retransmits**; then "the server has terminated" | 290.3 MB |

Across all runs [M]:
- **0 USB events**
- AT telemetry never lost
- `Total Number of Exceptions` stayed **9**
- the bearer stayed active (`CGACT 2,1`)

The module carried sustained load in both directions **without a reset**. That is the comparison with the FM350, which reset on uploads after 28–250 MB in the 5G DONGLE.

Interpretation:
- **[H]** Single TCP streams collapse under light loss; four streams in parallel together reach the link capacity. The FM350 reached 100 Mbit/s with a single stream from the same server (2026-11-05), so the server alone does not explain it.
- **[M]** "the server has terminated" also occurred with the FM350 (2026-10-05, after 3 s), while FM350 runs of up to 118.6 s completed. **[H]** Sporadic server-side behaviour, not a fixed time limit.
- **[H]** The upstream path is the weak point: many retransmits and stalls in the first 2 minutes.

## 2b. Single-stream TCP collapse: analysis (18:48–18:56)

A new session at 18:48 (IP `10.25.98.248`, gateway `.249`). Header-only capture (`l830_tcpdiag.sh`, `tcpdump -s 128`, 0 packets dropped by the kernel) of one stream each, then UDP tests (`l830_netdiag3.sh`). Evaluated with `tshark` and Python; raw data in `bench/tcpdiag-20261108-184836/` and `bench/diag3-20261108-185521/`.

**Runs** [M]:

| Run | Result |
|---|---|
| TCP down, 1 stream, 25 MB target | **23.6 MB in 83 s = 2.27 Mbit/s**, 1–3 Mbit/s from the first second; the server ended it just before the end |
| TCP up, 1 stream, 15 MB | **15.3 MB in 15.7 s = 7.8 Mbit/s**, 53 retransmits. The upload is not affected |

**Packet analysis of the download** (data connection port 53424, 16,319 data segments) [M]:
- **Loss:** 189 holes (1–4 segments: 89×1, 77×2, 15×3, 8×4), **320 lost segments = 1.96 %**, 2.25 holes/s, median spacing 375 ms.
- **No reordering.** The server's IP ID rises by +1 per segment in 98.8 % of cases. Every gap is closed by a segment with a *newer* ID, i.e. a retransmission. 0 exact duplicates, 0 spurious retransmissions, 0 segments overtaken.
- **No retransmission timeouts:** the longest pause in the data stream is 76 ms.
- **ECN negotiated** (SYN with ECE+CWR, server ECT(0)), but **0 CE marks** on 16,324 data segments. No congestion signal through ECN.
- **No zero window.** macOS: SACK, RACK, CUBIC, auto-tuning up to 4 MB, window scale 6.
- **Model check** (Mathis: rate ≈ MSS/RTT · 1.22/√p): p = 1.96 %, RTT 45 ms gives **2.24 Mbit/s predicted, 2.25 Mbit/s measured**. TCP behaves correctly; the collapse is a direct consequence of the loss.

**UDP 10 Mbit/s downstream** (`l830_netdiag3.sh`, 18:55) [M]:

| Test | Loss | per second |
|---|---|---|
| A evenly spaced (iperf3 default) | 0.83 % (36/4,359) | 8, 4, 10, 6, 8 |
| B bursty (`--pacing-timer 100000`, about 86 packets back to back every 100 ms) | 0.56 % (24/4,317) | 4, 0, 0, 18, 2 |
| C as A + upstream ping 100/s | 0.83 % (36/4,359) | 8, 6, 7, 5, 10 |
| ping (C) | 0.9 % (6/650) | – |

Ruled out [M]:
- **burst sensitivity:** B is not worse than A
- **interference from upstream traffic:** C is identical to A
- **reordering, ECN, the receive window, timeouts, MTU**

The macOS NCM driver reports 0 input errors (`IONetworkStatsKey`: 382,716 packets in, 0 errors). It runs NTB16 with 32,764-byte receive transfers and 4 outstanding receive transfers.

**The loss varies over time** [M]: the same UDP test A gave **0 %** at 17:51, 0.2 % at 30 Mbit/s at 17:52, and **0.83 %** at 18:55.

**Still open: where the loss happens.** The radio/operator network, or the L830 itself (radio part or the NCM transfer). The module offers no data counters (`+XGCNTRD`/`+XGCNTSET`: not supported).
- **[H]** The FM350 reached 100 Mbit/s with 1 stream on the same SIM, at the same place, from the same server on 2026-11-05. That points to the L830 path, but it is not a same-time comparison.
- Clearest test: an A/B comparison at the same time (SIM in the FM350, same UDP test A, about 7 MB).

**A/B with the FM350** (19:00–19:01, SIM in the Waveshare 5G DONGLE, `fm350mac up --route-host`, `utun8`; script `fm350_udp_ab.sh`) [M]:

| Module | Time | UDP 10 Mbit/s down, evenly spaced, 5 s |
|---|---|---|
| L830 | 17:51 | 0 % (0/4,353) |
| L830 | 18:55 | 0.83 % (36/4,359), 4–10 per second |
| **FM350** | **19:01** | **0.44 % (19/4,361)**, 0–6 per second |

The second FM350 run failed on the server side ("unable to read from stream socket").

**Result:** at the same place, with the same SIM and server and at nearly the same time, the FM350 also loses packets, of the same order. **[M]** The download loss is therefore **not specific to the L830**. It comes largely from the mobile network or the server path and varies over time (0 % at 17:51). The difference of 0.44 vs. 0.83 % is single runs and about 2 standard deviations, so it is not conclusive.

**Confirmation, FM350 TCP 1 stream down** (19:02, `iperf3 -R -n 25M`, same tunnel) [M]:
- **23.6 Mbit/s** in the first 2 s, then a steady **3.7–6.3 Mbit/s**; **25 MB in 38 s = 5.5 Mbit/s**
- **153 retransmissions** for about 18,100 segments, about 0.85 % loss
- The Mathis model with p ≈ 0.85 % and RTT ≈ 45 ms gives about 3.4–4 Mbit/s, consistent with the steady phase
- The same pattern as the L830 at 17:49 (fast start, then collapse)
- On 2026-11-05 the same FM350 reached 100 Mbit/s with 1 stream from the same server

The FM350 driver statistics at the end of the session (19:03) [M]: `rx=23273/34728899B tx=4835/264849B drops=0`. On the FM350 path no packet was lost between the module and the Mac either, so the loss happens before the module.

**Result for O7:** the single-stream collapse is caused by **packet loss on the path (mobile network/server path) at this time**. It affects the FM350 the same way and is **not an L830 defect**. Neither module can avoid it; several parallel streams (or protocols with loss-tolerant congestion control) mitigate it.

## 3. Method

| Step (time) | What | Changes to module |
|---|---|---|
| Enumeration (15:58) | `ioreg -p IOUSB -l`, full IORegistry subtree | none |
| AT probe (16:03–16:19) | 12 read queries; once with SIM, `AT+CFUN=1` (already 1), `AT+COPS=0` (was 2 without SIM, 0 with SIM), `AT+CGDCONT=1,…` (refused) | effectively none |
| Passive USB recording | sessionID every 2 s (16:20, 180 s), every 30–50 ms (16:30 60 s, 16:31 180 s, 17:11 without SIM 90 s, 17:21 switch 300 s, 17:28 mode 0 600 s) | none (port closed) |
| Full readout part 1 (16:27) | `AT+CLAC` (392 commands), then `?` and `=?` of every extended command, 625 queries | none intended; firmware exceptions |
| Full readout part 2 (16:34) | remaining 157 queries plus `AT+XLOG=7` / `AT+XLOG=0` (display uptime / exception, per the module's own `=?` help) after each reopen | none intended; firmware exceptions |
| Document research (16:35–17:15) | web, archive.org/Wayback, FCC filings, GitHub (sections 7, 10) | – |
| Mode switch (17:22, approved by the user) | `AT+GTUSBMODE=0`, `AT+CFUN=15` | **persistent: USB mode 1 → 0** |
| Mode-0 probe (17:31) | read-only, port roles and state | none |
| Power cycle (17:38, 17:44) | unplug and replug, then again with SIM; passive recording 17:37–17:44 | none |
| Data session (17:45) | `l830_ncm.py up` (cid 2, XDATACHANNEL, CGDATA); `l830_net.sh up/probe` (en7, host route, static ARP) | cid 2 defined/activated (**[H]** `CGDCONT` entries are usually stored persistently) |
| Diagnostics and load runs (17:49–18:00, SIM ~712 MB) | `l830_netdiag.sh`, `l830_netdiag2.sh`, `l830_bench.py` (1 and 4 streams) | none |
| Teardown (18:00–18:02) | `l830_net.sh down`, `AT+CGACT=0,2` | bearer cid 2 deactivated |

Never sent:
- basic and internal commands (`&F &D &C @NVM *CNTI`)
- `AT+COPS=?`
- phonebook, SMS and own-number reads
- the `?` form of `GTERASEDYNNVM`, `XSTRESSSIM`, `MSTART`, `XLCSSHUTDOWN`, `XLGNVRAM`
- `XLOG=2/4/5/6/8`
- `GTSET` writes
- `GTUSBMODE=2/3`

Mistakes on my part:
- In part 2 the retry logic sent the crashing LCS queries a second time, which doubled the exceptions.
- In part 1, timeouts were logged as "USB DROP" (`AtTimeoutError` is an `OSError`).
- The switch script hung on the trace port (see 2).

## 4. Module identity

| Field | Value | Source |
|---|---|---|
| Model | `+CGMM: "L830 LTE Module","L830"`; USB product `L830-EB` / `L830_EB` | [M] |
| Firmware | `18300.1002.00.01.01.25`, built `May 12 2020 12:18:42` | [M] `AT+CGMR`, `ATI` |
| Build string | `FBC_7260_V2_LC_XMM7260_V2_REV_3.0_NAND_GNSS 2020-May-12 12:25:15` | [M] `AT+XLOG=0` |
| Chipset | **XMM7260** per the firmware; a reseller says XMM7262 ([4gltemall](https://www.4gltemall.com/fibocom-l830-eb.html)) | [M]/[D] |
| Lenovo part no. | 01AX761 per retailers ([lapstore](https://www.lapstore.de/a.php/shop/lapstore/lang/en/a/40960/kw/LENOVO-ThinkPad-Fibocom-L830-EB-4G-LTE)); not checked on the label | [D] retail |
| USB serial | `004999010640000`, a generic placeholder (identical in [systemd #23288](https://github.com/systemd/systemd/issues/23288)) | [M]/[D] |
| Exception counter | `Total Number of Exceptions: 5` (16:34:16) → `9` (16:35:10); `Exception Records in NVM: 0` | [M] `AT+XLOG=7` |
| `Uptime:` | always `8`, unit undocumented, not usable as evidence | [M] |
| IMEI / IMSI / ICCID / FSN | present, redacted here | [M] |

## 5. USB

**[M] Device descriptor** (same in both modes):
- `2cb7:0210`, `bcdUSB` 0x0200, `bcdDevice` 0x0333
- class 0xEF/0x02/0x01 (IAD composite), 1 configuration
- High Speed

The boot stage is `2cb7:0203` with `bcdDevice` 0x0000 for about 5 s; descriptors in `usb/ioreg_bootstage_2cb7-0203.txt`.

**Mode 1 (as found, "MBIM")** [M], USB interface string `:020e00:0a0002:020200:0a0000:`, identical to a ThinkPad under Linux ([systemd #23288](https://github.com/systemd/systemd/issues/23288)):

| Interface | Class / Sub / Proto | macOS driver | Meaning |
|---|---|---|---|
| 0 | 0x02 / 0x0E / 0x00 | **none** | MBIM control |
| 1 "Data (OFF)" | 0x0A / 0x00 / 0x02 | none | MBIM data (NTB), alt 0 = inactive |
| 2 / 3 | 0x02 / 0x02 / 0x00 + 0x0A | `AppleUSBACMControl`/`Data` | `/dev/cu.usbmodem11303`. **[H]** Per the Lenovo manual this is the "GNSS device", which also answers AT |

**Mode 0 (after the switch)** [M], 14 interfaces:

| Interfaces | Class | macOS driver | Result |
|---|---|---|---|
| 0/1, 2/3, 4/5 | CDC-ACM | `AppleUSBACMControl`/`Data` | `/dev/cu.usbmodem11301` = **AT**, `11303` = **trace** (streams about 22 KB/s unprompted), `11305` = **AT** (probe 17:31) |
| 6/7, 8/9, a/b, c/d | CDC-NCM 0x02/0x0D + data | `AppleUSBNCMControl`/`AppleUSBNCMData` | **`en7`, `en8`, `en9`, `en10`**, MAC `00:00:11:12:13:14`/`16`/`18`/`1a`, `status: inactive` (no data session) |

Per the manual, mode 0 is "3ACM (2 AT + 1 Trace) and 3NCM"; we get **4** NCM.

State in mode 0 [M] (17:31, without SIM, read-only on `11301`):
- `+GTUSBMODE: 0`, `+CFUN: 1,0`, `AT+CPIN?` → `ERROR` (no SIM), `AT+CGDCONT?` → `ERROR`
- `+XSIO: 0, *0`, `+XCONFIG: /USBCDC/0` (both unchanged)
- `+GTSET` unchanged, including **`"USBAROUND",2,15`**. The setting is still there, but no cycle occurs in mode 0. **[H]** It only takes effect in MBIM mode.
- `AT+XLOG=7`: `Uptime: 9` (was `8` at 16:34, despite the restart at 17:22). **[H]** A cumulative counter, probably operating hours. `Total Number of Exceptions: 9` (unchanged, so the planned restart did not count as an exception).

Other USB-related values in mode 1 [M]:
- `+XSIO: 0, *0`, which per the module's own help means "AT= USIF1 USB[02]; Trace= TnD; OCT= USB1"
- `+XCONFIG: /USBCDC/0`
- `+GTSET: "USBAROUND",2,15`, `"USBAROUNDINFO",0`

Topology [M]:
- The module sits behind a Genesys USB 3 hub (`05e3:0625` + `05e3:0610`, locationID `0x01130000`). It is not on the adapter's schematic.
- The user rules the hub out, because the FM350 worked through it. The fix (cycle gone with the same hub) confirms that.

### 5.1 15 s re-enumeration in mode 1 (measured)

| Window | Condition | Events | Interval |
|---|---|---|---|
| 16:20–16:23, 2 s sampling | SIM, port closed | 12 | 14–17 s (sampling-limited) |
| 16:31–16:34, 30–50 ms sampling | SIM, port closed | 10 intervals | 15.262–15.455 s, mean **15.36 s** |
| 17:11–17:12, 30–50 ms | **no SIM**, port closed | 5 | 15.34–15.37 s |
| 17:21–17:22, 30–50 ms | no SIM | 5 | 15.34–15.35 s |

- Absent for 0.02–0.03 s, or not visible as absent at all.
- The identity stays `0210`/`0333`.
- The exception counter doesn't change.
- `+XCELLINFO` returned the serving cell 1 s after a drop, and there was no SIM re-initialisation.

So these are **USB-only reconnects, not module restarts**. A RESET# restart takes about 10 s until the module is active again [D, HW manual Fig. 3-10].

### 5.2 Firmware exceptions from read queries (measured)

| Query | Event | Back after | Exception counter |
|---|---|---|---|
| `AT+XLCSLSTR?` ×2 (16:34:35, 16:34:44) | port gone | 8.4–8.5 s | 5 → 6 → 7 |
| `AT+XLCSAETTA?` ×2 (16:34:53, 16:35:01) | port gone | 8.4–8.5 s | 7 → 8 → 9 |
| same 4 queries in part 1 (16:29:02–16:29:28) | port gone | 8.5–8.6 s | (not read) |
| `AT+XIPCONFIG?` (16:29:43) | AT silent for > 50 s, then **`2cb7:0203`** (≥ 4 s), back by 16:31:08 | > 70 s | (not read) |
| `AT+XCSG=?` | timeout, no answer | 0.4 s | unchanged |

After the exceptions the module reported `+CME ERROR: SIM BUSY` and the unsolicited `+SIM READY`. So the SIM was re-initialised, which means these were real restarts. `Exception Records in NVM: 0`.

## 6. Radio and network in mode 1 (measured with SIM, 16:27–16:35)

| Parameter | Value |
|---|---|
| `+CFUN` | `1,0` |
| `+CPIN` | `READY` (no PIN); `+MSMPD: 1` (SIM hot-plug detection on) |
| `+COPS` / `+CEREG` | `0,0,"Telekom.de",7` / `0,1` (home, E-UTRAN). Without a SIM: `+COPS: 2` |
| `+CESQ` | RSRQ 14–23 (−13 to −8.5 dB), RSRP 35–57 (−106 to −84 dBm) |
| `+RSRQ` (cells) | PCI 355 / EARFCN 1300 (B3) −10.5 dB; PCI 391 / 1300 (B3) −13.0 dB; PCI 120 / 3050 (B7) −7.5 dB |
| `+XACT` | `4,2,1,1,5,8,101,103,105,107,108,120,128`: allowed 3G+4G, preferred 4G; UMTS B1/5/8; LTE B1/3/5/7/8/20/28 ([D] decoding per [ModemManager](https://gitlab.freedesktop.org/mobile-broadband/ModemManager/-/raw/main/src/plugins/xmm/mm-modem-helpers-xmm.c)) |
| `+GTRAT` | `4,3` |
| `+CGATT` / `+CGACT` | `1` / `1,1` |
| `+CGDCONT` | only cid 1 `"IPV6","ims.mnc001.mcc262.gprs"`, active. `AT+CGDCONT=1,…` → `operation not allowed`, so use cid 2–20 for the internet APN |
| `+XRXDIV` / `+BODYSARON` | `1,3` / `0` |
| Thermal thresholds | `+GTPWROFFTH: 120000,110000,120000,120000` etc. (TPMU, TDBBCPU, TDBBSPCU, TSMARTI; unit undocumented) |

All 389 commands with read and test responses are in the [appendix](#appendix-all-parameters-read).

## 7. Waveshare USB TO M.2 B KEY versus the L830-EB

Sources: [Waveshare schematic V1.02](https://files.waveshare.com/upload/4/4d/USB_TO_M.2_B_KEY_Sch.pdf), read from rendered crops; L830-EB HW manuals V1.0.2/V1.0.3 and the Lenovo version V1.0.0 (section 10).

| M.2 pin | Adapter | L830-EB per manual | Assessment |
|---|---|---|---|
| 2/4/70/72/74 +3.3V | VBAT_VPH through R29/R30 0 Ω; SY8105I, FB 0.6 V, R5 100 k / R12 18 k → **3.93 V** (labelled "OUT:4.2v") | 3.135 / 3.3 / 4.4 V; up to 1 A, 833 mA at B28 +23 dBm | within range |
| 6 FULL_CARD_POWER_OFF# | R11 100 kΩ pull-up to the same rail | "High: Power on"; 3.3V logic max 3.465 V | module on (correct); level above the logic maximum, limited by 100 kΩ |
| 7/9 USB D+/D− | to the Type-A through 0 Ω | USB 2.0 HS | OK |
| 8 / 26 W_DISABLE1#/2# | test pads TP13/TP12 only | internal pull-up; "High/Floating: WWAN enabled" | OK |
| 10 LED1# | NET LED through 1 kΩ | "RF function ON = low (LED on)" | lit NET LED = RF on |
| 25 DPR | TP10 | "High/Floating: default power" | OK |
| **44 GNSS_IRQ** (Lenovo) | net `SDX_UIM2_CLK` → only **R26 "22R NC"** (not fitted; other side SIM1 CLK_EN) → **floating** | "MBIM/ACM system port switch": High/Floating = MBIM, Low = ACM; at boot by level, at runtime by edge with a restart | **reason the module boots into MBIM** |
| 66 SIM_DETECT | SIM slot | active high, `+MSMPD` | OK |
| 67 RESET# | TP2 + 10 nF to GND, nothing drives it | internal 100 kΩ pull-up; reset = low ≥ 10–100 ms | OK; the adapter cannot reset |
| 1/21/69/75 CONFIG | not connected | module **outputs** (NC/GND, "WWAN-SSIC 0") | OK |

LEDs ("both red"):
- The power LED (LED1) is on VBAT_VPH and is lit whenever the board has power.
- The NET LED (LED2) is lit when the module pulls LED1# low, i.e. "RF function ON". Both lit is the documented normal state.
- Waveshare doesn't document colours or meanings, and doesn't list the L830 or any Intel XMM module as supported ([wiki](https://www.waveshare.com/wiki/USB_TO_M.2_B_KEY)).

Hardware alternative, deferred by the user: pull pin 44 low. Connect the **module-side pad of R26** to GND; never bridge R26 itself, because that would connect pin 44 to SIM1 CLK. Per the Lenovo manual that also gives the ACM mode. It is not needed, because the software path works.

## 8. What is still open

| # | Question | Test | Needs |
|---|---|---|---|
| ~~O1~~ | Port roles in mode 0: **done** (17:31): `11301` AT, `11303` trace, `11305` AT; state see section 5 | – | – |
| ~~O2~~ | Long-term stability: **done**. A single USB session (`sessionID 7783147016754`) from 17:22:37 to 17:37:22 (**14 min 45 s**, including the port accesses at 17:31) with no re-enumeration; mode 1 would have meant about 58 | – | – |
| ~~O3~~ | Power cycle: **done**. Unplugged 17:38:00.6 (the Genesys hub disappeared with it, so it sits in the unplugged path), replugged 17:38:25.8 → boot stage `0203` 4.7 s → `0210` at 17:38:33.3 → again **3 ACM + 4 NCM** (`en7`–`en10`). `GTUSBMODE=0` is persistent; the pin-44 boot check does **not** override it. Afterwards **5 min 28 s without re-enumeration** (17:38:33–17:44:01), so the cycle stays away after a cold start too. At 17:44 it was replugged with the SIM, again in mode 0 | – | – |
| ~~O4~~ | Data session over NCM: **done**, see [section 2a](#2a-data-session-over-ncm-on-macos-17451800-with-sim). Still open: why single TCP streams collapse; a complete 500 MB run (the server cut every run short) | – | – |
| O5 | Exact trigger of the cycle in mode 1 (`USBAROUND`, missing MBIM host) | not needed any more | – |
| O6 | Actual voltage on the module rail and pin 6 | multimeter on C17 (3V3) and R11 | you, physically |
| ~~O7~~ | Single-stream collapse: **done** ([section 2b](#2b-single-stream-tcp-collapse-analysis-18481856)). 0.4–2 % random downstream loss on the path; the FM350 collapses the same way (19:02: 23.6 Mbit/s, then 4–6 Mbit/s, average 5.5 Mbit/s, 153 retransmissions). Not an L830 defect | – | – |
| O8 | **Deferred** (2026-11-08). A complete 500 MB run per direction: the server (`185.150.27.2`) ended all three runs on its own (26 s, 56 s, 237 s). The FM350 also had "the server has terminated" on 2026-10-05 | a different iperf3 server | up to ~1.1 GB SIM |
| O9 | macOS sends mDNS/Bonjour and DHCP through `en7` (computer name, AirPlay records). **Fixed in code, not hardware-tested**: `tools/l830/l830_net.sh up` does two things. (1) It loads a pf anchor `com.apple/l830-ncm` (evaluated through `anchor "com.apple/*"` in `/etc/pf.conf`) that passes only IPv4 from the session address to the given hosts on `en7` and drops everything else; pf is enabled with a reference token (`pfctl -E`/`-X`). (2) It switches IPv4/IPv6 configuration off for the macOS network service of `en7`, because DHCP goes out through BPF and pf cannot filter it. It then counts what the Mac still sends on `en7` (expected: 0). `down` restores DHCP/automatic and releases the token. Checked: shellcheck, ruff, self-tests. **Not checked: a run on the hardware** (the user ended the tests) | `l830_net.sh up`: the leak counter must show 0 | ~1 MB SIM |

## 9. Corrections to earlier statements in this session

- "L830-EB = Intel XMM7160" in the plan: wrong; the firmware says XMM7260.
- "2 antenna connectors (MAIN/AUX)": now confirmed, "M" and "D/G" (HW manual V1.0.2, 4.1.1).
- `AT+XACT` decoding: correct, but unverified at the time; now backed by the ModemManager source.
- "The module drops when the port is opened": refuted; it also drops with the port closed and without a SIM.
- "Lenovo host check": speculation; the actual cause is USB mode 1 (MBIM) combined with the pin-44 boot logic (section 1).
- Several early statements (hub, power, LED meaning) came without sources. The user objected; from then on every statement carries M/D/H.

## 10. Sources

Fibocom documents (copies in `backups/l830-eb-20261108/manuals/`):
- **L8-Family AT Commands User Manual V1.1.3 (2015-09-28)**, 284 pages, applies to "The L810, L830". Wayback capture from 2019-03-04 of `http://szght9.us05.host.35.com/upfile/down/file_2_3_3_2_14_1.pdf`. Sections read: 4.2.1 `+GTUSBMODE`, 4.3.1 `+GTSET`, 9.2.2 `+CFUN`. No `USBAROUND`.
- **L830-EB Hardware User Manual – Lenovo Customization Version V1.0.0 (2017-07-04)**, FCC filing ZMOL830EB: https://fccid.io/ZMOL830EB/User-Manual/User-Manual-3526080. Read: 3.4 (USB driver mapping), 3.7.2 (System Switch, pin 44).
- L830-EB Hardware User Manual V1.0.3 (2017-06-26, applicability L830-EB-11): https://gzhls.at/blob/ldb/d/b/c/6/b55de51fcf49ee0187f8a88476ae536b8675.pdf. Read: 3.1.2, 3.2–3.7.
- L830-EB-11 Hardware User Manual V1.0.2 (2017-09-21), ManualsLib print: https://www.manualslib.com/manual/2718077/Fibocom-L830-Eb-11.html. Adds 3.10/3.11/4.1.1 and the 10 s boot time; no USB-mode content.
- FCC test report 170712W003-1 (ZMOL830EB, 2017-08-24): https://fccid.io/ZMOL830EB/Test-Report/RF-Test-Report-22-3526077. No USB or host details (3.3 V lab supply, HP PC, radio tester; FW `…01.01.04_R04`).
- L860 AT Commands User Manual V3.2.3 (2019): https://www.docdroid.net/gEsTYmk/fibocom-l860-at-commands-user-manual-v323-pdf. `+GTWAKE`, `+MSMPD`; `+GTUSBMODE` "deleted". The Scribd copy "Fibocom L8-Family at Commands User Manual" (Wayback) is the same document.
- L860-GL-16 Hardware Guide V1.0.0 (2020): https://fccid.io/ZMOL860GL16/User-Manual/L860-GL-16-User-Guide-updated-5120529. PCIe module; no USB-mode content.
- Fibocom MBB AT Commands Manual V2.4 (Scribd via Wayback): newer modules (modes 17–33). Only the general description is relevant: "two main profiles: AT+NCM … and MBIM … activated after a reset".
- Not relevant: FCC ID ZMOB830GL is the Bluetooth module B830-GL.

Waveshare: [schematic V1.02](https://files.waveshare.com/upload/4/4d/USB_TO_M.2_B_KEY_Sch.pdf), [wiki](https://www.waveshare.com/wiki/USB_TO_M.2_B_KEY), [product page](https://www.waveshare.com/usb-to-m.2-b-key.htm).

Community and code:
- [libmbim-devel 2019-01, L830-EB stuck in GTUSBMODE=3](https://web.archive.org/web/20231208194205/https://lists.freedesktop.org/archives/libmbim-devel/2019-January/001056.html)
- [mrhaav/openwrt atc-fib-l8x0_gl](https://github.com/mrhaav/openwrt/tree/master/atc/fib-l8x0_gl) ("Change to NCM mode with AT+GTUSBMODE=0", L850/L860)
- [systemd #23288](https://github.com/systemd/systemd/issues/23288) (L830-EB-00 udev data, mode-1 layout)
- [Thomster/omarchy-fibocom-l830](https://github.com/Thomster/omarchy-fibocom-l830) (L830-EB under Linux via ModemManager/MBIM)
- [xmm7360-usb-modeswitch](https://github.com/xmm7360/xmm7360-usb-modeswitch) (L850, GTUSBMODE 7 = MBIM)
- [ModemManager XMM helpers](https://gitlab.freedesktop.org/mobile-broadband/ModemManager/-/raw/main/src/plugins/xmm/mm-modem-helpers-xmm.c) (XACT)
- [ModemManager list 2020-06](https://www.mail-archive.com/modemmanager-devel@lists.freedesktop.org/msg05966.html) (L850 boot stage with a different ID)
- [Linux commit L850 remote-wake quirk 2021](https://lkml.rescloud.iu.edu/hypermail/linux/kernel/2103.2/05416.html)
- [Debian #1091338](https://www.mail-archive.com/debian-bugs-dist@lists.debian.org/msg2008653.html) (L830 as `2cb7:0002` with NCM)
- [4gltemall](https://www.4gltemall.com/fibocom-l830-eb.html)

Searched without a result for `USBAROUND`: web (3 searches), GitHub code search, all of the manuals above.

## Raw data

The reusable tools are in the repo under [`tools/l830/`](../tools/l830/); their run state is in the git-ignored `tools/l830/state/` and bench results in `tools/bench-results/l830-*`. Raw data is local under **`backups/l830-eb-20261108/`**. It is git-ignored ("never commit") because it contains the FSN, cell IDs and IMS addresses.

- `at/`: complete AT transcripts (`l830_readout.txt`, `l830_readout2.txt`; IMEI/ICCID masked), `readout.json`, the switch log `l830_usbmode_0.log`
- `usb/`: IORegistry dumps for mode 1, the boot stage `0203` and mode 0; USB time series (`transitions_*.log`, `l830_usb_*.log`)
- `scripts/`:
  - readout: `l830_probe.py`, `l830_full_readout.py`, `l830_readout2.py`, `l830_mode0_probe.py`
  - mode switch: `l830_usbmode.py`
  - recorder: `usb_transitions.sh`
  - data session: `l830_ncm.py`, `l830_net.sh` (+ the captures `l830_net_capture*.txt`)
  - load and diagnostics: `l830_bench.py`, `l830_netdiag.sh`, `l830_netdiag2.sh`
- `bench/`: one folder per run (`20261108-174932`, `-175403`, `-175601`, `diag-…`, `diag2-…`) with iperf3 JSON, `telemetry.log`, USB `transitions.log` and `SUMMARY.md`. The `SUMMARY.md` of the 17:56 upload shows `0.0` Mbit/s because of an evaluation bug fixed afterwards; it was really 8.7 Mbit/s
- `at/l830_mode0_probe_1731.txt`, `usb/transitions_powercycle_1738.log`, `usb/transitions_mode0_10min_1728.log`
- `manuals/`: the PDFs from section 10
- `research/`: research notes and schematic crops

## Appendix: all parameters read

Read and test form of each of the 389 extended commands the module lists in `AT+CLAC`. Identifiers (IMEI, IMSI, ICCID, FSN, cell IDs, addresses, firmware UUID) are redacted. "not queried" means deliberately skipped (see section 2).

| Command | Read (`?`) – current value | Test (`=?`) – supported values |
|---|---|---|
| `+CLAC` | +CME ERROR: operation not supported | +CME ERROR: operation not supported |
| `+CMER` | +CMER: 2 ,0 ,0 ,0 ,0 | +CMER: (1,2),0,0,(0-1),0 |
| `+CGSMS` | +CGSMS: 1 | +CGSMS: (0-3) |
| `+CMGD` | +CME ERROR: operation not supported | +CMGD: (1-20),(0,1,2,3,4) |
| `+CMGF` | +CMGF: 0 | +CMGF: (0-1) |
| `+CMGR` | not queried | OK (empty) |
| `+CMGS` | +CME ERROR: operation not supported | OK (empty) |
| `+CMGW` | +CME ERROR: operation not supported | OK (empty) |
| `+CMMS` | +CMMS: 0 | +CMMS: (0-2) |
| `+CMSS` | +CME ERROR: operation not supported | OK (empty) |
| `+CNMA` | +CME ERROR: operation not supported | +CNMA: (0-2) |
| `+CNMI` | +CNMI: 1,1,0,0,0 | +CNMI: (0-2),(0-3),(0-3),(0-2),(0-1) |
| `+CPMS` | +CPMS: "SM",0,20,"SM",0,20,"SM",0,20 | +CPMS: ("BM","ME","SM","SR"),("SM"),("BM","SM","SR") |
| `+CSCA` | (redacted) | OK (empty) |
| `+CSCB` | +CSCB: 0,"","" | +CSCB: (0-1) |
| `+CSMS` | +CSMS: 0,1,1,1 | +CSMS: (0-1) |
| `+CNEM` | +CNEM: 0 | OK (empty) |
| `+XSMSFDN` | +XSMSFDN: 0 | +XSMSFDN: (0-3) |
| `+SMMFULL` | +SMMFULL: 0 | +SMMFULL: (0-1) |
| `+XCMAS` | +XCMAS: 0 | +XCMAS: (0,1) |
| `+XCSSMS` | +XCSSMS: 0 | +XCSSMS: (0-1),(1-48) |
| `+XSMS` | +XSMS: 0,1 | +XSMS: (0-1) |
| `+XTESM` | +XTESM: 0 | +XTESM: (0-1) |
| `+XWAPNMI` | +XWAPNMI: 0,0 | +XWAPNMI: (0-1) |
| `+CSAS` | +CME ERROR: operation not supported | +CSAS: (0) |
| `+CRES` | +CME ERROR: operation not supported | +CRES: (0) |
| `+CSDH` | +CSDH: 0 | +CSDH: (0-1) |
| `+CMGC` | +CME ERROR: operation not supported | OK (empty) |
| `+CSMP` | +CSMP: 17,168,0,0 | OK (empty) |
| `+CGREG` | +CGREG: 0,0 | +CGREG: (0-3) |
| `+COPN` | +CME ERROR: operation not supported | OK (empty) |
| `+COPS` | +COPS: 0,0,"Telekom.de",7 | not queried |
| `+CREG` | +CREG: 0,1 | +CREG: (0-3) |
| `+CIINFO` | +CIINFO: 0 ,0,2,2<br>+CIINFO: 0,1,255,255 | +CIINFO: (0,1),(0-1),(0,1,2,255),(0,1,2,255) |
| `+XCOPS` | +CME ERROR: operation not supported | +XCOPS: (0-9,11-13) |
| `+XCSPAGING` | +CME ERROR: operation not supported | +XCSPAGING: (0-1) |
| `+XEONS` | +CME ERROR: operation not supported | OK (empty) |
| `+XINEM` | (redacted) | +XINEM: (0,1) |
| `+XREG` | (redacted) | +XREG: (0,1) |
| `+XAACOPS` | +CME ERROR: operation not supported | +XAACOPS: (1-24),(1-24),(0-999),(0-999),[(0-65535)[,(0-65535)[,(0-65535)]]][,(1-3),(0-65535)...[,(0-65535)]][,(0-65535)[,(0-65535)[,(0-65535)]]] |
| `+GTACT` | +XACT: 4,2,1,1,5,8,101,103,105,107,108,120,128 | +XACT: (0-6),(0-2),(0-2),0,1,5,8,101,103,105,107,108,120,128 |
| `+XACT` | +XACT: 4,2,1,1,5,8,101,103,105,107,108,120,128 | +XACT: (0-6),(0-2),(0-2),0,1,5,8,101,103,105,107,108,120,128 |
| `+WS46` | +WS46: 25 | +WS46: (12,22,25,28,29,30,31) |
| `+CEREG` | +CEREG: 0,1 | +CEREG: (0-3) |
| `+CEMODE` | +CEMODE: 2 | +CEMODE: (0-3) |
| `+XLRTA` | +CME ERROR: operation not supported | OK (empty) |
| `+CPLS` | +CPLS: 0 | +CPLS: (0-2) |
| `+CPOL` | (redacted) | +CPOL: (1-8),(0-2) |
| `+XHOMEZR` | (redacted) | +XHOMEZR: (0-1) |
| `+XCSG` | +XCSG: 0 | no answer (failed twice) |
| `+CIREP` | +CIREP: 0, 1 | +CIREP: (0,1) |
| `+XCSQ` | +XCSQ: 0, 4, 19 | +XCSQ: (0-1) |
| `+XSYSERR` | +XSYSERR: 0 | +XSYSERR: (0,1) |
| `+XCSFB` | +XCSFB: 2 | +XCSFB: (0-5) |
| `+XDAMCFG` | +XDAMCFG: 0,6,5,5,60,30,60 | +XDAMCFG: (0-2),(1-60),(1-60),(1-60),(1-60),(1-60),(1-60) |
| `+XCMI` | +CME ERROR: operation not supported | +CME ERROR: operation not supported |
| `+XCMIEXT` | +XCMIEXT: 0,4000,,,,,,, | OK (empty) |
| `+XNIMTC` | +XNIMTC: 1,1,0,0,0,0,0 | +XNIMTC: (0-4),(0,1),(0,1),(0,1),(0-255),(0,1),(0,1),(0-255) |
| `+XSIB16INFO` | (redacted) | +XSIB16INFO: (0,1) |
| `+XIMSIREG` | (redacted) | +XIMSIREG: (0-3) |
| `+XMCI` | (redacted) | +XMCI: (0,1) |
| `+XLNWC` | (redacted) | +XLNWC: (0,1) |
| `+XTIMER` | +XTIMER: 0 | +XTIMER: (0-2),(0,1) |
| `+XCABANDCFG` | +CME ERROR: operation not supported | +XCABANDCFG: (0-2) |
| `+CHUP` | +CME ERROR: operation not supported | OK (empty) |
| `+CMOD` | +CMOD: 0 | +CMOD: (0-4) |
| `+CTFR` | +CME ERROR: operation not supported | OK (empty) |
| `+XCALLSTAT` | +XCALLSTAT: 0 | +XCALLSTAT: (0-1) |
| `+XSPEECHCONFIG` | +XSPEECHCONFIG:4,3,2,1,0,8,7,6,5 | +XSPEECHCONFIG: (0-8) |
| `+XDIAG` | +CME ERROR: operation not supported | OK (empty) |
| `+XEMC` | +XEMC:0 | +XEMC:(0-1) |
| `+XDTMF` | +XDTMF: 1 | +XDTMF: (0-1) |
| `+XVTS` | +CME ERROR: operation not supported | OK (empty) |
| `+CSTA` | +CSTA: 129 | +CSTA: (128-255) |
| `+CVHU` | +CVHU: 0 | +CVHU: (0-2) |
| `+VTD` | +VTD: 5 | +VTD: (0-255 x 100 ms) |
| `+CCWE` | +CCWE: 0 | +CCWE: (0-1) |
| `+CR` | +CR: 0 | +CR: (0-1) |
| `+CRC` | +CRC: 0 | +CRC: (0-1) |
| `+XPROGRESS` | +XPROGRESS: 0 | +XPROGRESS: (0-1) |
| `+XREDIAL` | +XREDIAL: 0, 0 | +XREDIAL: (0-1),(0-2) |
| `+XLIN` | +XLIN: 1 | +XLIN: (1-2) |
| `+CAOC` | +CAOC: 1 | +CAOC: (0-2) |
| `^DSCI` | +CME ERROR: operation not supported | +CME ERROR: operation not supported |
| `^SYSINFO` | +CME ERROR: operation not supported | +CME ERROR: operation not supported |
| `^SYSCONFIG` | +CME ERROR: operation not supported | +CME ERROR: operation not supported |
| `+CCFC` | +CME ERROR: operation not supported | +CCFC: (0-5) |
| `+CCWA` | +CCWA: 0 | +CCWA: (0-1) |
| `+CHLD` | +CME ERROR: operation not supported | +CHLD: (0,1,1x,2,2x,3,4,5,6,7,8) |
| `+CLCC` | +CLCC: 0 | +CLCC: (0-1) |
| `+CLCK` | +CME ERROR: operation not supported | +CLCK: ("AO","OI","AI","IR","OX","AB","AG","AC","PS","PN","PU","PP","PC","SC","FD") |
| `+CLIP` | ERROR | +CLIP: (0,1) |
| `+CLIR` | ERROR | +CLIR: (0-2) |
| `+CNAP` | ERROR | +CNAP: (0,1) |
| `+COLP` | ERROR | +COLP: (0,1) |
| `+COLR` | ERROR | OK (empty) |
| `+CPWD` | +CME ERROR: operation not supported | +CPWD: ("SC",8),("P2",8),("PS",16),("PN",16),("PU",16)("PP",16),("PC",16),("AO",4),("OI",4),("OX",4),("AI",4),("IR",4),("AB",4),("AG",4),("AC",4) |
| `+CSSN` | +CSSN: 0,0 | +CSSN: (0-1),(0-1) |
| `+CUSD` | +CUSD: 0 | +CUSD: (0-2) |
| `+CCUG` | +CCUG: 0,0,0 | +CCUG: (0-1),(0-10),(0-3),(10-32767) |
| `+CBST` | +CBST: 71,0,1 | +CBST: (0,4-7,12,14-17,39,43,47-51,68,70-71,75,79-84,115-116,120-121,130-134),(0-1,4-5),(0-3) |
| `+CEER` | +CME ERROR: operation not supported | OK (empty) |
| `+CGACT` | +CGACT: 1,1 | +CGACT: (0,1) |
| `+CGANS` | +CME ERROR: operation not supported | +CGANS: (0,1), (PPP,M-OPT-PPP,M-HEX,M-RAW_IP) |
| `+CGATT` | +CGATT: 1 | +CGATT: (0,1) |
| `+CGAUTO` | +CGAUTO: 3 | +CGAUTO: (0-4) |
| `+CGCLASS` | +CGCLASS: "B" | +CGCLASS: ("A","B","CC","CG") |
| `+CGCMOD` | +CME ERROR: operation not supported | +CGCMOD: (1) |
| `+CGDATA` | +CME ERROR: operation not supported | +CGDATA: (PPP,M-OPT-PPP,M-HEX,M-RAW_IP,PAD,X25) |
| `+CGDCONT` | +CGDCONT: 1,"IPV6","ims.mnc001.mcc262.gprs","<ipv6>",0,0,0,0,0,0 | +CGDCONT: (1-20),"IP",,,0,(0,1,2,3,4),(0,1),(0,1),(0,1),(0,1)<br>+CGDCONT: (1-20),"IPV6",,,0,(0,1,2,3,4),(0,1),(0,1),(0,1),(0,1)<br>+CGDCONT: (1-20),"IPV4V6",,, … |
| `+CGDSCONT` | +CGDSCONT: 0,0,0,0,0 | +CGDSCONT: (1-20),(),"IP",,,(0-2), (0,1,2,3,4),(0,1)<br>+CGDSCONT: (1-20),(1),"IPV6",,,(0-2), (0,1,2,3,4),(0,1) |
| `+CGEQMIN` | +CGEQMIN: 1,0,0,0,0,0,0,0,"0E0","0E0",0,0,0,0,0 | +CGEQMIN: "IP",(0-3),(1-63 in 1 kbps steps, 64-568 in 8 kbps steps, 576-8640 in 64 kbps steps),(1-63 in 1 kbps steps, 64-568 in 8 kbps steps, 576-8640 in 64 kbp … |
| `+CGEQNEG` | +CME ERROR: operation not supported | +CGEQNEG: (1) |
| `+CGEQREQ` | +CGEQREQ: 1,4,0,0,0,0,0,0,"0E0","0E0",3,0,0,0,0 | +CGEQREQ: "IP",(0-4),(1-63 in 1 kbps steps, 64-568 in 8 kbps steps, 576-8640 in 64 kbps steps),(1-63 in 1 kbps steps, 64-568 in 8 kbps steps, 576-8640 in 64 kbp … |
| `+CGEREP` | +CGEREP: 0,0 | +CGEREP: (0-2),(0-1) |
| `+CGPADDR` | (redacted) | +CGPADDR: (1) |
| `+CGQMIN` | +CGQMIN: 1,0,0,0,0,0 | +CGQMIN: "IP",(0-3),(0-4),(0-5),(0-9),(0-18,31) |
| `+CGQREQ` | +CGQREQ: 1,0,0,0,0,0 | +CGQREQ: "IP",(0-3),(0-4),(0-5),(0-9),(0-18,31) |
| `+CGTFT` | +CGTFT: 0, 0, 0, "0.0.0.0.0.0.0.0", 0, 0.0, 0.0, 00000000, 0.0, 00000, 0 | +CGTFT: IP, (1 - 16), (0 - 255), (0.0.0.0.0.0.0.0 - 0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0), (0 - 255), (0.0 - 65535.65535), (0.0 - 655 … |
| `+XCGCLASS` | +XCGCLASS: "B" | +XCGCLASS: ("B","CC") |
| `+XDNS` | +XDNS: 1, "10.74.210.210", "10.74.210.211"<br>+XDNS: 1, "10.74.210.210", "10.74.210.210"<br>+XDNS: 1, "10.74.210.210", "10.74.210.211"<br>+XDNS: 1, "10.74.210.210", "<ipv6>"<br>+XDNS: 1, "10.74.210.210", "<ipv6>" | +XDNS: (1-20),(0-3) |
| `+MGAUTH` | +CME ERROR: operation not supported | +XGAUTH: (1-20),(0-2),64,64 |
| `+XGAUTH` | +CME ERROR: operation not supported | +XGAUTH: (1-20),(0-2),64,64 |
| `+CSCON` | +CSCON: 0,0 | +CSCON: (0-2) |
| `+XMULTISLOT` | +XMULTISLOT: 33,33,33,33 | +XMULTISLOT: (1-12,30-33,35-38), (1-12,30-33,35-38), (0,5,9,11,31-33,36-38), (0,5,9,11,31-33,36-38) |
| `+XGCNTRD` | +CME ERROR: operation not supported | OK (empty) |
| `+XDATASTAT` | +XDATASTAT: 0,0 | +XDATASTAT: (0,1) |
| `+XGCNTSET` | +CME ERROR: operation not supported | +XGCNTSET: (0-20),(0-2147483646),(0-2147483646) |
| `+FCLASS` | 0 | (0,8) |
| `+CRLP` | +CRLP: 61,61,48,6 | +CRLP: (0-61),(0-61),(39-255),(1-255) |
| `+CGEQOS` | +CGEQOS: 1,9,0,0,0,0 | +CGEQOS: (1-20),(0-9),(1-63 in 1 kbps steps, 64-568 in 8 kbps steps, 576-8640 in 64 kbps steps, 8700-16000 in 100 kbps steps),(1-63 in 1 kbps steps, 64-568 in 8 … |
| `+CGEQOSRDP` | +CME ERROR: operation not supported | +CGEQOSRDP: 1) |
| `+CGTFTRDP` | +CME ERROR: operation not supported | +CGTFTRDP: (1) |
| `+CGCONTRDP` | (redacted) | +CGCONTRDP: 1) |
| `+CGSCONTRDP` | +CME ERROR: operation not supported | +CME ERROR: operation not allowed |
| `+XPCO` | +CME ERROR: operation not supported | +XPCO: (1,2) |
| `+XMULTISIM` | +CME ERROR: operation not supported | +XMULTISIM:(0-1),(0-4) |
| `+XDATACONTROL` | +CME ERROR: operation not supported | +XDATACONTROL: (0,1) |
| `+XNVMPLMN` | (redacted) | +XNVMPLMN: (0-2),(0,1,2),(1-50),[(0-999),(0-999),(0-4294967293)[,(0-65535)[,(0-65535)....[,(0-65535)]....]],(0-4294967293),(0-63)] |
| `+XNVMMCC` | (redacted) | +XNVMMCC: (0-1)[,(1-20)[,(0-999),(0-65535),(0-65535),(0-4294967293),(0-4294967293)]] |
| `+CCID` | (redacted) | OK (empty) |
| `+CCLK` | +CCLK: "04/01/01,00:10:55+04" | OK (empty) |
| `+CFUN` | +CFUN: 1,0 | +CFUN: ( 0,1,4,5,6,7,8,16,15,27,31,32,33,34,35,36,37,38),(0-1) |
| `+CIMI` | (redacted) | OK (empty) |
| `+CMEE` | +CMEE: 2 | +CMEE: (0-2) |
| `+CMUX` | +CMUX: 0,0,,0,0,0,0,0,0 | +CMUX: (0),(0),,(1-1509),(1-255),(0-5),(2-255),, |
| `+CPIN` | +CPIN: READY | OK (empty) |
| `+CPWROFF` | +CME ERROR: operation not supported | OK (empty) |
| `+CRSM` | (redacted) | OK (empty) |
| `+CSCS` | +CSCS: "IRA" | +CSCS: ("UCS2", "IRA", "HEX", "GSM") |
| `+CSIM` | +CME ERROR: operation not supported | OK (empty) |
| `+CSVM` | +CME ERROR: operation not allowed | +CSVM: (0-1),(128-255) |
| `+CTZR` | +CTZR: 0 | +CTZR: (0-1) |
| `+CTZU` | +CTZU: 0 | +CTZU: (0-1) |
| `+IPR` | +IPR: 115200 | +IPR: (300,1200,2400,4800,9600,19200,38400,57600,115200,230400,460800,500000,750000,921600,1843200,3000000,3250000,6000000),() |
| `+XCTMS` | +XCTMS: 0 | +XCTMS: (0-3) |
| `+XGENDATA` | (redacted) | +XGENDATA: (0-7) |
| `+XPINCNT` | +CME ERROR: operation not supported | +XPINCNT: (1-9) |
| `+XGATR` | +CME ERROR: operation not supported | OK (empty) |
| `+XLOG` | +CME ERROR: operation not supported | +XLOG: (0,2,4,5,6,7,8)[,"number"][,<urrc_error_type>]<br>+XLOG=0 (default) : display exception<br>+XLOG=2 : delete exception from NVRAM<br>+XLOG=4 : invoke exce … |
| `+CMMIVT` | ERROR | +CMMIVT: (1,2) |
| `+GCAP` | +CME ERROR: operation not supported | OK (empty) |
| `+CPINR` | +CME ERROR: operation not supported | OK (empty) |
| `+CESQ` | +CME ERROR: operation not supported | +CESQ: (0-63,99),(0-7,99),(0-96,255),(0-49,255),(0-34,255),(0-97,255) |
| `+XCESQ` | +XCESQ: 0,99,99,255,255,23,37,14 | +XCESQ: (0,1),(0-63,99),(0-7,99),(0-96,255),(0-49,255),(0-34,255),(0-97,255),(-100-100,255) |
| `+XCESQRC` | (redacted) | +XCESQRC: (0,1),(0-2),(0,1) |
| `+XMER` | +XMER: 0 | +XMER: (0-1) |
| `+XSIMSTATE` | +XSIMSTATE: 0,7,1,1<br>+XLOCK: "PN",5,0,"PU",5,0,"PP",5,0,"PC",5,0,"PS",5,0 | +XSIMSTATE: (0,1) |
| `+XSYSCHANGEIND` | +XSYSCHANGEIND: 0 | +XSYSCHANGEIND : (0-1) |
| `+CIND` | +CIND:0,0 | +CIND :     ("call",(0,1)),     ("roam",(0,1)) |
| `+CSUS` | +CSUS: 0 | +CSUS: 1 |
| `+XL1SET` | +CME ERROR: operation not supported | +CME ERROR: operation not supported |
| `+XSIO` | +XSIO: 0, *0 | +XSIO: [ XMMREL8+] Variant=0 :  AT= USIF1 USB[02]; Trace= TnD; OCT= USB1;;<br>+XSIO: Variant=1  :  AT= USB[02]; Trace= TnD; OCT= USB1;;<br>+XSIO: Variant=2  :   … |
| `+XDLCTEST` | +CME ERROR: operation not supported | +XDLCTEST: (1-4),(0-63),(0-65535),(0-2047),(0-255) |
| `+XPOW` | +XPOW: 3,0,0 | +XPOW: (0-1),(0-65535),(0-255) |
| `+XCEER` | +CME ERROR: operation not supported | OK (empty) |
| `+XEER` | +CME ERROR: operation not supported | OK (empty) |
| `+XMUX` | +XMUX: 1,1,0,0,0,0,0,0,0,0,0 | +XMUX: "AT+XMUX=action[,p1][,p2][,p3][,p4][,p5][,p6][,p7][,p8][,p9][,p10]" |
| `+XFDOR` | +XFDOR: 2,4,5 | +XFDOR: (1-3)[,(1-60)][,(1-120)] |
| `+XFDORT` | +XFDORT: 5 | +XFDORT: (0-120) |
| `+XCFC` | +CME ERROR: operation not supported | OK (empty) |
| `+XEMN` | (redacted) | OK (empty) |
| `+CSSAC` | +CME ERROR: operation not supported | OK (empty) |
| `+XCONFIG` | +XCONFIG: /USBCDC/0 | +XCONFIG: (0-5),(0,1) |
| `+XAPP` | +CME ERROR: operation not supported | [+XAPP: ""<br>[+XAPP: "siotest"]] |
| `+XHSDUPA` | +XHSDUPA: 1, 24, 1, 6 | +XHSDUPA: (0-1),(6,8,10,12-18,19-24), (0,1),(1,2,6,7) |
| `+XCAP` | +XCAP: 0 | +XCAP: (0,1) |
| `+XFDSLEEP` | +CME ERROR: operation not supported | OK (empty) |
| `+CAVIMS` | +CME ERROR: unknown | +CAVIMS: (0,1) |
| `+CASIMS` | +CME ERROR: unknown | +CASIMS: (0,1) |
| `+XLDRAPNE` | +CME ERROR: operation not supported | +CME ERROR: operation not supported |
| `+XLDR` | +XLDR: 0 | +XLDR: (0-1) |
| `+XETWCFG` | +XETWCFG: 0, 0, 0, 0 | +XETWCFG: (0,1),(0,1),(0,1),(0,1) |
| `+XETWNTFYSTART` | +CME ERROR: operation not allowed | +XETWNTFYSTART: (0-6),(1-100),(0-65535),(0-65535) |
| `+XETWNTFYSTOP` | +CME ERROR: operation not supported | +XETWNTFYSTOP: ((0-6),255) |
| `+XMETRIC` | +CME ERROR: operation not supported | +XMETRIC: (0,1),(10,11,12,13,14),1<br>+XMETRIC: (0,1),(0,1,2,3,4,5,6,7,8,9),2,(0-25),(0-25) |
| `+XNRTAPP` | +XNRTAPP: 8,4 | +XNRTAPP: (0-65535),(0-65535) |
| `+XNRTCWS` | +XNRTCWS: 0,-1,-1,-1,-1 | +XNRTCWS: (0-2),(0,1),(0-2),(0,1) |
| `+CAMM` | +CME ERROR: SIM FAILURE | OK (empty) |
| `+CACM` | +CME ERROR: SIM FAILURE | OK (empty) |
| `+XDATACHANNEL` | +CME ERROR: operation not supported | +XDATACHANNEL: (0-2),(0,1),(0,1,2),(1-20) |
| `+GTUSIM` | +GTUSIM: 1 | +CME ERROR: operation not supported |
| `+CPIN2` | +CPIN2: SIM PIN2 | OK (empty) |
| `+CONNECTPORT` | +CME ERROR: operation not supported | +CME ERROR: operation not supported |
| `+CCHO` | +CME ERROR: operation not supported | OK (empty) |
| `+CCHC` | +CME ERROR: operation not supported | OK (empty) |
| `+XCSP` | +CME ERROR: operation not supported | +XCSP: (1-9, c0, d5) |
| `+NEER` | +CME ERROR: operation not supported | OK (empty) |
| `+CUAD` | +CME ERROR: operation not supported | +CUAD: (0,1) |
| `+CEAP` | +CME ERROR: operation not supported | OK (empty) |
| `+CERP` | +CME ERROR: operation not supported | OK (empty) |
| `+XUICC` | +XUICC: 1 | +CME ERROR: operation not supported |
| `+XLEMA` | +XLEMA: 1,5,"0199220110",8,1,262<br>+XLEMA: 2,5,"110",1,1,262<br>+XLEMA: 3,5,"112",4,1,262<br>+XLEMA: 4,5,"112",,1<br>+XLEMA: 5,5,"911",,1 | +XLEMA: (0-6) |
| `+XAUTH` | +CME ERROR: operation not supported | OK (empty) |
| `+CEN` | +CEN1: 0,262,01<br>+CEN2: 8,"0199220110"<br>+CEN2: 1,"110"<br>+CEN2: 4,"112" | +CEN: (0,1) |
| `+XSYSTRACE` | +CME ERROR: operation not supported | +XSYSTRACE: (0,1,10-17,20,30-33,40,50),["<master>=<port>[;<master>=<port>[;...]]"],["<master>=<config>[;<master>=<config>[;...]]"],["[oct=<oct_mode>][;pti1=<pti … |
| `+XDBGCONF` | +CME ERROR: operation not supported | +CME ERROR: operation not supported |
| `+XABBTRACE` | +CME ERROR: operation not supported | +CME ERROR: operation not supported |
| `+CPUC` | +CME ERROR: SIM FAILURE | OK (empty) |
| `+CLAN` | +CME ERROR: operation not allowed | OK (empty) |
| `+CGLA` | +CME ERROR: operation not supported | OK (empty) |
| `+CRLA` | +CME ERROR: operation not supported | OK (empty) |
| `+CPAS` | +CME ERROR: operation not supported | +CPAS: (0-5) |
| `+SETLITRACE` | +CME ERROR: operation not supported | +SETLITRACE: (0-2),(80000000,40000000,20000000,10000000,8000000,4000000,2000000,1000000,800000,400000,200000,100000,80000,40000,20000,10000,8000,4000,2000,1000, … |
| `+XSVM` | +CME ERROR: operation not allowed | +XSVM: (1,2),(0,1),(44),(128-255) |
| `+XNOTIFYDUNSTATUS` | +XNOTIFYDUNSTATUS:0 | +XNOTIFYDUNSTATUS:(0, 1) |
| `+XRXDIV` | +XRXDIV: 1,3 | +XRXDIV: (0-1)[,(0-3)] |
| `+XRXDIV3GRAB` | +XRXDIV3GRAB: 0, 0, 0, 0, 0, 0 | +XRXDIV3GRAB: (0-2), (0-2), (0-2), (0-2), (0-2), (0-2) |
| `+XMAGETKEY` | +CME ERROR: operation not supported | +XMAGETKEY: ("trace.dec.zip") |
| `+XMAGETBLOCK` | +XMAGETBLOCK: ("No file specified"),0,0,0 | +CME ERROR: operation not allowed |
| `+XTSM` | +CME ERROR: operation not supported | +XTSM: (0-3) |
| `+XTAMR` | +CME ERROR: operation not supported | +CME ERROR: unknown |
| `+XADPCLKFREQINFO` | +XADPCLKFREQINFO: 0 | +CME ERROR: operation not supported |
| `+XULTHROT` | +XULTHROT: 0,0 | +XULTHROT: (0,1) |
| `+XBCON` | +CME ERROR: operation not supported | +XBCON: (0-1),(0-1),(0-1) |
| `+XBDISC` | +CME ERROR: operation not supported | OK (empty) |
| `+XBATR` | +CME ERROR: operation not supported | OK (empty) |
| `+XBAPDU` | +CME ERROR: operation not supported | OK (empty) |
| `+XBPWR` | +CME ERROR: operation not supported | +XBPWR: (0-2) |
| `+XBCSTAT` | +XBCSTAT: 0, 0 | +XBCSTAT: (0-1) |
| `+XBCRDSTAT` | +CME ERROR: operation not supported | OK (empty) |
| `+XLGTEST` | +CME ERROR: operation not supported | +XLGTEST: (0,1),(1-32),(0-32767) |
| `+XLGINFO` | +CME ERROR: operation not supported | +XLGINFO: (1-7200) |
| `%GPS` | %GPS: 1 0 0 0 0 0.00000 0.00000 0 | %GPS:(S,E,1-100) |
| `+XLCSLSR` | ERROR | +XLCSLSR:(0-2),(0-3), ,(0,1), ,(0,1),(0 -7200),(0-255),(0-1),(0-2),(1-256),(0,1),(0,1) |
| `+XLSRSTOP` | +CME ERROR: operation not supported | OK (empty) |
| `+XLTC` | +CME ERROR: operation not supported | +XLTC:(0-4096),(1-11),0 |
| `+XLOMR` | +CME ERROR: operation not supported | +XLOMR:(0-4096),(1-11) |
| `+XLCSLRMT` | +XLCSLRMT : 1 | +XLCSLRMT : (0-1) |
| `+XLCSLRV` | +CME ERROR: operation not supported | +XLCSLRV : (0,1) |
| `+XLCSLQOS` | +XLCSLQOS : 50, 100, 150, 0, 0, 3, 1 | +XLCSLQOS : (0 -1800000),(0 -990),(0-14400),(0-1966020),(0-2),(0-3),(0-1) |
| `+XLICLS` | +XLICLS: 1 | +XLICLS: (0,1) |
| `+XLCSSLP` | +CME ERROR: operation not supported | +XLCSSLP: (0-3) |
| `+XLICLP` | +CME ERROR: operation not supported | +XLICLP: (0,1),(1-8),(0-65535),(0-127) |
| `+XLCSLUI` | +CME ERROR: operation not supported | +XLCSLUI: |
| `+XLCSTER` | +CME ERROR: operation not supported | +CME ERROR: operation not supported |
| `+XLGASSIST` | ERROR | +CME ERROR: operation not supported |
| `+XLGNVRAM` | +CME ERROR: operation not supported | +XLGNVRAM: (1-255),(0,1,2,3,4) |
| `+XLCSSHUTDOWN` | +CME ERROR: operation not supported | +XLCSSHUTDOWN |
| `%XLCSTEST` | +XLCSTEST: 0,0,"","","" | +XLCSTEST: (0,1),(1-20) |
| `+XLCSSUPLVER` | +XLCSSUPLVER: 2 | +XLCSSUPLVER: (0-2) |
| `+XLCSLSTR` | no answer (failed twice) | +XLCSLSTR:(0,1,255),(0-2),(0,1),(1-8639999),(1-8639999),(0-2678400),(0-127),(0-2),(1-256),(0,1), (0,1) |
| `+XLCSSUPLAPPID` | +XLCSSUPLAPPID:"","","" | +XLCSSUPLAPPID: "APP PROVIDER - Max Len (1-24)","APP NAME - Max Len (1 - 32)","APP VERSION - Max Len (0 - 8)" |
| `+XLCSAETTA` | no answer (failed twice) | +XLCSAETTA:(0,1),(0-3),<CO-ORIDNATES OF SHAPE> |
| `+XLCSTTTPLR` | ERROR | +XLCSTTTPLR:(0,1),(0-3),(0),,(0-1),(0-127),(1-8639999),(1-8639999),(0-2678400) |
| `+XLCSTPLR` | +CME ERROR: operation not supported | +XLCSTPLR:(0-1),,(0,1) |
| `+XLCSSWITCH` | +XLCSSWITCH : 0, 0 | +XLCSSWITCH: (0-1), (0-2) |
| `+XLCSINIT` | +CME ERROR: operation not supported | +XLCSINIT: (INITIALIZE GNSS) |
| `+XLCSSTOREAPN` | +XLCSAPNNAME:"" | +XLCSSTOREAPN: (APN Name) |
| `+XLGTSR` | +XLGTSR: ,,,,, | OK (empty) |
| `+CPOS` | +CME ERROR: operation not supported | OK (empty) |
| `+CPOSR` | +CPOSR: 0 | +CPOSR: (0,1) |
| `+CMOLR` | +CMOLR: 0,0,0,,0,0,,0,0,,0,0,0 | +CMOLR: (0,2),(0-3),(0,1),(0-127),(0,1),(0,1),(0-127),(0-4),(0,1),(),(1-65535),(1,2,4,8,16,32,64),(0,1) |
| `+CMTLR` | +CMTLR : 0 | +CMTLR : (0-3) |
| `+CMTLRA` | +CMTLRA : 1, 0 | +CMTLRA : (0-1) |
| `+XCPOSR` | +XCPOSR: 0,0 | +XCPOSR: (0,1),(0,1) |
| `+XLCAPS` | +XLCAPS: 1,1,1,1,0,1,1,1,1,1,16,16,16,1 | +XLCAPS: (0-1),(0-1),(0-1),(0-1),(0-1),(0-1),(0-1),(0-1),(0-1),(0-1),(0-31),(0-31),(0-31),(0-7) |
| `+CMOLRE` | +CMOLRE: 0 | +CMOLRE: (0-1) |
| `+XCLA` | +CME ERROR: operation not supported | +CME ERROR: operation not supported |
| `+XCLAR` | +XCLAR:0 | +XCLAR:(0-1) |
| `+CPBS` | +CME ERROR: SIM BUSY | +CPBS: ("SM","FD","ON","EN","BL") |
| `+CPBW` | +CPBW: -1 | +CME ERROR: SIM BUSY |
| `+CPBF` | +CME ERROR: operation not supported | not queried |
| `+XCPBW` | +CME ERROR: operation not supported | +CME ERROR: SIM BUSY |
| `+XCPBR` | +CME ERROR: operation not supported | +CME ERROR: SIM BUSY |
| `+STKPROF` | +STKPROF: 62,"FFFFEFFF7F9100FFBF<num>CB000000E24F015700003F7F0800" | OK (empty) |
| `+SATR` | +CME ERROR: operation not supported | +CME ERROR: operation not supported |
| `+SATE` | +CME ERROR: operation not supported | +CME ERROR: operation not supported |
| `+STKCTRLIND` | +CME ERROR: operation not supported | +STKCTRLIND: (0-3) |
| `+SATD` | +CME ERROR: operation not supported | +SATD: (0,1) |
| `+XUSATS` | +XUSATS: 0 | +XUSATS: (0,1) |
| `+XSTKCFG` | +XSTKCFG: 0,1 | +XSTKCFG: (0,1),(0,1) |
| `+XATTMODE` | +XATTMODE: 0 | +XATTMODE: (0-1) |
| `+XSETCAUSE` | +XSETCAUSE: 0,16 | +XSETCAUSE: (0,1),(1,3,6,8,16-19,21,22,25-31,34,38,41-44,47,49,50,55,57,58,63,65,68-70,79,81,87,88,91,95-102,111,127) |
| `+XSPEECHINFO` | +XSPEECHINFO: 0 | +XSPEECHINFO: (0,1) |
| `+XUCCI` | +XUCCI: 0,0,0,1,1,0,0 | +XUCCI: (0-2) |
| `+CGPIAF` | +CGPIAF: 0,0,0,0 | +CGPIAF: (0-1),(0-1),(0-1),(0-1) |
| `+XSDT` | +XSDT: 0 | +XSDT: (0-3) |
| `+XRFCBT` | +XRFCBT: 0,0,0 | OK (empty) |
| `+XTCPFLTR` | +CME ERROR: operation not supported | +XTCPFLTR:(0-4),<packet_type>,<channel_id>[,<no_of_individual_ports>[,<port_1>,<port_2>...[,<no_of_port_ranges>[,<port_range_1_start>,<port_range_1_end>,<port_r … |
| `+CSNS` | +CSNS: 0 | +CSNS: (0,4) |
| `+XCAB` | +XCAB: 0 | +XCAB: (0,1) |
| `+XLEC` | +XLEC: 0,2,5,3 | +XLEC: (0,1) |
| `+XCCINFO` | (redacted) | +XCCINFO: (0,1) |
| `+XNVMCFG` | +CME ERROR: operation not supported | +XNVMCFG: (0,1),(0) |
| `+XHSLP` | +CME ERROR: operation not supported | OK (empty) |
| `+RSRP` | +RSRP: 355,1300,-105.00,391,1300,-108.00 | OK (empty) |
| `+RSRQ` | +RSRQ: 355,1300,-10.50,391,1300,-13.00,120,3050,-7.50 | OK (empty) |
| `+RSCP` | OK (empty) | OK (empty) |
| `+ECNO` | OK (empty) | OK (empty) |
| `+XIPCONFIG` | not queried | +CME ERROR: operation not supported |
| `+CSQ` | +CSQ: 99,99 | +CSQ: (0-31,99),(0-7,99) |
| `+CFSN` | (redacted) | +CME ERROR: operation not supported |
| `+GFSN` | +CME ERROR: operation not supported | +CME ERROR: operation not supported |
| `+FMR` | +FMR: "18300.1002.00.01.01.25" | +CME ERROR: operation not supported |
| `+FMM` | +FMM: "L830 LTE Module","L830" | +CME ERROR: operation not supported |
| `+GMI` | +GMI: "Fibocom" | +CME ERROR: operation not supported |
| `+CGMI` | +CGMI: "Fibocom" | +CME ERROR: operation not supported |
| `+GMM` | +GMM: "L830 LTE Module","L830" | +CME ERROR: operation not supported |
| `+CGMM` | +CGMM: "L830 LTE Module","L830" | +CME ERROR: operation not supported |
| `+GMR` | +GMR: "18300.1002.00.01.01.25" | +CME ERROR: operation not supported |
| `+CGMR` | +CGMR: "18300.1002.00.01.01.25" | +CME ERROR: operation not supported |
| `+CGSN` | (redacted) | +CME ERROR: operation not supported |
| `+GSN` | (redacted) | +CME ERROR: operation not supported |
| `+MSTART` | not queried | +MSTART: (0-1,0-1) |
| `+TGPIO` | +CME ERROR: unknown | +TGPIO: (0,1,4-6,8,12,23,24,40,42-44,49,71,86,118,119,121,122),(0-3) |
| `+CBC` | +CME ERROR: operation not supported | +CME ERROR: operation not supported |
| `+MMAD` | +MMAD: 1,528<br>+CME ERROR: operation not supported | +CME ERROR: operation not supported |
| `+MTSM` | +MTSM: 0 | +MTSM: (0-3,6-7),(1-255),(-40-125) |
| `+FMI` | +FMI: "Fibocom" | +CME ERROR: operation not supported |
| `+GTUSBMODE` | +GTUSBMODE: 1 | +GTUSBMODE: (0-3) |
| `+GTRAT` | +GTRAT: 4,3 | +GTRAT: (0-6),(0,2,3),(0,2,3) |
| `+MSMPD` | +MSMPD: 1 | +MSMPD: (0,1) |
| `+GTSET` | +GTSET: "DIGITPLAY",0<br>+GTSET: "SIMREPO",1<br>+GTSET: "SIMPHASE",1<br>+GTSET: "SWCOREDUMP",0<br>+GTSET: "USBAROUND",2,15<br>+GTSET: "USBAROUNDINFO",0 | +GTSET: "Name",P1<,P2> |
| `+CLK26M` | +CLK26M: 0 | +CLK26M: (0,1) |
| `+GTFMODE` | +GTFMODE: 0 | +GTFMODE: (0-1)<br>+SIM READY |
| `+GTWAKE` | +GTWAKE: 0 | +GTWAKE: (0-1) |
| `+GTMULTIOS` | +GTMULTIOS: 0 | +GTMULTIOS: (0-1) |
| `+GTERASEDYNNVM` | not queried | +GTERASEDYNNVM: (0-1) |
| `+GTDYNNVMERASECNT` | +GTDYNNVMERASECNT: 0 | +CME ERROR: operation not supported |
| `+BODYSAR` | +BODYSAR:<br>1,200,"0x1"<br>1,200,"0x10"<br>1,200,"0x80"<br>2,200,"0x1"<br>2,200,"0x4"<br>2,200,"0x10"<br>2,200,"0x40"<br>2,200,"0x80"<br>2,200,"0x80000"<br>2,200,"0x8000000"<br>2,200,"0x80000000" | +BODYSAR:1,(150-230),"0x91"<br>+BODYSAR:2,(150-230),"0x880800d5" |
| `+BODYSARST` | +BODYSARST:1 | +CME ERROR: operation not supported |
| `+BODYSARCLEAR` | +CME ERROR: operation not supported | +CME ERROR: operation not supported |
| `+BODYSARON` | +BODYSARON:0 | +BODYSARON:(0,1) |
| `+GTPWROFFTH` | +GTPWROFFTH: 120000,110000,120000,120000 | +GTPWROFFTH: <TPMU>,<TDBBCPU>,<TDBBSPCU>,<TSMARTI> |
| `+GTPWRSLITH` | +GTPWRSLITH: 120000,95000,120000,120000 | +GTPWROFFTH: <TPMU>,<TDBBCPU>,<TDBBSPCU>,<TSMARTI> |
| `+GTPWRSIGTH` | +GTPWRSIGTH: 120000,105000,120000,120000 | +GTPWROFFTH: <TPMU>,<TDBBCPU>,<TDBBSPCU>,<TSMARTI> |
| `+MAI2SY` | +MAI2SY: 0,0,0,0 | +MAI2SY: (0,1),(0,1),(0,1,2,3,4,5,6),(0,1,2) |
| `+MMICG` | +MMICG: 10 | +MMICG: (0-15) |
| `+MAPATH` | +CME ERROR: operation not supported | +CME ERROR: operation not supported |
| `+CLVL` | +CLVL: 4 | +CLVL: (0-7) |
| `+MATONE` | +MATONE: 0,0 | +MATONE: (0,1),(0-28),(0-32767),(1-7) |
| `+CMUT` | +CMUT: 0 | +CMUT: (0-3) |
| `+MAVOL` | +MAVOL: 1,1,4<br>+MAVOL: 1,2,4<br>+MAVOL: 1,4,4 | +MAVOL: (1-3),(1-7),(0-7) |
| `+CRSL` | +CRSL: 4 | +CRSL: (0-7) |
| `+VTA` | +VTA: 1 | +VTA: (0,1) |
| `+VTS` | +CME ERROR: operation not supported | +VTS: (0-9,#,*,A-D),(1-10) |
| `+CALM` | +CALM: 1,1 | +CALM: (0,1),(0,1) |
| `+GTCODECN` | +CME ERROR: operation not supported | +CME ERROR: operation not supported |
| `+GTAUDMODE` | +GTAUDMODE: 0 | +GTAUDMODE: (1-5) |
| `+PCMMODE` | +PCMMODE: 0 | +PCMMODE: (0-3) |
| `+GTPCMCFG` | +GTPCMCFG:<br>0: 1,1,0,2,0,0,0,0,0,1<br>1: 1,1,0,2,0,0,3,0,0,0,0,1 | +GTPCMCFG:<br>0: (0-1),(0-1),(0-1),(0-3),(0-7),(0-1),(0-15),(0-1),(0-1),(0-1)<br>1: (0-1),(0-1),(0-1),(0-3),(0-7),(0-1),(0-3),(0-1),(0-1),(0-1),(0-1),(0-1) |
| `+GTI2SCFG` | +GTI2SCFG:<br>0: 1,1,0,2,0,0,0,0,0,0<br>1: 0,1,0,2,0,0,3,0,0,0,0,0 | +GTI2SCFG:<br>0: (0-1),(0-1),(0-1),(0-3),(0-7),(0-1),(0-15),(0-1),(0-1),(0-1)<br>1: (0-1),(0-1),(0-1),(0-3),(0-7),(0-1),(0-3),(0-1),(0-1),(0-1),(0-1),(0-1) |
| `+GTAUDGAIN` | +CME ERROR: operation not supported | +GTAUDGAIN: (0-26),(-960-960) |
| `+GTAUDOFFSET` | +CME ERROR: operation not supported | +GTAUDOFFSET: (0-26),(-960-960) |
| `+GTAUDI2SYRX` | +GTAUDI2SYRX: 0,4,0,1,0,10,65535,65535,10,65535,35,65535,65535,65535,65535 | +GTAUDI2SYRX: (0-9),(0-255),(0-255),(0-65535),(0-65535),(0-65535),(0-65535),(0-65535),(0-65535),(0-65535),(0-65535),(0-65535),(0-65535),(0-65535),(0-65535) |
| `+GTAUDI2SYTX` | +GTAUDI2SYTX: 0,255,3,65535,1,0 | +GTAUDI2SYTX: (0-9),(0-255),(0-255),(0-65535),(0-65535),(0-65535) |
| `+GTAUDSPCHRX` | +GTAUDSPCHRX: 0,0,2,0,1,0,0,65535,65535,65535,65535,65535,65535,10,65535,9,65535,65535,1,155,65535,65535,0,65535,65535,65535,65535,65535,1,65535,65535 | +GTAUDSPCHRX: (0-19),(0-255),(0-255),(0-65535),(0-65535),(0-65535),(0-65535),(0-65535),(0-65535),(0-65535),(0-65535),(0-65535),(0-65535),(0-65535),(0-65535),(0- … |
| `+GTAUDSPCHTX` | +GTAUDSPCHTX: 0,3,0,1,0,65535,0,65535,65535,65535,65535,0,0,0,65535,65535,65535,65535,65535,0,65535,0,65535,65535,65535,65535,65535,65535,65535,65535,65535,65535,65535 | +GTAUDSPCHTX: (0-19),(0-255),(0-255),(0-65535),(0-65535),(0-65535),(0-65535),(0-65535),(0-65535),(0-65535),(0-65535),(0-65535),(0-65535),(0-65535),(0-65535),(0- … |
| `+GTAUDEDL` | +GTAUDEDL: 0,0 | +GTAUDEDL: (0-19),(0-65535) |
| `+GTAUDAEC` | +GTAUDAEC: 0,100,100,100,2,2,2,1,65527,4,24576,32767,31784,65531,65528,65531,65525,50,65522,0,2,65528,4096,65534,63,40,40 | +GTAUDAEC: (0-19),(0-65535),(0-65535),(0-65535),(0-65535),(0-65535),(0-65535),(0-65535),(0-65535),(0-65535),(0-65535),(0-65535),(0-65535),(0-65535),(0-65535),(0 … |
| `+GTAUDAGC` | +GTAUDAGC: 0,0,0,5000,65528,65535,65535,65531,65535,0,6144,12288,256,282,307,7,2,0,65532,65535,65535,768,39704,39210,37722,35247,21379,24441,29532,4000,8192 | +GTAUDAGC: (0-19),(0-65535),(0-65535),(0-65535),(0-65535),(0-65535),(0-65535),(0-65535),(0-65535),(0-65535),(0-65535),(0-65535),(0-65535),(0-65535),(0-65535),(0 … |
| `+GTAUDSER` | +GTAUDSER: 0,32113,2048,2560,6554,29491,4096,8290,1,3 | +GTAUDSER: (0-9),(0-65535),(0-65535),(0-65535),(0-65535),(0-65535),(0-65535),(0-65535),(0-65535),(0-65535) |
| `+GTAUDCMPR` | +GTAUDCMPR: 0,0,1,4,8192,6,10240,8192,5120,3072,50176,61696,768,1280,256,64,1,20,1638,5,28316,3850,20713,1598,54016,59392,0,768,256,256 | +GTAUDCMPR: (0-39),(0-1),(0-65535),(0-65535),(0-65535),(0-65535),(0-65535),(0-65535),(0-65535),(0-65535),(0-65535),(0-65535),(0-65535),(0-65535),(0-65535),(0-65 … |
| `+GTAUDFIR` | +GTAUDFIR: 0,1,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,32767,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0 | OK (empty) |
| `+GTAUDIIR` | +GTAUDIIR: 0,8,22737,20650,22737,17027,22737,36334,36725,28989,25718,29424,0,0,0,0,32767,0,0,0,0,32767 | +GTAUDIIR: (0-49),(0-65535),(0-65535),(0-65535),(0-65535),(0-65535),(0-65535),(0-65535),(0-65535),(0-65535),(0-65535),(0-65535),(0-65535),(0-65535),(0-65535),(0 … |
| `+GTAUDULNR` | +GTAUDULNR: 0,6000,8000,8000,65534,65532,800,32767,768,1664,3072,0,1,32,32767,1024,1000,19660,1 | +GTAUDULNR: (0-19),(0-65535),(0-65535),(0-65535),(0-65535),(0-65535),(0-65535),(0-65535),(0-65535),(0-65535),(0-65535),(0-65535),(0-65535),(0-65535),(0-65535),( … |
| `+GTAUDDLNR` | +GTAUDDLNR: 0,6000,8000,8000,65534,65532,800,32767,768,1664,3072,0,1,32,32767,1024,1000,19660,1 | +GTAUDDLNR: (0-9),(0-65535),(0-65535),(0-65535),(0-65535),(0-65535),(0-65535),(0-65535),(0-65535),(0-65535),(0-65535),(0-65535),(0-65535),(0-65535),(0-65535),(0 … |
| `+TPIN` | +TPIN: 3,10,3,10 | +CME ERROR: operation not supported |
| `+GTFIRMWARE` | (redacted) | +GTFIRMWARE=<"firmware ID"> |
| `+GTUSBWALOG` | +CME ERROR: operation not supported | +CME ERROR: operation not supported |
| `+XSTRESSSIM` | not queried | +XSTRESSSIM: (0-1) |
| `+XCELLINFO` | (redacted) | OK (empty) |
| `+TRACE` | OK (empty) | OK (empty) |
| `+XSATK` | +XSATK: 0,0 | +XSATK: ( 0-1),(0-1) |
| `+XCOLP` | +XCOLP: 0 | +XCOLP: (0,1) |
| `+XNITZINFO` | +XNITZINFO: 0 | +XNITZINFO: (0-1) |
| `+XCALLNBMMI` | +XCALLNBMMI: 0 | +XCALLNBMMI: 0-1 |
| `+XRLCSET` | +CME ERROR: operation not supported | OK (empty) |
