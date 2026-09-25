# Compatibility and risks

What's actually confirmed about running the FM350-GL in the Waveshare adapter and on GL.iNet/OpenWrt firmware, what's a single community report, what's still unknown, and the fallback if any of it doesn't work out. Read this before you buy the hardware or file a bug.

Legend: confirmed = primary source or reproduced report; reported = single community report; unknown = no evidence found.

## 1. FM350-GL in the Waveshare USB TO M.2 B KEY

| Claim | Status | Evidence |
|---|---|---|
| Waveshare says FM350 is unsupported | confirmed | RM520N-GL wiki: *"The FM350 series is a custom module for a certain brand's computer and does not support it either."* Not in the adapter's tested list. |
| FM350-GL works in this exact adapter | reported | [OpenMPTCProuter #3421](https://github.com/Ysurac/openmptcprouter/issues/3421): *"In OpenWRT, it worked with the same protocol (fm350-modem and luci-proto-fm350) and without an auxiliary power supply."* (Raspberry Pi 4) |
| FM350 carries data over USB | confirmed | Linux `option` patch by Bjørn Mork (2024): USB is "fully functional" despite being labelled debug; OpenWrt forum users report ~1.2 Gbit/s over USB |
| Pin conflicts on the M.2 slot | unknown | The wiki asks users to check pins 8 (W_DISABLE#), 23, 26 (GNSS disable), 67 (RESET#) for unlisted modules. Laptop FM350 variants expect W_DISABLE# high. Try `AT+GTFMODE?` if the radio stays off. |

If it doesn't work: a Quectel RM520N-GL is officially supported by the adapter and is in GL.iNet's stock modem list (`2c7c:0801`). It's the lowest-risk replacement.

## 2. FCC lock (checked 2026-09-25: this unit is not locked, `+GTFCCEFFSTATUS: 0,1`)

- FM350-GL modules sold loose are usually pulled from Lenovo or Dell laptops (for example Intel "5G Solution 5000", Dell DW5931e). They are **FCC-locked**: the radio stays off and `AT+CFUN=1` returns `ERROR` until the host unlocks it.
- Check: `AT+GTFCCEFFSTATUS?` returns `<mode>,<status>`. Mode 0 = no lock, 1 = one-time unlock, 2 = unlock needed at every power-up. Status 1 = unlocked.
- Unlock: `AT+GTFCCLOCKGEN` returns a challenge. The response is the first 4 bytes of `SHA-256(challenge ‖ vendor_hash)`, sent with `AT+GTFCCLOCKVER=<n>`. The vendor hash is `3df8c719` (Lenovo and others) or `4909b5a4` (Dell DW5931e).
- Ready-made script: [`fm350_fcc_unlock.sh`](https://github.com/mrhaav/openwrt/blob/master/atc/fib-fm350_gl/fm350_fcc_unlock.sh) (needs `xxd`, `comgt`). With mode 2 the script also sends `AT+GTFCCLOCKMODE=0` so the unlock persists.
- ModemManager's `dispatcher-fcc-unlock/14c3` script uses the same algorithm.

## 3. GL.iNet firmware support

| Firmware | Kernel | FM350 status |
|---|---|---|
| Stock 4.9.x (OpenWrt 21.02) | 5.4 | Not in the cellular allowlist (`/lib/modem_data/modem_list.json`, 16 entries: Quectel, Huawei and a few generic). The RNDIS network device appears (eth2 / usb0), but the serial ports need `echo "0e8d 7127 ff" > /sys/bus/usb-serial/drivers/option1/new_id`. The GL cellular manager logs `No 'b2_1_p7127_v0e8d' found in slot_feature config` ([gl-modem-community #95](https://github.com/rudironsoni/gl-modem-community/issues/95)). |
| 4.9.0-op24 (OpenWrt 24.10) | 6.6 | The kernel recognises the FM350 natively. The GL UI still lacks FM350 support. Community package `gl-modem-community` adds definitions; data sessions are unverified there. |
| op25 (OpenWrt 25.12) | 6.12 | `gl-modem-community` confirmed detection and UI visibility on GL-MT3000. Full data session not yet proven. |
| Vanilla OpenWrt 24.10 / 25.12 | 6.6 / 6.12 | Works with `xmm-modem` (modemfeed) or `atc-fib-fm350_gl` (mrhaav). This is the most-used path. |

GL.iNet staff: *"we can't guarantee compatibility with every third-party model"*. The stock firmware has QMI/MBIM drivers and no ModemManager. That doesn't matter here, because the FM350 in USB mode uses neither QMI nor MBIM.

## 4. Data connection quirks (USB/RNDIS)

- DHCP on the RNDIS interface is unreliable (reported on the OpenWrt forum). The working scripts do this instead:
  1. `AT+CGDCONT=1,"IP","<apn>"`, then `AT+CGACT=1,1`
  2. `AT+CGPADDR=1` returns the IP, which is set as a static `/24` on the RNDIS interface
  3. `ip link set <if> arp off`; default route through the device
  4. `AT+GTDNS=1` returns the DNS servers
- After a network drop the FM350 gives few signals the host can react to. mrhaav's script uses the NITZ indication `+CTZV` to trigger a reconnect. A hotplug helper (`60-fm350_crash`) re-runs `ifup` when the modem re-enumerates after a crash.
- Band and RAT control use Fibocom `GT` commands (`AT+GTACT`), not Quectel `QCFG` or `QNWPREFCFG`, so any Quectel-oriented UI will not work for these settings.

## 5. Power and thermal

- Flint 2 USB supplies 5 V / 2 A. The reporter in #3421 ran without auxiliary power on a Raspberry Pi, but use the auxiliary plug anyway to avoid brown-out resets during attach and uplink bursts.
- Typical symptom of too little power: the modem disappears from `lsusb` or re-enumerates in a loop, often logged as a "crash".

## 6. Firmware on the module

The module firmware can be updated with SP Flash Tool on Windows from Microsoft Update catalog packages (for example 29.23.06). This can brick the module. **Only do it if you hit a bug that newer firmware fixes.** Record the current version (`AT+CGMR`) first.

## Known behaviour of the `atc` protocol handler (tested against a fake modem, 2026-09-25)

Found by `openwrt/tests/atc-test.sh` (atc-fib-fm350_gl 2025.08.24-r3):

- No SIM: fails fast and cleanly (`SIM not inserted`, restart blocked). It does not loop.
- `AT+CGACT` errors: only `+CME ERROR: Requested service option not subscribed (#33)` is treated as fatal. Any other `+CME ERROR` during activation is ignored, and the handler then waits forever for URCs that never arrive. `wwan` stays "connecting" and never goes online in mwan3, so failover would silently be unavailable. If this happens on the bench, `ifup wwan` restarts it. A watchdog is a possible later fix.
- Its own AT-readiness retry loop (`while [ $atOut != 'OK' ]`, unquoted) gives up after one attempt. This is harmless because `gcom` already waits up to 25 s per command.
- `atc_debug` unset prints `sh: out of range` to stderr. This is cosmetic.

## Open questions to answer on the bench

1. Does the module enumerate on the Flint 2 (`lsusb` shows `0e8d:7127`)? On a Mac: yes, 5 Gbps.
2. FCC lock: not locked (`0,1`), confirmed on the bench.
3. Does `AT+CFUN=1` and then `AT+COPS?` register on the carrier with the SIM in the Waveshare slot? Yes, confirmed on the bench: registered on Vodafone DE (`+COPS: 0,2,"26202",13`, LTE band 1), once a defective pair of antenna pigtails was replaced (see [bench-log.md](bench-log.md)). Still open: whether the module registers identically once it's plugged into the Flint 2 itself rather than a Mac.
4. Does the modem stay up under a sustained speed test with and without auxiliary power? Still open; needs a data SIM.
5. On GL firmware: does multi-WAN failover accept the interface? On vanilla OpenWrt: `mwan3` correctly fails traffic over to `wwan` and back in our QEMU test (see [openwrt/README.md](../openwrt/README.md)), but that test uses a DHCP stand-in for the modem interface, not the real FM350. GL firmware's multi-WAN behaviour with a custom-protocol interface is still untested.

One user in the OpenWrt thread reports (post [#429](https://forum.openwrt.org/t/fibocom-fm350-gl-support/142682), checked 2026-09-25) that `AT+CGCONTRDP` crashes the FM350. mrhaav's `atc.sh` sends `AT+CGCONTRDP=1` after every connect. Others have run it without problems, so on the router check for USB re-enumerations right after connecting.
