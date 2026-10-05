# openwrt/ - router-side 5G failover setup

## In short

- These scripts automate [setup guide](../docs/setup-guide.md) steps 6–7 — installing a protocol handler and setting up mwan3 failover — for a Fibocom FM350-GL in a USB M.2 dongle on vanilla OpenWrt 24.10+.
- We built this for a GL.iNet Flint 2 (GL-MT6000). (An earlier version of this README said "built and tested against" the Flint 2, but the root README and the setup guide say the scripts haven't run on the Flint 2 itself yet; the testing described below is all emulated.) The scripts only use plain OpenWrt/mwan3/opkg-or-apk mechanisms, so they should work on other OpenWrt 24.10+ routers with a USB port too, but we haven't tried that. They target busybox ash; no bashisms.
- Tested so far in an OpenWrt Docker rootfs, against a pty modem emulator, and in a full OpenWrt-under-QEMU build with real mwan3 — not yet on real router hardware.
- A watchdog service works around a known hang in the `atc` protocol handler by restarting the `wwan` interface.
- IPv6 isn't covered by the failover policy; see the warning below.
- The backup SIM is metered: read [Metered SIM: tracking traffic and what is not capped](#metered-sim-tracking-traffic-and-what-is-not-capped) and [the PIN/PUK risk](#pinpuk-risk) before relying on it.

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
/etc/init.d/fm350-watchdog start   # unless --no-watchdog; install.sh only enables it
fm350-status                # confirm registration
mwan3 status                # confirm the failover policy is active
```

### Flags

| Flag | Default | Effect |
|---|---|---|
| `--proto atc\|xmm` | `atc` | `atc`: downloads and installs `luci-proto-atc` + `atc-fib-fm350_gl` (mrhaav). `xmm`: prints instructions for the modemfeed `xmm-modem` package instead (not in official feeds) and applies the `network.wwan` config for proto `xmm` without installing anything. |
| `--apn APN` | (required) | Carrier APN, e.g. `internet.telekom`, `web.vodafone.de`, `internet`. Letters, digits, dots, underscores, and hyphens are accepted. |
| `--dry-run` | off | Prints the uci commands and package actions that would run; installs nothing and changes no config. |
| `--no-mwan3` | off | Skips installing `mwan3`/`luci-app-mwan3` and skips `uci/mwan3.uci`. |
| `--no-watchdog` | off | Skips installing and enabling `fm350-watchdog`. |
| `--extras` | off | Also installs `usbutils` and `picocom`, for manual debugging (`lsusb`, `picocom -b 115200 /dev/ttyUSBn`). |

`install.sh` detects `opkg` vs `apk` automatically. It also auto-detects the
FM350's AT tty from sysfs (`*:1.6/ttyUSB*` for `0e8d:7127` mode 41, `*:1.4/ttyUSB*`
for `0e8d:7126` mode 40, `:1.6` preferred) and only accepts an interface whose
parent USB device has vendor ID `0e8d`, so another USB serial adapter is never
picked. The lookup lives in `files/usr/lib/fm350/at-port.sh`, installed to
`/usr/lib/fm350/at-port.sh` and shared by `install.sh`, `fm350-status` and the
watchdog. If the modem isn't enumerated yet, `install.sh` falls back to
`/dev/ttyUSB4` with a warning; the watchdog re-resolves the port before each
`ifup` and updates `network.wwan.device` if the configured path is gone or is
no longer an FM350 AT port (its `ubus call network reload` step is unverified
on hardware).

`install.sh` also checks that a `wan` firewall zone exists before it changes
any config, and shows `uci batch` errors instead of hiding them (`-q` is no
longer passed).

### What happens to your existing config

- **Your own `network.wwan` / mwan3 `wwan`, `wan_m1`, `wwan_m2`, `failover`,
  `default`**: every section the installer creates carries `option
  fm350_owned '1'`. On the first install, a section with one of those names
  but without the marker is yours: it is saved (as `uci batch` commands,
  lists kept as lists) to `/etc/fm350-usb/<config>.<section>.batch` before it
  is replaced, and `uninstall.sh` restores it. Restored sections are appended
  at the end of their config, so their position in the rule order may change.
  Unverified on real `uci`; the conversion was only exercised against sample
  `uci export` text and a fake `uci`. `uninstall.sh` only deletes those
  sections (and `network.wwan`) when they carry the marker or `install.sh`
  recorded its state for that config, so an uninstall after `--no-mwan3`, or
  without a prior install, leaves your own `mwan3` sections and `network.wwan`
  untouched.
- **Re-running** `install.sh` keeps options you added to our `network.wwan`
  (for example `pincode`, `atc_debug`, `custom_at`, `pdp`) as long as `--proto`
  is unchanged; only the options the template sets (`proto`, `device`, `apn`,
  `auth`, `delay`, `defaultroute`, `peerdns`, `metric`) are refreshed.
- **`wwan` in the `wan` firewall zone**: if it was already in the zone's
  network list before the first install, `uninstall.sh` leaves it there.

## Rollback

```sh
cd /root/openwrt
./uninstall.sh
/etc/init.d/network reload
/etc/init.d/firewall reload
/etc/init.d/mwan3 restart
```

`uninstall.sh` removes the uci sections listed below, the hotplug driver file,
`/usr/bin/fm350-status`, `/usr/lib/fm350/at-port.sh` and the watchdog (it
stops and disables the service, then deletes `/etc/init.d/fm350-watchdog`,
`/usr/sbin/fm350-watchdog` and `/etc/config/fm350_watchdog`), restores any of
your own sections that `install.sh` saved, but leaves packages installed (it prints the
`opkg remove` / `apk del` command for that). To fully revert, also run that
command and, if flashed over GL.iNet firmware, restore your backup from step 0
of the setup guide.

## What gets installed

| File | Purpose |
|---|---|
| `install.sh` | Installs packages, downloads the FM350 protocol handler, and applies the uci config below. Run as root on the router. |
| `uninstall.sh` | Removes the uci sections and files added by `install.sh`. Leaves packages installed. |
| `fm350-status.sh` | Read-only modem status: registration, operator, signal (RSRP/RSRQ/SINR), serving cell and band, temperature, FCC-lock state. `-r` prints the raw AT responses, `-x` masks the cell ID and TAC so you can paste the output publicly. Never changes modem state. Copied to `/usr/bin/fm350-status` by `install.sh`. |
| `files/usr/sbin/fm350-watchdog`, `files/etc/init.d/fm350-watchdog` | Watchdog that restarts `wwan` when the `atc` handler hangs (see below). Installed and enabled by `install.sh` unless you pass `--no-watchdog`. |
| `files/usr/lib/fm350/at-port.sh` | Shared AT tty lookup (vendor `0e8d` only, `:1.6` before `:1.4`), sourced by `install.sh`, `fm350-status` and the watchdog. Installed to `/usr/lib/fm350/at-port.sh`. |
| `uci/fm350-watchdog.uci` | Default settings for the watchdog (`/etc/config/fm350_watchdog`). |
| `uci/network-atc.uci` | `network.wwan` for the mrhaav `atc` protocol handler, plus `network.wan.metric`. |
| `uci/network-xmm.uci` | `network.wwan` for the modemfeed `xmm` protocol handler, plus `network.wan.metric`. |
| `uci/firewall.uci` | Adds `wwan` to the `wan` firewall zone and the "Allow modem RA" ICMPv6 rule. |
| `uci/mwan3.uci` | wan/wwan mwan3 tracking, members, `failover` policy and `default` rule. |
| `files/etc/hotplug.d/usb/50-fm350_driver` | Binds the `option` serial driver via `new_id` on kernel < 6.6 only. Copied by `install.sh` when needed. |

All `uci/*.uci` files are `uci batch` snippets: they `delete` a section (or
`del_list`/`add_list` a single value) before (re-)creating it, so running
`install.sh` twice never duplicates sections or list entries. They are never
applied with `uci import`, so they never touch unrelated config in
`/etc/config/network`, `/etc/config/firewall` or `/etc/config/mwan3`.

## How it avoids clobbering your mwan3 config

In short: installing `mwan3` on OpenWrt 24.10 gives you a working default config of its own, already pointed at a `wan`-only policy. Rather than overwrite that, `install.sh` builds its failover setup around whatever is already there: it only ever changes the handful of settings it actually needs, it fully owns and rebuilds its own sections every run so re-running it is safe, and where it has to redirect an existing rule to its failover policy, it remembers the original value so `uninstall.sh` can put it back exactly.

The precise mechanics: `opkg install mwan3` on OpenWrt 24.10 ships a default
`/etc/config/mwan3` with its own `globals` and `wan`
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

`install.sh` also records the original `network.wan.metric` and the
`mwan3.wan` options it changes, preserving whether each option was unset.
The first install's snapshot is retained if you run the installer again.
`uninstall.sh` mirrors all of this: it restores those original values, deletes `wwan`/`wan_m1`/`wwan_m2`/
`failover`/`default`, removes only the `track_ip` values listed in
`mwan3.wan.fm350_added_track_ip` from `mwan3.wan` and then deletes that
bookkeeping option (leaving pre-existing sections in place), and
restores `default_rule_v4`/`https`'s original `use_policy` from
`fm350_orig_policy` before deleting that option. It also removes a
`mwan3.wan` or `mwan3.globals` section if the installer created it.

> **IPv6 is not covered**: the `failover` policy has no IPv6 members, so IPv6
> traffic never fails over to `wwan`. mwan3's stock `default_rule_v6` (policy
> `balanced`) can blackhole LAN IPv6 when `wan6` is disabled, which is the
> stock setting. So `install.sh` points `default_rule_v6` at the stock
> `wan_only` policy when `mwan3.wan6` is enabled, and otherwise removes the
> rule (saving it in `/etc/fm350-usb`, and `fm350_orig_policy` for the
> redirect case, so `uninstall.sh` restores it). **This IPv6 handling is
> unverified on hardware** (and untested under QEMU).

## Metered SIM: tracking traffic and what is not capped

The backup SIM is a metered 5 GB plan (Telekom), so the mwan3 defaults in
`uci/mwan3.uci` are tuned to keep probing small and failover stable:

- `wan`: `interval 5`, `down 5`, `up 10`. A blip of a few seconds doesn't move
  traffic to the SIM (25 s of misses to fail over, 50 s of successes to fail back).
- `wwan`: `interval 30`, `reliability 1`, two track IPs. Roughly one ping
  (about 170 bytes with the reply) per probe means about 15 MB per month if the
  first track IP answers, up to about 30 MB if both are probed every cycle.
  This is an estimate, not a measurement; mwan3track also runs while `wan` is
  healthy.
- **Nothing caps or filters LAN traffic during failover.** While `wan` is down,
  every LAN client (updates, backups, video, cloud sync) uses the 5 GB. Set a
  data limit or alert with the carrier, or add your own firewall/QoS rules.
- `peerdns` is left at `1` on `wwan`: when the interface is up, the carrier's DNS
  servers are added to the resolver list, so DNS queries may go over the
  metered link even while `wan` is healthy, depending on how dnsmasq picks
  servers. We have not measured this.

## PIN/PUK risk

If the SIM has a PIN, `atc.sh` sends `AT+CPIN="<pincode>"` from the
`network.wwan.pincode` option on every interface start. A wrong PIN is tried
again at each start and three failures PUK-lock the SIM (a carrier-issued PUK
is then needed, and ten wrong PUKs destroy the SIM). The watchdog restarts
`wwan`, so it must never do that blindly: it reads `ifstatus wwan`'s
`errors[].code` and does **not** restart (it logs once per episode) when a code
contains `pin`, `puk`, `sim` or `denied`, case-insensitively. This covers the
codes `atc.sh` passes to `proto_notify_error` right before
`proto_block_restart` in the pinned revision: `PINmissing`, `PINerror`,
`SIMreadfailure`, `REG_DENIED`, and raw `+CME ERROR` texts such as
`SIM not inserted`. Assumption: these codes show up in `ifstatus` as
`errors[].code`; this is unverified on hardware, and `SESSION_FAILED` (bad
APN) is still retried with backoff. Prefer a SIM with the PIN disabled, and do
not set `pincode` until you have checked the PIN by hand.

## fm350-watchdog: recovering a stuck `wwan`

mrhaav's `atc.sh` treats only `+CME ERROR ... (#33)` from `AT+CGACT` as fatal. Any other error during activation leaves it waiting forever for messages that never come, so `wwan` stays "connecting" and failover to 5G is silently unavailable (see [docs/compatibility-and-risks.md](../docs/compatibility-and-risks.md)).

`fm350-watchdog` is a small procd service that checks `ifstatus wwan` every 30 s. If the interface hasn't been up for 600 s in a row (`pending_threshold`, raised from 180 s to be gentle with SIM PIN attempts and slow cell search), it runs `ifdown wwan; ifup wwan` and logs to syslog (`logread -e fm350-watchdog`). Restarts back off exponentially up to 30 minutes, and the backoff resets once `wwan` has stayed up for 5 minutes. It leaves the interface alone when:

- you disabled it (`network.wwan.disabled=1` or `network.wwan.auto=0`),
- you paused the watchdog (`touch /tmp/fm350-watchdog.pause`),
- the modem is absent (no FM350 AT port in sysfs; its clocks are reset, so a replug doesn't trigger an immediate restart), or
- `ifstatus` shows a SIM/PIN/registration-denied error (see [the PIN/PUK risk](#pinpuk-risk)).

The watchdog runs without `set -e`, so a failing `ifstatus`, `jsonfilter` or `uci` call is treated as "unknown" instead of killing it, and procd respawns it indefinitely (`respawn 3600 5 0`). `check_interval`, `pending_threshold` and `backoff_max` must be greater than 0; a `0` falls back to the default.

It also waits until the router has been up for 5 minutes (`boot_grace`) before the first restart, so a slow first connection after boot isn't mistaken for a hang.

Settings live in `/etc/config/fm350_watchdog`: `enabled`, `interface`, `check_interval`, `pending_threshold`, `backoff_max`, `backoff_reset_after` and `boot_grace`. Values that aren't plain numbers fall back to the default with a log line. Re-running `install.sh` resets them to the defaults. The header of `files/usr/sbin/fm350-watchdog` documents each one. The decision logic was only exercised with fake `ifstatus`/`ifup`/clock and stubbed `uci`/`jsonfilter`. We haven't yet shown end to end that it recovers a real CGACT hang: the pty emulator has no netifd to restart.

## For contributors

### The `atc` protocol option names

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

### Package URLs

`install.sh` downloads these files from `mrhaav/openwrt`, **pinned to commit
[`0d56d84`](https://github.com/mrhaav/openwrt/tree/0d56d844cc49906285c9181a008186f4af515c85/atc)**
(the `master` head we verified against on 2026-09-25; it hadn't changed since
2026-05-18). It checks each file's SHA-256 before installing anything, and
stops if a file doesn't match:

| File | SHA-256 |
|---|---|
| `luci-proto-atc_2025.01.10-r2_all.ipk` | `c3c70dbeb90c1f181024cc6c9b0449b5c549f558cf8f932350ee6b93cebd81d6` |
| `atc-fib-fm350_gl_2025.08.24-r3_all.ipk` | `7a15abc63d09c36b75ac88b8601817f56d3e8e5f65385c02a5fb605fd6b15050` |
| `luci-proto-atc-2025.01.10-r2.apk` | `7a196e9a2565534d4657d81ce9c18794ad3687812fe941bf76aa2c4106577484` |
| `atc-fib-fm350_gl-2025.01.11-r2.apk` | `94e097b6a674f818921c648ed8c6ab80639e626c129f37d4224e64fe37c2eba0` |

The `atc-fib-fm350_gl` `.ipk` is byte-identical to the one that was run
against a fake modem. The other three files were fetched from the same
commit but haven't been exercised by a test.

(The `.apk` build of `atc-fib-fm350_gl` lags the `.ipk` build; `2025.01.11-r2`
is the newest `.apk` published at that commit, `2025.08.24-r3` the newest `.ipk`.)

**Updating to a newer release** is a deliberate step: pick the new commit and
file names in mrhaav/openwrt, download the files, check them, and update the
URLs and hashes in `install.sh` together.

**What you're still trusting:** the pinned files are whatever mrhaav published
at that commit. The pin and the hashes guarantee that you get the same bytes
we tested, not that those bytes were reviewed line by line. On `apk` systems
they're installed with `apk add --allow-untrusted`, because they aren't signed
with an OpenWrt feed key; the base packages from the official feeds (`mwan3`,
`kmod-*`, `comgt`) are installed normally, with signature checks.

**Worth flagging:** the option names above come from `mrhaav/openwrt-packages` (`main`), while `install.sh` downloads the built packages from a different repository, `mrhaav/openwrt` (`atc/`, pinned commit above). Both are kept here as given because that's what we verified against; whether the two repos track exactly the same code wasn't confirmed, so treat this as something to double-check if the two ever seem to disagree.

### Linting

```sh
shellcheck -s sh install.sh uninstall.sh fm350-status.sh \
  files/etc/hotplug.d/usb/50-fm350_driver \
  files/usr/sbin/fm350-watchdog files/etc/init.d/fm350-watchdog
```

### Failover measured under QEMU

The run booted OpenWrt 24.10.8 (armsr-armv8, checksum verified) under `qemu-system-aarch64 -accel hvf`, installed the real
`mwan3` package, ran `install.sh --skip-packages`, and then replaced only
`network.wwan` with a DHCP stand-in on its own QEMU uplink. A LAN client
(a netns on `br-lan`) generates the traffic. nft `postrouting` counters show
which NIC it actually leaves through. The run checked:

- baseline traffic leaves via wan;
- `set_link wan off` fails over to wwan, and the LAN client keeps connectivity;
- `set_link wan on` fails back;
- a dead upstream (link up, all egress dropped) also fails over and back;
- with both uplinks down the policy is `unreachable` and the LAN ping fails
  instead of hanging.

Measured on 2026-09-25, with the earlier mwan3 settings (`wan` down/up 3/3,
`wwan` interval 10): failover 5 s / failback 4 s on link loss, 12–13 s /
16 s on a dead upstream. With the current defaults (`wan` down 5 / up 10,
`wwan` interval 30) expect roughly 25 s / 50 s on a dead upstream and a
longer run (calculated, not yet re-measured). The bounds are derived from `uci/mwan3.uci`. The root README rounds this to "13 s"; treat the 12–13 s range here as the more precise figure, since it comes directly from this run.

## What's not proven yet

- **IPv6** isn't covered by the failover policy at all, and the `default_rule_v6` handling is unverified (see the warning above).
- Saving/restoring your own sections, the AT port re-resolve, and the SIM/PIN skip on real `ifstatus` `errors[].code` values have only been tested against stubs and sample text.
- The watchdog hasn't been shown, end to end, to recover a real `AT+CGACT` hang — only its decision logic is tested, against a fake clock and fake `ifstatus`/`ifup`.
- Everything here is verified in Docker, against a pty emulator, and under QEMU — none of it has run on real router hardware yet.
- The package file names in [Package URLs](#package-urls) change with every upstream release; re-verify them if `install.sh` starts reporting 404s.

## Glossary

Terms used on this page, defined in the [shared glossary](../docs/glossary.md): [AT command](../docs/glossary.md#at-command), [Failover / failback](../docs/glossary.md#failover--failback), [mwan3](../docs/glossary.md#mwan3), [Protocol handler](../docs/glossary.md#protocol-handler), [QEMU / Docker](../docs/glossary.md#qemu--docker), [RSRP / RSRQ / SINR](../docs/glossary.md#rsrp--rsrq--sinr), [uci](../docs/glossary.md#uci), [Watchdog](../docs/glossary.md#watchdog).
