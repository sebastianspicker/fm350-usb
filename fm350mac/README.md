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
- `probe` and the read-only commands (`status`, `doctor`, `at`) work against real hardware. The full data session (`up`) has run successfully against an in-process fake modem (`--loopback`), but not yet against a real SIM.

## Status

The [root README's status table](../README.md#status) has the project-wide picture; this section is the detail for `fm350mac` itself.

`fm350mac probe` works against real hardware. The data path (`up`) hasn't run live yet -- it needs a SIM and root
(or the helper, see below). `up --loopback`, which replaces the modem with
an in-process fake, has run live successfully; see
[`../docs/bench-log.md`](../docs/bench-log.md).

The sections below also cover `status --watch`, `doctor` and the privilege-separation helper.

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
- `--json` prints machine-readable output.
- `--redact` masks the cell ID and TAC (serving and neighbour cells). Use it before you paste output anywhere public: with the operator code, those two values locate you to within a few hundred metres in public cell databases.
- `--watch [SECONDS]` refreshes every 2 s (or the interval you give) with a signal bar. It's handy for aiming antennas. Ctrl-C stops it.

If `status` reports no cell at all, check the antenna pigtails before anything else — that was the cause on our own unit; see the [Dell guide's troubleshooting section](../docs/dell-dw5931e-usb.md#no-cells-at-all-cesq-all-99255-gtccinfo-empty) rather than repeating that story here.

`doctor` reads the settings that matter on OEM modules and explains them: firmware image (`_5025` = Dell DW5931e), DIPC mode, FCC lock, `GTFMODE`, USB mode, RAT mode, antenna tuner, radio and SIM state, and whether any cell is measured. It prints one `[OK]`, `[WARN]` or `[INFO]` line per check and exits 1 if anything is a warning. It only sends read commands.

Signal values are decoded per 3GPP TS 27.007, reporting the lower bound of each range the modem returns (the router's `fm350-status` uses the same convention).

## Running a data session: `up`

With the helper installed, `up` (and `up --loopback`) need no root at all:
they create the `utun` interface and set routes/DNS through the helper (see
"Privilege separation" below). Without it, pass `--no-helper` and run with
`sudo`, the same as before privilege separation existed. `--dry-run` never
needs root or the helper, with either mode.

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
see `rndis_device.py`), `up` doesn't give up immediately: it leaves the utun
interface and routes as they are, polls for the modem to come back for up to
`--reenum-timeout` seconds (default 180), waits 15s for its firmware to
settle, and rebuilds the whole AT/RNDIS/bridge session. If the modem doesn't
come back in time, `up` exits with status 3. Any other fatal bridge failure
(or a device loss with `--no-supervise`) exits with status 2.

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
`RunAtLoad`/`KeepAlive` both false: it only runs while `up` is actually
connected to it (launchd starts it on the first connection, via the plist's
`Sockets` entry with `SockPathOwner` set to the installing user's uid, so
the unprivileged main process can open a socket that launchd itself created
as root).

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

- The real data path (`up` against actual hardware) hasn't been run live
  yet -- we're waiting on a data SIM. Everything about it is verified either
  by `up --loopback` against the in-process fake modem, not
  against the real FM350-GL.
- Throughput hasn't been measured with `iperf3`; the ~150 Mbps async design
  target (see [`../docs/macos-driver.md`](../docs/macos-driver.md)) is an
  estimate, not a result.
- No IPv6 support yet (`IPV4V6` PDP context, router advertisements from the
  modem).
- macOS/Apple Silicon only, by design (see the rejected alternatives in
  [`../docs/macos-driver.md`](../docs/macos-driver.md)): no Linux/Windows
  support, no kernel extension, no DriverKit dext.
- DHCP on the FM350's RNDIS interface is unreliable; `fm350mac` always
  assigns the IP itself from `AT+CGPADDR` instead of relying on DHCP.

## Glossary

Terms used on this page, defined in the [shared glossary](../docs/glossary.md): [APN](../docs/glossary.md#apn), [AT command](../docs/glossary.md#at-command), [Band / EARFCN / PCI](../docs/glossary.md#band--earfcn--pci), [Cell ID / TAC](../docs/glossary.md#cell-id--tac), [DIPC mode](../docs/glossary.md#dipc-mode), [FCC lock](../docs/glossary.md#fcc-lock), [LaunchDaemon](../docs/glossary.md#launchdaemon), [libusb](../docs/glossary.md#libusb), [OEM image](../docs/glossary.md#oem-image), [PDP context / data session](../docs/glossary.md#pdp-context--data-session), [RAT](../docs/glossary.md#rat), [RNDIS](../docs/glossary.md#rndis), [RSRP / RSRQ / SINR](../docs/glossary.md#rsrp--rsrq--sinr), [utun](../docs/glossary.md#utun).
