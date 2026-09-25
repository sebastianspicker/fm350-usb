# Sources

Every external reference used elsewhere in this repo — vendor docs and datasheets, kernel patches, forum threads, and community scripts — collected here so you can check them yourself. All retrieved 2026-09-25.

## Vendor

- Waveshare USB TO M.2 B KEY product page: https://www.waveshare.com/usb-to-m.2-b-key.htm?sku=23252
- Waveshare wiki, USB TO M.2 B KEY: https://www.waveshare.com/wiki/USB_TO_M.2_B_KEY
- Waveshare wiki, RM520N-GL (FM350 "not supported" statement): https://www.waveshare.com/wiki/RM520N-GL
- GL.iNet Flint 2 datasheet: https://static.gl-inet.com/www/images/products/datasheet/mt6000_datasheet_20251103.pdf
- GL.iNet Flint 2 docs: https://docs.gl-inet.com/router/en/4/user_guide/gl-mt6000/
- GL.iNet firmware versions: https://www.gl-inet.com/support/firmware-versions/
- Fibocom FM350 AT Commands User Manual V2.10: https://www.minipc.de/support_db/support_files/Fibocom_FM350_AT%20Commands%20User%20Manual_V2.10.pdf
- FM350-GL spec summary: https://www.4gltemall.com/fibocom-fm350-gl.html

## Linux / OpenWrt

- `option` driver patch adding FM350-GL (USB modes, IDs): https://lkml.iu.edu/hypermail/linux/kernel/2406.3/04255.html
- OpenWrt forum, FM350-GL support thread: https://forum.openwrt.org/t/fibocom-fm350-gl-support/142682
- OpenWrt forum, FM350 with ModemManager: https://forum.openwrt.org/t/how-to-use-fibocom-fm350-with-modem-manager/245004
- mrhaav `atc-fib-fm350_gl`, FCC unlock and firmware upgrade notes: https://github.com/mrhaav/openwrt/tree/master/atc/fib-fm350_gl
- koshev-msk modemfeed (`xmm-modem`, `luci-proto-xmm`): https://github.com/koshev-msk/modemfeed
- ModemManager FCC unlock docs: https://modemmanager.org/docs/modemmanager/fcc-unlock/

## GL.iNet-specific community

- GL forum, Flint 2 USB 5G modem support (staff answers): https://forum.gl-inet.com/t/gl-mt6000-flint-2-usb-5g-modem-support-qmi-mbim-modemmanager/66128
- GL forum, MT6000 modem not recognised (PID allowlist): https://forum.gl-inet.com/t/no-modem-mode-in-mt6000/55947
- gl-modem-community (FM350 for GL stack, gap analysis): https://github.com/rudironsoni/gl-modem-community
- gl-modem-community issue #95 (FM350 on stock 4.9.0 not registered): https://github.com/rudironsoni/gl-modem-community/issues/95

## Field reports with this exact adapter

- OpenMPTCProuter #3421: FM350-GL in Waveshare USB TO M.2 B KEY, working on OpenWrt: https://github.com/Ysurac/openmptcprouter/issues/3421
