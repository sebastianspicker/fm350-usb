# fm350mac

A user-space macOS data path for a Fibocom FM350-GL 5G modem: USB RNDIS
(libusb, via our own ctypes binding in `usb_async.py` -- no pyusb) bridged
to a macOS `utun` interface, with no kernel extensions, DriverKit
entitlements or SIP changes. It's for anyone bench-testing an FM350-GL /
Dell DW5931e on a Mac, either as a throwaway uplink or to check SIM,
registration and throughput before the module goes on a router.

See [`../docs/macos-driver.md`](../docs/macos-driver.md) for the full
architecture, session flow and rationale.

## In short

- `fm350mac` is a pure-Python program that talks to the FM350-GL's USB RNDIS interface directly (via libusb) and hands the packets to a macOS `utun` interface — no kernel extension, no DriverKit entitlement, no SIP change.
- USB access itself needs no root. A small root helper, installed once as a background service, does only the part that does need root (creating the `utun` interface and setting routes/DNS), so the main program runs as you.
- `probe` and the read-only commands (`status`, `doctor`, `at`) work against real hardware. The full data session (`up`) has run against an in-process fake modem (`--loopback`) and, briefly, against the real modem with a Telekom DE SIM (2026-10-05, `up --route-host`; see Limitations).

## Status

The [root README's status table](../README.md#status) has the project-wide picture; this section is the detail for `fm350mac` itself.

Scaffold implemented and reviewed (2026-09-25): 101 unit tests pass, and
`fm350mac probe` works against real hardware. At that point the data path
(`up`) had code and unit-test coverage but hadn't run live yet; it first ran
against a real SIM on 2026-10-05 (see Limitations). `up --loopback`, which replaces the modem with
an in-process fake, has run live successfully; see
[`../docs/bench-log.md`](../docs/bench-log.md).

Since then the suite has grown (`uv run pytest -q` collected **327 tests** on 2026-09-26, all passing). The sections below also cover `status --watch`, `doctor` and the privilege-separation helper.

## Requirements

- macOS on Apple Silicon (developed and tested against macOS 27)
- Python >= 3.11
- [uv](https://docs.astral.sh/uv/)
- libusb: `brew install libusb`

## Install

```sh
cd fm350mac
uv sync
```

## Commands

```sh
uv run fm350mac probe                       # RNDIS init + query OIDs, then halt (no root)
uv run fm350mac at 'ATI' 'AT+CPIN?'         # send raw AT commands (no root)
uv run fm350mac status                      # SIM/registration/cell/thermal status (no root)
uv run fm350mac doctor                      # read-only diagnostic checks, one OK/WARN/INFO line each (no root)
uv run fm350mac connect --apn internet      # define + activate a PDP context (no root)
uv run fm350mac disconnect                  # deactivate the PDP context (no root)
uv run fm350mac up --apn internet           # run the full session: AT + RNDIS + utun + routes + pump
uv run fm350mac async-selftest              # live, read-only check of the async USB transfer pools
uv run fm350mac helper install|uninstall|status  # manage the root helper LaunchDaemon

# One-time setup so `up` doesn't need sudo (see "Privilege separation" below):
sudo uv run fm350mac helper install
uv run fm350mac up --apn internet --default-route --dns

# Smoke-test the utun/route/bridge path with no SIM and no real modem:
uv run fm350mac up --loopback
ping 198.51.100.1                           # answered in-process, see loopback.py

# Live, read-only check of the async USB transfer pools (no SIM needed).
# Ends with a real USB reset -- run this at most a couple of times in a row.
uv run fm350mac async-selftest
```

## Checking the modem: `status` and `doctor`

`status` prints a readable snapshot: SIM, LTE and NR registration, operator and access technology, the serving cell (band, EARFCN, PCI, RSRP, RSRQ), NR signal when the modem measures a 5G carrier, neighbour cells by band, and temperature. Options:

- `--raw` shows the AT responses as the modem sent them.
- `--json` prints machine-readable output (not combinable with `--watch`, which redraws the screen).
- `--redact` masks the cell ID and TAC (serving and neighbour cells). Use it before you paste output anywhere public: with the operator code, those two values locate you to within a few hundred metres in public cell databases.
- `--watch [SECONDS]` refreshes every 2 s (or the interval you give) with a signal bar. It's handy for aiming antennas. Ctrl-C stops it.

If `status` reports no cell at all, check the antenna pigtails before anything else — that was the cause on our own unit; see the [Dell guide's troubleshooting section](../docs/dell-dw5931e-usb.md#no-cells-at-all-cesq-all-99255-gtccinfo-empty) rather than repeating that story here.

`doctor` reads the settings that matter on OEM modules and explains them: firmware image (`_5025` = Dell DW5931e), DIPC mode, FCC lock, `GTFMODE`, USB mode, RAT mode, antenna tuner, radio and SIM state, and whether any cell is measured. It prints one `[OK]`, `[WARN]` or `[INFO]` line per check and exits 1 if anything is a warning. It only sends read commands; a unit test enforces that.

Signal values are decoded per 3GPP TS 27.007, reporting the lower bound of each range the modem returns (the router's `fm350-status` uses the same convention).

## Running a data session: `up`

With the helper installed, `up` (and `up --loopback`) need no root at all:
they create the `utun` interface and set routes/DNS through the helper (see
"Privilege separation" below). Without it, pass `--no-helper` and run with
`sudo`, the same as before privilege separation existed. `--dry-run` never
needs root or the helper, with either mode.

### `--route-host`, `--dns`, `--dry-run`

- `--route-host IP` (repeatable, max 8, unicast IPv4) adds a host route
  `IP -> utunN` after the interface is configured and removes it on
  teardown. Use it instead of `--default-route` for safe testing on a metered
  SIM: only the chosen hosts go through the tunnel, e.g.
  `uv run fm350mac up --apn internet --route-host 1.1.1.1` then
  `ping 1.1.1.1`.
- The modem's DNS servers are always queried and logged (`DNS=[...]` or
  `DNS=<none returned>`). `--dns` publishes them via `scutil`, and requires
  `--default-route` (otherwise the carrier's resolver would be queried over
  the normal uplink, so `up` refuses). A warning is logged if `--dns` was
  given but the modem returned no servers.
- `--dry-run` has no side effects: only read-only AT queries (`CPIN?`,
  `CGSN`, `CGACT?`, `CGPADDR`, `GTDNS`), no `CGDCONT`/`CGACT`, no RNDIS
  init/halt, nothing run on the system. It prints the AT and system
  commands it would run (with a placeholder address if the PDP context isn't
  active yet).
- `--redact` masks the assigned IP, DNS servers and IMEI in every log line
  (and in the `--dry-run` plan).
- On shutdown (SIGINT/SIGTERM/SIGHUP) routes/DNS are removed first, then the
  bridge stops, then the PDP context is deactivated, then RNDIS is halted.
  While waiting for a modem re-enumeration, the default route, host routes
  and DNS are removed too (so the Mac isn't left routing into a dead utun)
  and re-added after the rebuild.

### `--io async|sync`

`up` keeps several USB bulk transfers in flight per direction by default
(`--io async`, via `usb_async.py`/`async_bridge.py`), instead of one
synchronous transfer per packet. `--io sync` falls back to the original
one-transfer-per-packet path (`bridge.py`) if async ever needs to be ruled
out while debugging. `--rx-urbs`/`--tx-urbs` (default 8 each) control how
many transfers stay in flight per direction with `--io async`.

`async-selftest` exercises the async transfer pools against the real modem
without a SIM: 8 RX transfers pending for 3s then cancelled (expect all 8
retire `CANCELLED`), 5 ARP frames submitted on the OUT pool with no active
bearer (expect 3 `COMPLETED` + 2 `TIMED_OUT` -- the modem's known no-bearer
queue depth), then an RNDIS halt. It always ends with a USB reset (step 2
leaves the modem's TX queue jammed) and confirms the modem re-enumerates and
responds to a fresh RNDIS INIT afterwards.

### `--loopback`

`up --loopback` replaces the AT/RNDIS/USB modem with an in-process fake
(`loopback.py`): no SIM, no real device needed. It answers ARP and ICMP echo
requests for any destination, addressed as `192.0.2.2` (TEST-NET) with a
host route only to `198.51.100.1` (TEST-NET-2) -- never the default
route, even if `--default-route` is also passed (a warning is printed and
the flag is ignored). No DNS changes are made in loopback mode.

### `--supervise` / `--no-supervise` and reconnects

`up` runs a reconnect supervisor by default (`--supervise`, on by default;
disable with `--no-supervise` to fall back to a plain sleep loop). It polls
registration (`AT+CEREG?`/`AT+C5GREG?`) and the PDP context (`AT+CGPADDR`)
every 10s with read-only AT commands, and on loss reconnects
(`AT+CGACT=0` then `1`) with exponential backoff (5s up to 300s, reset after
10 minutes stable). If the modem's IP changes across a reconnect, the utun
address is updated in place and the bridge is told about it.

If the bridge fails because the USB device itself disappeared (the FM350's
firmware is known to crash and re-enumerate under real network conditions --
see `rndis_device.py`), `up` doesn't give up immediately: it keeps the utun
interface (removing its routes and DNS meanwhile), polls for the modem to come
back for up to `--reenum-timeout` seconds (default 180), waits 15s for its
firmware to settle, and rebuilds the whole AT/RNDIS/bridge session. If the
modem doesn't come back in time, `up` exits with status 3; if a modem with a
different IMEI comes back, it refuses to rebuild and exits with status 4. Any
other fatal bridge failure (or a device loss with `--no-supervise`) exits with
status 2.

## Privilege separation and the trust model

USB access (libusb claim, RNDIS, AT) never needs root; only creating the
`utun` interface and setting routes/DNS do. `fm350mac` splits those apart:

| | Main process (`fm350mac up`) | Helper (`fm350mac-helper`) |
|---|---|---|
| Runs as | you (no root) | root, via a LaunchDaemon |
| Interpreter | Homebrew/`.venv` Python (any) | `/usr/bin/python3` only (Apple's Command Line Tools Python, root-owned, `-I -S` isolated: nothing user-writable is on its path) |
| Code | everything: USB, RNDIS, AT, bridge, supervisor | one file, stdlib-only, Python 3.9 compatible (`src/fm350mac/helper/fm350mac_helper.py`), installed root:wheel 0755 |
| Does | data path | creates the `utun`, hands its file descriptor back over a Unix socket, sets the address/routes/DNS, undoes all of it when the connection closes -- for any reason, including `kill -9` of the main process |

This means nothing user-writable (your Homebrew prefix, this project's
`.venv`, any dependency) ever runs as root once the helper is installed --
the previous trust model (`sudo fm350mac up` running your whole venv as
root) is gone. See
[`../docs/macos-driver.md`](../docs/macos-driver.md#privilege-separation-decided-2026-09-25)
for the wire protocol and the helper's internals.

### Setup

```sh
sudo uv run fm350mac helper install    # copies the helper, writes and loads the LaunchDaemon
uv run fm350mac helper status          # no sudo needed: reports files/socket/reachability
sudo uv run fm350mac helper uninstall  # reverses install
```

Both `install` and `uninstall` print every step before doing it, and support
`--dry-run` (prints the plan only, needs no sudo). `install` refuses to run
if `/usr/bin/python3` is missing or not root-owned, or if the helper file
doesn't compile under it -- so a bad install fails loudly instead of quietly
installing a broken daemon. The helper's `LaunchDaemon` plist has
`RunAtLoad`/`KeepAlive` both false: launchd starts it on the first
connection, via the plist's `Sockets` entry with `SockPathOwner` set to the
installing user's uid, so the unprivileged main process can open a socket
that launchd itself created as root. Once started, the helper stays resident
(idle) until `helper uninstall` or a reboot, so `helper install` first runs
`/bin/launchctl bootout system/de.fm350mac.helper` (failure ignored) to
replace a running helper when you reinstall after an update.
`helper status` prints "installed helper is out of date" if the installed
file differs from the packaged one; `helper uninstall` also removes
`/var/log/fm350mac-helper.log`.

Once installed, `up` (and `up --loopback`) use the helper automatically and
need no root. Pass `--no-helper` to fall back to the old direct/root path
(`sudo fm350mac up --no-helper ...`) -- useful if the helper isn't installed,
or for comparison/debugging.

### If `up` is killed uncleanly (e.g. `kill -9` / SIGKILL)

With the helper (the default), this is handled: `kill -9`-ing `up` closes
its socket to the helper, which undoes everything it configured for that
connection (routes, DNS, the interface) in reverse order -- no manual
cleanup needed.

With `--no-helper`, teardown runs in a `finally` block and is best-effort,
but nothing survives `SIGKILL`: it can leave the default route and/or the
DNS resolver key pointing at the (now-gone) tunnel. Clean up by hand:

```sh
# Restore the default route (replace en0/<gateway> with what `netstat -rn`
# shows as the correct interface/gateway for your network):
sudo route delete default
sudo route add default <gateway-or-interface>

# Remove the DNS key fm350mac published:
sudo scutil <<'EOF'
remove State:/Network/Service/fm350mac/DNS
EOF
```

## Limitations

- The real data path has been run live only briefly (2026-10-05, Telekom DE,
  `up --route-host`: ping, HTTPS, a 1 MB download). Long sessions,
  re-enumeration recovery and `--default-route` / `--dns` on hardware are
  still verified only by unit tests and `up --loopback`.
- macOS counts received packets twice in a utun's `netstat -ib` byte counters
  (a 1 MB download showed ~2.1 MB); the `stats:` line `up` logs on shutdown
  is the real SIM usage.
- Throughput has only been measured in short, capped `iperf3` runs
  (2026-10-05, 5 MB per test on a weak LTE B7 cell: 20.4 Mbit/s down,
  13.8 Mbit/s up, ~9% CPU). The ~150 Mbps async design target (see
  [`../docs/macos-driver.md`](../docs/macos-driver.md)) is still an estimate.
  That first upload run also exposed the OUT pool acting as an 8-packet
  tail-drop queue; the tx thread now waits up to 1 s for a free slot
  (backpressure into the utun) before dropping. A later sync-vs-async
  comparison (interleaved rounds, 5 MB per test) found a lost wakeup in that
  wait that made async uploads slower than sync in 4/4 rounds; after the fix
  async uploaded at 15.2/9.1/7.2 Mbit/s vs sync 6.7/7.7 (one sync run ended
  by the server). Both modes are radio-limited here (driver CPU 3-8%); async
  stays the default. `--io sync` works but had one unexplained download stall
  in 6 runs (the missing data never reached the driver).
- No IPv6 support yet (`IPV4V6` PDP context, router advertisements from the
  modem).
- macOS/Apple Silicon only, by design (see the rejected alternatives in
  [`../docs/macos-driver.md`](../docs/macos-driver.md)): no Linux/Windows
  support, no kernel extension, no DriverKit dext.
- DHCP on the FM350's RNDIS interface is unreliable; `fm350mac` always
  assigns the IP itself from `AT+CGPADDR` instead of relying on DHCP.

## For contributors: Tests

```sh
cd fm350mac && uv run pytest -q
```

Tests are pure unit tests and fake-hardware end-to-end tests: RNDIS codec,
Ethernet/ARP, AT response parsers, utun AF framing, netconfig dry-run,
bridge threads with fake USB/utun, CLI argument validation, `cli.py` command
functions with a scriptable fake AT port and fake RNDIS/utun, the loopback
fake modem, and the reconnect supervisor — no USB or network access, no root,
no real subprocess calls, no SIM needed.

`usb_async.py`'s ctypes binding has its own tests: `test_usb_async_layout.py`
compiles a small C program against the real `libusb.h` and checks
`LibusbTransfer`'s field layout against it (skipped, with a reason, only if
no C compiler or the header is missing); `test_usb_async_pool.py` exercises
`AsyncEndpoint`/`EventLoop`'s transfer-lifetime state machine against a fake
`Libusb` that records submit/cancel/free calls and lets tests fire a
transfer's callback with any status; `test_async_bridge.py` covers
`AsyncBridge`'s RX ordering, ARP replies and TX pool exhaustion the same
way. None of these touch real hardware.

`test_helper.py` covers the root helper (`helper/fm350mac_helper.py`) and
its client (`helper_client.py`): request validation for every op (bad IPs,
`0.0.0.0`, multicast, oversize messages, unknown ops/fields, a non-`/24`
loopback host), the reverse-order teardown on disconnect, default-route
capture/restore semantics, peer-uid rejection (with an injected credential
lookup), `SCM_RIGHTS` fd passing over a `socket.socketpair()` (a pipe fd
standing in for a real utun), and an end-to-end run of `cli.cmd_up
--loopback` against a real helper server on a background thread. `helper
install|uninstall|status` are covered by `test_helper_admin.py`, entirely
through `--dry-run`/injected `subprocess.run`/`launchctl` fakes -- no sudo,
nothing under `/usr/local`, `/Library` or `/var/run` is ever touched.

Because the helper file itself must run under the *system*
`/usr/bin/python3` (3.9.6, `-I -S`), not this project's `.venv`:

```sh
tests/run_helper_tests_py39.sh
```

compiles it with `python3 -I -S -m py_compile` and runs a stdlib-`unittest`
port of its core tests (`tests/helper_unittest_py39.py`) under that exact
interpreter -- no pytest, no third-party imports, since `-I -S` gives it no
access to site-packages.

## Glossary

Terms used on this page, defined in the [shared glossary](../docs/glossary.md): [APN](../docs/glossary.md#apn), [AT command](../docs/glossary.md#at-command), [Band / EARFCN / PCI](../docs/glossary.md#band--earfcn--pci), [Cell ID / TAC](../docs/glossary.md#cell-id--tac), [DIPC mode](../docs/glossary.md#dipc-mode), [FCC lock](../docs/glossary.md#fcc-lock), [LaunchDaemon](../docs/glossary.md#launchdaemon), [libusb](../docs/glossary.md#libusb), [OEM image](../docs/glossary.md#oem-image), [PDP context / data session](../docs/glossary.md#pdp-context--data-session), [RAT](../docs/glossary.md#rat), [RNDIS](../docs/glossary.md#rndis), [RSRP / RSRQ / SINR](../docs/glossary.md#rsrp--rsrq--sinr), [utun](../docs/glossary.md#utun).
