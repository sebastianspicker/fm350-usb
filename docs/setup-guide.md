# Setup guide

Step-by-step instructions for wiring up the Fibocom FM350-GL as an OpenWrt WAN interface and putting mwan3 in front of it for failover. Written for the GL.iNet Flint 2 (GL-MT6000) we used, but steps 5 onward should apply to any OpenWrt 24.10+ router with a USB port.

## In short

- Nine steps take you from assembling the dongle to mwan3 failover on OpenWrt: assemble → check enumeration → identify the module and check the FCC lock → register on the network → choose firmware → install a protocol handler → set up mwan3 → (optional) keep GL.iNet firmware → tune.
- Step 4 is the go/no-go point: if `AT+CGPADDR=1` returns an IP address, the hardware combination works and what's left is software integration.
- The fast path is [`openwrt/install.sh`](../openwrt/install.sh), which automates steps 6b and 7; the manual instructions further down are for reference, or if you'd rather do it by hand.
- The biggest caveat: steps 2–4 have been run only with the module connected to a Mac, not on the Flint 2 itself. Nobody has completed a real data session yet on any host — see the table below.

## Where each step has been tested

| Steps | What was checked | Tested on | Status |
|---|---|---|---|
| 2–4 (USB enumeration, AT access, the FCC-lock check, SIM detection, LTE registration) | The commands in this guide | The module connected to a Mac | Verified on the bench [Bench log]. The router-side commands should give the same results, but we haven't run them on the Flint 2 itself yet. |
| 4, the data-session block (`AT+CGDCONT`/`AT+CGACT`/`AT+CGPADDR`) | Whether a PDP context comes up | — | **Not tested on any host.** We're waiting on a data SIM. |
| 6–7 (`install.sh`/`uninstall.sh`, mwan3 failover) | Install, uninstall, and failover/failback | Docker, a pty modem emulator, and QEMU | Verified in emulation [openwrt README]. Not yet run on the Flint 2. |

Track your own results as you go through the steps below.

## 0. Before you start

- [ ] Check that the SIM works in a phone (data, PIN known or disabled).
- [ ] Note the carrier APN (e.g. Telekom `internet.telekom`, Vodafone `web.vodafone.de`, O2 `internet`).
- [ ] Back up the Flint 2 config (GL admin panel: System, then Backup/Restore) before you change firmware.
- [ ] Have a separate 5 V ≥ 2 A USB charger for the auxiliary power plug.

See [Hardware](hardware.md#power-budget) for why the auxiliary power plug matters.

## 1. Assemble the dongle

1. Open the Waveshare case. Insert the FM350-GL into the M.2 slot and fix it with the screw.
2. Put the included thermal pad between the module and the aluminium lid.
3. Connect the 4 IPEX-4 pigtails to the module's antenna pads (MAIN/AUX/MIMO; all four are needed for 4×4). Press straight down until they click.
4. Insert the nano-SIM. Close the case and attach the 4 antennas.
5. Connect the dual-plug cable: the data+power plug goes into the Flint 2 USB 3.0 port, and the power-only plug goes into the 5 V charger.

Expected: the power LED is on. The network LED starts blinking once the modem registers (it can't register while FCC-locked).

## 2. Check that the modem enumerates (any firmware)

SSH into the router (`ssh root@192.168.8.1` on GL firmware).

```sh
opkg update && opkg install usbutils picocom comgt   # on apk-based firmware: apk add ...
lsusb | grep -i 0e8d          # expect 0e8d:7127 (or 7126)
dmesg | grep -iE 'rndis|usb .*0e8d|ttyUSB'
ip link                       # a new ethX / usbX interface = RNDIS
```

If `lsusb` shows nothing, check power (auxiliary plug) and reseat the module. If the device appears and disappears repeatedly, the power supply is too weak.

### Kernel 5.4 only (GL stock 4.9.x): bind the serial driver by hand

```sh
echo "0e8d 7127 ff" > /sys/bus/usb-serial/drivers/option1/new_id   # 7126 if in mode 40
ls /dev/ttyUSB*
```

Kernel 6.6+ (vanilla OpenWrt 24.10+, GL op24/op25) binds it automatically.

### Find the AT port

The AT port is USB interface 6 in mode 41 (`0e8d:7127`) and interface 4 in mode 40 (`0e8d:7126`). Map the interface to a tty:

```sh
ls -d /sys/bus/usb/devices/*:1.6/ttyUSB*   # mode 41
ls -d /sys/bus/usb/devices/*:1.4/ttyUSB*   # mode 40
```

Typically this is `/dev/ttyUSB4` in mode 41. Open it:

```sh
picocom -b 115200 --echo /dev/ttyUSB4    # exit: Ctrl-A Ctrl-X
```

## 3. Identify the module and check the FCC lock

```text
ATI
AT+CGMR                 # record firmware version
AT+GTUSBMODE?           # 41 = default
AT+GTFCCEFFSTATUS?      # <mode>,<status>; mode 0 = no lock
AT+CFUN?                # 1 = radio on
```

If `AT+GTFCCEFFSTATUS?` reports mode 1 or 2 and `AT+CFUN=1` returns `ERROR`, run the unlock script:

```sh
opkg install xxd
wget -O /root/fm350_fcc_unlock.sh https://raw.githubusercontent.com/mrhaav/openwrt/master/atc/fib-fm350_gl/fm350_fcc_unlock.sh
sh /root/fm350_fcc_unlock.sh /dev/ttyUSB4
```

The script uses vendor hash `3df8c719` (Lenovo). For a Dell DW5931e module, change `VENDOR_ID_HASH` to `4909b5a4`. Close picocom first, because only one program can use the port at a time.

**A note on this check, because two different readings are used across this repo's docs:** this guide's test above (mode 1 or 2, plus `AT+CFUN=1` returning `ERROR`) and the test used in [Compatibility](compatibility-and-risks.md) and the diagnostics tool look at the same response but read it differently. The first value (`<mode>`) is the *type* of lock (0 none, 1 one-time, 2 every power-up); the second value (`<status>`) is whether the radio is *unlocked right now*. If you just want a yes/no answer to "is it locked?", check the second value — status `1` means unlocked, and that's the test Compatibility and the diagnostics tool use. Our own module read `0,1`: mode 0 (no lock configured) and status 1 (unlocked) [Bench log]. See [Compatibility](compatibility-and-risks.md) for the full background on the FCC lock and the unlock algorithm.

## 4. Register on the network

```text
AT+CPIN?                          # READY
AT+CFUN=1
AT+COPS?                          # operator
AT+CEREG?  /  AT+C5GREG?          # ,1 or ,5 = registered
AT+CESQ                           # signal
AT+GTCCINFO?                      # serving cell (LTE/NR, band, RSRP)
```

Test a data session by hand:

```text
AT+CGDCONT=1,"IP","internet.telekom"
AT+CGACT=1,1
AT+CGPADDR=1                      # e.g. +CGPADDR: 1,"10.123.45.67"
AT+GTDNS=1
```

This is the go/no-go point: if you get an IP address here, the hardware combination works, and what's left is software integration.

## 5. Choose firmware

**A note on this section:** earlier text here pointed to "the options table in the README" — the README doesn't have one. The options are defined in this guide: **option A** is vanilla OpenWrt, covered by the rest of this guide; **options B/C** keep GL.iNet's own firmware and are covered in [§8 below](#8-option-bc-notes-keep-glinet-firmware).

1. Download the `glinet_gl-mt6000` sysupgrade image for the current stable release (24.10.x or 25.12.x) from `firmware-selector.openwrt.org`.
2. Follow the install notes on the OpenWrt wiki page for the GL-MT6000. It can be flashed from the GL admin panel (local upgrade, do **not** keep settings) or through the U-Boot recovery web UI (hold reset while powering on, then open 192.168.1.1).
3. The U-Boot recovery also lets you go back to GL stock firmware.

## 6. Install the FM350 protocol handler (vanilla OpenWrt)

> [`openwrt/install.sh`](../openwrt/install.sh) automates steps 6b and 7. Copy `openwrt/` to the router and run `sh install.sh --apn <apn>` (try `--dry-run` first). It merges with mwan3's stock config instead of overwriting it, and `uninstall.sh` reverts everything. It's tested in an OpenWrt 24.10 Docker rootfs, a pty modem emulator, and under QEMU; see [openwrt/README.md](../openwrt/README.md). The manual steps below are for reference, or if you'd rather do it by hand.

Choose one of these handlers.

### 6a. `xmm-modem` + `luci-proto-xmm` (modemfeed, by koshev-msk)

Dependencies: `comgt kmod-usb-acm kmod-usb-serial-option kmod-usb-net-cdc-ncm kmod-usb-net-rndis`. This package isn't in the official OpenWrt feeds. Add the modemfeed repository for your release (see the [modemfeed README](https://github.com/koshev-msk/modemfeed)) or build it with the SDK.

`/etc/config/network`:

```text
config interface 'wwan'
	option proto 'xmm'
	option device '/dev/ttyUSB4'
	option apn 'internet.telekom'
	option pdp 'ip'            # ip | ipv4v6 | ipv6
	option profile '1'
	option auth 'auto'
	option delay '10'
	option metric '20'
```

The protocol script looks up the RNDIS netdev, runs CGDCONT/CGACT, and sets the IP from `CGPADDR` as a /24 with ARP turned off. It also installs the default route and applies the DNS servers from `AT+GTDNS`.

### 6b. `atc-fib-fm350_gl` + `luci-proto-atc` (by mrhaav)

Dependencies: `kmod-usb-serial-option kmod-usb-net-rndis comgt`. This handler reconnects after network drops (using the NITZ `+CTZV` indication), supports dual-stack, receives SMS and can send custom AT commands at start-up.

```sh
cd /tmp
# the exact files install.sh pins (commit 0d56d84); newer releases: https://github.com/mrhaav/openwrt/tree/master/atc
wget https://github.com/mrhaav/openwrt/raw/0d56d844cc49906285c9181a008186f4af515c85/atc/luci-proto-atc_2025.01.10-r2_all.ipk
wget https://github.com/mrhaav/openwrt/raw/0d56d844cc49906285c9181a008186f4af515c85/atc/fib-fm350_gl/atc-fib-fm350_gl_2025.08.24-r3_all.ipk
sha256sum luci-proto-atc_*.ipk atc-fib-fm350_gl_*.ipk   # compare with openwrt/README.md, "Package URLs"
opkg install luci-proto-atc_*.ipk atc-fib-fm350_gl_*.ipk
# 25.12 (apk): use the .apk files and `apk add --allow-untrusted`
```

In LuCI, create an interface with protocol `ATC`, choose the AT port and APN, and assign it to the `wan` firewall zone.

If the modem crashes and re-enumerates, add the helper script:

```sh
wget -O /etc/hotplug.d/usb/60-fm350_crash https://raw.githubusercontent.com/mrhaav/openwrt/master/atc/fib-fm350_gl/60-fm350_crash
```

For IPv6 DNS through Router Advertisements, add a firewall rule that allows ICMPv6 from `fe80::1` on `wan` (see the mrhaav README).

### Common to both

- Add the new interface to the `wan` firewall zone.
- Turn off the handler's own "default route" logic only if mwan3 should own routing. By default, keep the default route and use metrics.

## 7. Failover with mwan3

```sh
opkg install mwan3 luci-app-mwan3
```

Give the interfaces distinct metrics (`wan` = 10, `wwan` = 20), then create `/etc/config/mwan3`:

```text
config globals 'globals'
	option mmx_mask '0x3F00'

config interface 'wan'
	option enabled '1'
	option family 'ipv4'
	list track_ip '1.1.1.1'
	list track_ip '9.9.9.9'
	option reliability '1'
	option interval '5'
	option down '3'
	option up '3'

config interface 'wwan'
	option enabled '1'
	option family 'ipv4'
	list track_ip '1.1.1.1'
	list track_ip '9.9.9.9'
	option reliability '1'
	option interval '10'
	option down '3'
	option up '3'

config member 'wan_m1'
	option interface 'wan'
	option metric '1'

config member 'wwan_m2'
	option interface 'wwan'
	option metric '2'

config policy 'failover'
	list use_member 'wan_m1'
	list use_member 'wwan_m2'
	option last_resort 'unreachable'

config rule 'default'
	option dest_ip '0.0.0.0/0'
	option use_policy 'failover'
```

Notes:
- A longer `interval` on `wwan` reduces the tracking traffic that counts against the mobile data plan.
- Add an IPv6 block if the ISP gives you IPv6.
- Test: `mwan3 status`, then unplug the WAN cable and check that traffic moves to wwan within about 15 s (`curl ifconfig.io` should show the carrier IP). Plug the cable back in and check that traffic returns.

## 8. Option B/C notes (keep GL.iNet firmware)

- On op24 / op25, the kernel already sees the modem. Install the same handler (6a or 6b). Whether GL's multi-WAN (`kmwan`) page will list and track a custom-protocol interface is unverified; if it doesn't, install `mwan3` alongside it, which may conflict with GL's own multi-WAN.
- [`gl-modem-community`](https://github.com/rudironsoni/gl-modem-community) adds FM350 definitions to the GL cellular stack so the modem shows in the GL UI. It has been tested only on GL-MT3000, and its data sessions are not fully validated. Treat it as experimental on the Flint 2.
- On stock 4.9.x (kernel 5.4) you also need the `new_id` binding from step 2 at every boot (a hotplug script like mrhaav's `50-fm350_driver`). This option needs the most fixes.

## 9. Tuning (after it works)

- Band lock or RAT preference: `AT+GTACT=?` lists the options. For example, `AT+GTACT=...` restricts bands. Read the current value with `AT+GTACT?` before you change it.
- Temperature under load: `AT+GTSENRDTEMP=1`.
- Carrier aggregation info: `AT+GTCAINFO?`.
- Antenna placement: move the dongle on its USB extension cable to a window. Compare RSRP/SINR from `AT+GTCCINFO?` between positions.

## Glossary

Terms used on this page, defined in the [shared glossary](glossary.md): [APN](glossary.md#apn), [AT command](glossary.md#at-command), [AT port](glossary.md#at-port), [Failover / failback](glossary.md#failover--failback), [FCC lock](glossary.md#fcc-lock), [mwan3](glossary.md#mwan3), [PDP context / data session](glossary.md#pdp-context--data-session), [Protocol handler](glossary.md#protocol-handler), [QEMU / Docker](glossary.md#qemu--docker), [RAT](glossary.md#rat), [RNDIS](glossary.md#rndis), [RSRP / RSRQ / SINR](glossary.md#rsrp--rsrq--sinr).
