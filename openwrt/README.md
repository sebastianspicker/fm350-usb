# openwrt/ - router-side 5G failover setup

Scripts that automate [docs/setup-guide.md](../docs/setup-guide.md) steps 6-7
(protocol handler + mwan3 failover) for a Fibocom FM350-GL in a USB M.2
dongle on vanilla OpenWrt 24.10+. We built and tested this against a GL.iNet
Flint 2 (GL-MT6000), but the scripts only use plain OpenWrt/mwan3/opkg-or-apk
mechanisms, so they should work on other OpenWrt 24.10+ routers with a USB
port too. Targets busybox ash; no bashisms.

## Files

| File | Purpose |
|---|---|
| `install.sh` | Installs packages, downloads the FM350 protocol handler, and applies the uci config below. Run as root on the router. |
| `uninstall.sh` | Removes the uci sections and files added by `install.sh`. Leaves packages installed. |
| `fm350-status.sh` | Read-only modem status: registration, operator, signal (RSRP/RSRQ/SINR), serving cell and band, temperature, FCC-lock state. `-r` prints the raw AT responses, `-x` masks the cell ID and TAC so you can paste the output publicly. Never changes modem state. Copied to `/usr/bin/fm350-status` by `install.sh`. |
| `files/usr/sbin/fm350-watchdog`, `files/etc/init.d/fm350-watchdog` | Watchdog that restarts `wwan` when the `atc` handler hangs (see below). Installed and enabled by `install.sh` unless you pass `--no-watchdog`. |
| `uci/fm350-watchdog.uci` | Default settings for the watchdog (`/etc/config/fm350_watchdog`). |
| `uci/network-atc.uci` | `network.wwan` for the mrhaav `atc` protocol handler, plus `network.wan.metric`. |
| `uci/network-xmm.uci` | `network.wwan` for the modemfeed `xmm` protocol handler, plus `network.wan.metric`. |
| `uci/firewall.uci` | Adds `wwan` to the `wan` firewall zone and the "Allow modem RA" ICMPv6 rule. |
| `uci/mwan3.uci` | wan/wwan mwan3 tracking, members, `failover` policy and `default` rule. |
| `files/etc/hotplug.d/usb/50-fm350_driver` | Binds the `option` serial driver via `new_id` on kernel < 6.6 only. Copied by `install.sh` when needed. |
| `tests/docker-test.sh` | Repeatable install/uninstall idempotency test against an OpenWrt Docker rootfs. |
| `tests/fixtures/mwan3.default` | Vendored copy of the stock mwan3 default config (see below), used by `docker-test.sh`. |

All `uci/*.uci` files are `uci batch` snippets: they `delete` a section (or
`del_list`/`add_list` a single value) before (re-)creating it, so running
`install.sh` twice never duplicates sections or list entries. They are never
applied with `uci import`, so they never touch unrelated config in
`/etc/config/network`, `/etc/config/firewall` or `/etc/config/mwan3`.

### mwan3: not clobbering the stock config

`opkg install mwan3` on OpenWrt 24.10 ships a default `/etc/config/mwan3`
(vendored at `tests/fixtures/mwan3.default`) with its own `globals` and `wan`
sections, plus a `https` rule (tcp/443) and a `default_rule_v4` rule
(`0.0.0.0/0`), both pointed at a `balanced` policy and evaluated **before**
any rule `install.sh` appends. mwan3 is first-match, so an appended `default`
rule would never be reached for IPv4 traffic. To handle this without
destroying a user's existing mwan3 tuning, `install.sh`:

- never deletes `mwan3.globals` or `mwan3.wan`; it only sets the options this
  setup needs on them (`wan`: `enabled`, `family`, `interval`, `down`, `up`,
  plus its two `track_ip` values, each `add_list`ed only if not already
  tracked; `globals`: `mmx_mask` only if it was unset);
- fully owns and recreates `wwan`, `wan_m1`, `wwan_m2`, `failover` and
  `default` on every run;
- runs `uci reorder mwan3.default=1` so `default` is evaluated right after
  `globals`, ahead of every stock rule;
- if `mwan3.default_rule_v4` and/or `mwan3.https` exist, points their
  `use_policy` at `failover` too, saving the original value in a
  `fm350_orig_policy` option (only on the first run, so a second run doesn't
  overwrite the saved original with `failover`).

Since uci list values aren't tagged with who added them, `install.sh` records
exactly which `track_ip` values it added (as opposed to ones that were
already there, e.g. `1.1.1.1` is coincidentally also a stock mwan3 default)
in `mwan3.wan.fm350_added_track_ip`, so a value that predates `install.sh` is
never removed by `uninstall.sh`.

`uninstall.sh` mirrors all of this: it deletes `wwan`/`wan_m1`/`wwan_m2`/
`failover`/`default`, removes only the `track_ip` values listed in
`mwan3.wan.fm350_added_track_ip` from `mwan3.wan` and then deletes that
bookkeeping option (leaving the section and `mwan3.globals` alone), and
restores `default_rule_v4`/`https`'s original `use_policy` from
`fm350_orig_policy` before deleting that option.

**IPv6 is not covered**: `default_rule_v6` is left untouched (still pointed
at `balanced`), and the `failover` policy has no IPv6 members. If your ISP
hands out IPv6 on `wan`, IPv6 traffic will not fail over to `wwan`.

### fm350-watchdog: recovering a stuck `wwan`

mrhaav's `atc.sh` treats only `+CME ERROR ... (#33)` from `AT+CGACT` as fatal. Any other error during activation leaves it waiting forever for messages that never come, so `wwan` stays "connecting" and failover to 5G is silently unavailable (see [docs/compatibility-and-risks.md](../docs/compatibility-and-risks.md)).

`fm350-watchdog` is a small procd service that checks `ifstatus wwan` every 30 s. If the interface hasn't been up for 180 s in a row, it runs `ifdown wwan; ifup wwan` and logs to syslog (`logread -e fm350-watchdog`). Restarts back off exponentially up to 30 minutes, and the backoff resets once `wwan` has stayed up for 5 minutes. It leaves the interface alone when:

- you disabled it (`network.wwan.disabled=1` or `network.wwan.auto=0`),
- you paused the watchdog (`touch /tmp/fm350-watchdog.pause`), or
- the modem's `/dev/ttyUSB*` device is missing (unplugged or still booting).

It also waits until the router has been up for 5 minutes (`boot_grace`) before the first restart, so a slow first connection after boot isn't mistaken for a hang.

Settings live in `/etc/config/fm350_watchdog`: `enabled`, `interface`, `check_interval`, `pending_threshold`, `backoff_max`, `backoff_reset_after` and `boot_grace`. Values that aren't plain numbers fall back to the default with a log line. Re-running `install.sh` resets them to the defaults. The header of `files/usr/sbin/fm350-watchdog` documents each one. `tests/watchdog-test.sh` tests the decision logic with fake `ifstatus`/`ifup`/clock inside the OpenWrt Docker image. We haven't yet shown end to end that it recovers a real CGACT hang: the pty emulator has no netifd to restart.

## Usage

```sh
scp -r openwrt root@192.168.8.1:/root/
ssh root@192.168.8.1
cd /root/openwrt
./install.sh --apn internet.telekom          # mrhaav atc handler (default)
./install.sh --apn internet.telekom --proto xmm   # modemfeed xmm-modem instead
./install.sh --apn internet.telekom --dry-run     # print the uci commands, change nothing
./install.sh --apn internet.telekom --no-mwan3    # network + firewall only, no mwan3
```

After a real run:

```sh
/etc/init.d/network reload
/etc/init.d/firewall reload
/etc/init.d/mwan3 restart   # unless --no-mwan3
fm350-status                # confirm registration
mwan3 status                # confirm the failover policy is active
```

### Flags

| Flag | Default | Effect |
|---|---|---|
| `--proto atc\|xmm` | `atc` | `atc`: downloads and installs `luci-proto-atc` + `atc-fib-fm350_gl` (mrhaav). `xmm`: prints instructions for the modemfeed `xmm-modem` package instead (not in official feeds) and applies the `network.wwan` config for proto `xmm` without installing anything. |
| `--apn APN` | (required) | Carrier APN, e.g. `internet.telekom`, `web.vodafone.de`, `internet`. |
| `--dry-run` | off | Prints the uci commands and package actions that would run; installs nothing and changes no config. |
| `--no-mwan3` | off | Skips installing `mwan3`/`luci-app-mwan3` and skips `uci/mwan3.uci`. |
| `--no-watchdog` | off | Skips installing and enabling `fm350-watchdog`. |
| `--extras` | off | Also installs `usbutils` and `picocom`, for manual debugging (`lsusb`, `picocom -b 115200 /dev/ttyUSBn`). |

`install.sh` detects `opkg` vs `apk` automatically. It also auto-detects the
FM350's AT tty from sysfs (`*:1.6/ttyUSB*` for `0e8d:7127` mode 41, `*:1.4/ttyUSB*`
for `0e8d:7126` mode 40); if the modem isn't enumerated yet it falls back to
`/dev/ttyUSB4` with a warning, and `network.wwan.device` can be corrected by
hand afterwards.

## The `atc` protocol option names

`uci/network-atc.uci` uses the exact `network.wwan` option names read by
mrhaav's netifd proto script and validated by its LuCI form, both fetched
2026-09-25:

- `atc-fib-fm350_gl/files/lib/netifd/proto/atc.sh` (`proto_atc_init_config`),
  from <https://github.com/mrhaav/openwrt-packages/tree/main/atc-fib-fm350_gl>
- `luci/protocols/luci-proto-atc/htdocs/luci-static/resources/protocol/atc.js`,
  from <https://github.com/mrhaav/openwrt-packages/tree/main/luci/protocols/luci-proto-atc>

Options used: `device`, `apn`, `pdp` (`IP`/`IPV4V6`/`IPV6`, default `IP`),
`auth` (`0`=none), `delay` (modem boot timeout, default `15`), plus the
standard netifd defaults `defaultroute`, `peerdns`, `metric`. `pincode`,
`username`/`password`, `atc_debug`, `v6dns_ra` and `custom_at` exist but
aren't set here (no PIN lock assumed, IPv4 default route).

## Package URLs

`install.sh` downloads these files from `mrhaav/openwrt` (verified against the
GitHub contents API and `curl -sI` on 2026-09-25; the file names change with
every release, re-verify if `install.sh` starts reporting 404s):

- `luci-proto-atc_2025.01.10-r2_all.ipk` / `luci-proto-atc-2025.01.10-r2.apk`
- `atc-fib-fm350_gl_2025.08.24-r3_all.ipk` / `atc-fib-fm350_gl-2025.01.11-r2.apk`

(The `.apk` build of `atc-fib-fm350_gl` lags the `.ipk` build; `2025.01.11-r2`
is the newest `.apk` currently published, `2025.08.24-r3` the newest `.ipk`.)

## Testing

```sh
shellcheck -s sh install.sh uninstall.sh fm350-status.sh \
  files/etc/hotplug.d/usb/50-fm350_driver \
  files/usr/sbin/fm350-watchdog files/etc/init.d/fm350-watchdog tests/*.sh
./tests/fm350-decode-test.sh   # status decoder against canned AT responses (plain sh, no Docker)
./tests/watchdog-test.sh       # watchdog decision logic (Docker)
./tests/docker-test.sh         # install/uninstall idempotency (Docker)
```

`tests/docker-test.sh` pulls an OpenWrt rootfs image and runs two scenarios,
each in its own container:

1. stock mwan3: seeds `tests/fixtures/mwan3.default` (the real
   `opkg install mwan3` default config) as `/etc/config/mwan3`, and asserts
   that after `install.sh` the `default` rule is reordered ahead of
   `https`/`default_rule_v4`, both of those now use `failover`, and
   `mwan3.wan` still has its stock `track_ip` entries plus ours.
2. empty mwan3: seeds an empty `/etc/config/mwan3`, exercising the
   from-scratch path.

Both scenarios seed a minimal `/etc/config/network` (`wan`/`lan`) and reuse
the image's default `/etc/config/firewall` (already has a `wan` zone), run
`install.sh --dry-run` (must change nothing), then a real run with
`--skip-packages` (a hidden flag that skips `opkg`/`apk` and all downloads,
for use in environments without kernel modules or network access) twice
to prove idempotency (`uci show` must be byte-identical after run 1 and
run 2, including rule order and the `fm350_orig_policy` bookkeeping), then
run `uninstall.sh` and check that every installer-owned section is gone,
`mwan3.wan`/`mwan3.globals` still exist, and `default_rule_v4`/`https` (stock
scenario) are back to their original `use_policy`. It exits non-zero on any
mismatch and removes the containers it creates.

### Protocol handler against a fake modem: `tests/atc-test.sh`

Runs mrhaav's real, unmodified `atc.sh` (from the cached
`atc-fib-fm350_gl` .ipk, driven by real `gcom`) in the OpenWrt Docker rootfs
against `tests/atc-sim/fake_fm350.py`, an FM350-GL AT emulator on a pty. The
`network.wwan` config comes from `uci/network-atc.uci`. The only stubs are
netifd's ubus notify, which is logged instead, and the sysfs lookup of the
RNDIS netdev; each is documented in `tests/atc-sim/run_setup.sh`. Scenarios:

| Scenario | Asserted |
|---|---|
| `ok` | exact 20-command AT transcript; address, gateway host route, default route and DNS reach netifd |
| `nosim` | stops after `AT+CPIN?`, reports `SIM not inserted`, blocks restart; no hang |
| `cgact_error` | `+CME ERROR ... (#33)` on `AT+CGACT` → `SESSION_FAILED`, blocks restart |
| `slow_boot` | the modem stays silent for 3 s and the session still comes up |

`tests/atc-sim/responses.py` is the golden response table. Each entry notes
whether it is verbatim from the bench log or inferred from 3GPP 27.007.
Takes about 1.5 min and needs network access for `opkg`.

### Failover end to end: `tests/qemu-failover-test.sh`

Boots OpenWrt 24.10.8 (armsr-armv8, checksum verified, cached in
`tests/.cache/`) under `qemu-system-aarch64 -accel hvf`. It installs the real
`mwan3` package, runs `install.sh --skip-packages`, and then replaces only
`network.wwan` with a DHCP stand-in on its own QEMU uplink. A LAN client
(a netns on `br-lan`) generates the traffic. nft `postrouting` counters show
which NIC it actually leaves through. The test asserts:

- baseline traffic leaves via wan;
- `set_link wan off` fails over to wwan, and the LAN client keeps connectivity;
- `set_link wan on` fails back;
- a dead upstream (link up, all egress dropped) also fails over and back;
- with both uplinks down the policy is `unreachable` and the LAN ping fails
  instead of hanging.

Measured on 2026-09-25: failover 5 s / failback 4 s on link loss, 12–13 s /
16 s on a dead upstream. The bounds are derived from `uci/mwan3.uci`. Takes
about 2 min and needs network access (opkg, track pings).

## Rollback

```sh
cd /root/openwrt
./uninstall.sh
/etc/init.d/network reload
/etc/init.d/firewall reload
/etc/init.d/mwan3 restart
```

`uninstall.sh` removes the uci sections listed above, the hotplug driver file
and `/usr/bin/fm350-status`, but leaves packages installed (it prints the
`opkg remove` / `apk del` command for that). To fully revert, also run that
command and, if flashed over GL.iNet firmware, restore your backup from step 0
of the setup guide.
