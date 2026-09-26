# Sources

Every external reference used elsewhere in this repo — vendor docs and datasheets, kernel patches, forum threads, and community scripts — collected here so you can check them yourself. All retrieved 2026-09-25.

## In short

- This page indexes every external link used elsewhere in the repo, grouped by topic, with a one-line note on what each is used for.
- Everything was retrieved on 2026-09-25.
- Two docs keep their own, more detailed source lists alongside this one: the [Dell guide](dell-dw5931e-usb.md#sources) lists the sources specific to that unit, and [firmware-reflash.md](firmware-reflash.md#sources) grades each source by how well-corroborated it is (that procedure is untested and hobbyist-assembled, so the confidence of each source matters more there).

## Vendor

- Waveshare USB TO M.2 B KEY product page: https://www.waveshare.com/usb-to-m.2-b-key.htm?sku=23252 — adapter specs [Hardware].
- Waveshare wiki, USB TO M.2 B KEY: https://www.waveshare.com/wiki/USB_TO_M.2_B_KEY — adapter specs and the vendor's own usage notes [Hardware].
- Waveshare wiki, RM520N-GL (FM350 "not supported" statement): https://www.waveshare.com/wiki/RM520N-GL — Waveshare's statement that the FM350 isn't on its supported list [Compatibility].
- GL.iNet Flint 2 datasheet: https://static.gl-inet.com/www/images/products/datasheet/mt6000_datasheet_20251103.pdf — router specs [Hardware].
- GL.iNet Flint 2 docs: https://docs.gl-inet.com/router/en/4/user_guide/gl-mt6000/ — router firmware and install notes [Hardware, Setup guide].
- GL.iNet firmware versions: https://www.gl-inet.com/support/firmware-versions/ — which GL firmware build to pick [Setup guide].
- Fibocom FM350 AT Commands User Manual V2.10: https://www.minipc.de/support_db/support_files/Fibocom_FM350_AT%20Commands%20User%20Manual_V2.10.pdf — the AT command reference behind [AT commands](at-commands.md), [Hardware](hardware.md) and the [Dell guide](dell-dw5931e-usb.md).
- FM350-GL spec summary: https://www.4gltemall.com/fibocom-fm350-gl.html — modem spec sheet [Hardware].

## Linux / OpenWrt

- `option` driver patch adding FM350-GL (USB modes, IDs): https://lkml.iu.edu/hypermail/linux/kernel/2406.3/04255.html — when Linux gained native support for the FM350's serial ports [Hardware, Setup guide].
- OpenWrt forum, FM350-GL support thread: https://forum.openwrt.org/t/fibocom-fm350-gl-support/142682 — the main community thread; cited throughout [Compatibility, Dell guide, Bench log, Reflash].
- OpenWrt forum, FM350 with ModemManager: https://forum.openwrt.org/t/how-to-use-fibocom-fm350-with-modem-manager/245004 — background on why ModemManager doesn't apply here.
- mrhaav `atc-fib-fm350_gl`, FCC unlock and firmware upgrade notes: https://github.com/mrhaav/openwrt/tree/master/atc/fib-fm350_gl — the `atc` protocol handler and unlock script used in [Setup guide](setup-guide.md) and [Compatibility](compatibility-and-risks.md).
- koshev-msk modemfeed (`xmm-modem`, `luci-proto-xmm`): https://github.com/koshev-msk/modemfeed — the alternative `xmm` protocol handler [Setup guide].
- ModemManager FCC unlock docs: https://modemmanager.org/docs/modemmanager/fcc-unlock/ — background on the unlock algorithm [Compatibility, Dell guide].

## GL.iNet-specific community

- GL forum, Flint 2 USB 5G modem support (staff answers): https://forum.gl-inet.com/t/gl-mt6000-flint-2-usb-5g-modem-support-qmi-mbim-modemmanager/66128 — GL staff's own answers on cellular USB support [Compatibility].
- GL forum, MT6000 modem not recognised (PID allowlist): https://forum.gl-inet.com/t/no-modem-mode-in-mt6000/55947 — why stock GL firmware doesn't see the FM350 [Compatibility].
- gl-modem-community (FM350 for GL stack, gap analysis): https://github.com/rudironsoni/gl-modem-community — the community package that adds FM350 definitions to the GL cellular UI [Setup guide, Compatibility].
- gl-modem-community issue #95 (FM350 on stock 4.9.0 not registered): https://github.com/rudironsoni/gl-modem-community/issues/95 — evidence for the stock-firmware allowlist gap [Compatibility].

## Field reports with this exact adapter

- OpenMPTCProuter #3421: FM350-GL in Waveshare USB TO M.2 B KEY, working on OpenWrt: https://github.com/Ysurac/openmptcprouter/issues/3421 — the one other report of this exact module/adapter pairing working [Compatibility, README].

## macOS RNDIS drivers (background for `fm350mac`)

macOS has no built-in RNDIS driver, which is why this repo carries its own user-space driver (`fm350mac`). These two third-party projects came up while researching that gap; neither has been tried against this module [Bench log]:

- TetherKit (libusb-based RNDIS, no kext): https://github.com/XiaoMiku01/TetherKit
- ReRNDIS (alternative RNDIS driver, macOS 15+): https://github.com/JellyBrick/ReRNDIS

## Glossary

This page is an index of links with one-line usage notes and uses no terms that need defining. All definitions are in the [shared glossary](glossary.md).
