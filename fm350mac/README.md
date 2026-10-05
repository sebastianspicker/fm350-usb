# fm350mac

> **Alpha software (0.1.0a1).** One modem, one SIM, one Mac so far. Expect
> rough edges, and read [Limitations](#limitations) and
> [the trust model](#privilege-separation-and-the-trust-model) before you
> install. Do not rely on it as your only internet connection.

A user-space macOS data path for a Fibocom FM350-GL 5G modem: USB RNDIS
(libusb, via our own ctypes binding in `usb_async.py`, no pyusb) bridged to a
macOS `utun` interface, with no kernel extensions, DriverKit entitlements or
SIP changes. It's for anyone bench-testing an FM350-GL / Dell DW5931e on a
Mac, either as a throwaway uplink or to check SIM, registration and
throughput before the module goes on a router.

For the architecture, session flow and rationale, see the
[design notes](https://github.com/sebastianspicker/fm350-usb/blob/v0.1.0a1/docs/macos-driver.md).
Project-wide context is in the
[repository README](https://github.com/sebastianspicker/fm350-usb/tree/v0.1.0a1#readme).

## In short

- `fm350mac` is a pure-Python program (no dependencies besides libusb) that talks to the FM350-GL's USB RNDIS interface directly and hands the packets to a macOS `utun` interface.
- USB access itself needs no root. A small root helper, installed once as a LaunchDaemon, does only the part that does need root (creating the `utun` interface, setting routes and DNS), so the driver runs as you.
- Commands that need no root (`probe`, `status`, `doctor`, `at`, `connect`, `disconnect`) and the data session (`up`, with the helper installed) work against real hardware.

## What was verified

Verified on hardware on 2026-10-05: FM350-GL / Dell DW5931e, Telekom DE SIM,
LTE B3/B7, macOS 27, Apple Silicon.

| Area | State |
|---|---|
| USB enumeration, AT access, `status`, `doctor`, `probe` | Verified |
| `up --route-host` with ping, HTTPS and capped `iperf3` | Verified. 50 MB transfers on LTE B8 (RSRP -94 dBm): 42.1 Mbit/s down, 34.7 Mbit/s up, 0 retransmits, driver CPU 13-23%. On a weak cell: 10-20 Mbit/s down, 7-15 Mbit/s up |
| `helper install` / `helper status` | Verified |
| Teardown (routes removed, PDP context deactivated) | Verified |
| `--default-route`, `--dns` | **Not verified on hardware** |
| 10-minute idle session | Verified: 40/40 pings, 122 keepalive acknowledgements, no watchdog false alarm |
| Recovery after unplugging and replugging the modem | Verified: detected in about 1 s, session rebuilt automatically 57 s after the unplug |
| Non-blocking utun read path | Verified (all hardware runs above) |
| Sessions longer than 10 minutes; keepalive watchdog, stall detection and rebuild retry actually firing; RNDIS-level rebuild | **Not verified on hardware** |
| Intel Macs | **Not verified** (the libusb loader also looks in `/usr/local`, but nobody has tried it) |

## Limitations

- **IPv4 only.** There is no IPv6 data path. `up` accepts only `--pdp IP`.
- **One modem only.** Two FM350s on one Mac are not supported.
- **`--io sync` is a frozen fallback and unsupported in 0.1.0a1.** It had one unexplained download stall in 6 runs. The default is `--io async`.
- **`--default-route` and `--dns` are experimental.** They have not run against a real modem; see the metered-SIM warning below.
- **Throughput is radio-limited and only measured briefly.** The numbers above come from short, capped runs on a weak cell. Treat them as a floor, not a capacity figure. No speed target is claimed.
- **Intel Macs are untested.**
- **macOS counts received packets twice** in a utun's `netstat -ib` byte counters (a 1 MB download showed about 2.1 MB). Use the `stats:` line, see below.
- **DHCP on the FM350's RNDIS interface is unreliable.** `fm350mac` takes the IP from `AT+CGPADDR` and assigns it itself.
- **macOS only, by design.** No Linux or Windows support, no kernel extension, no DriverKit dext.

## Requirements

- macOS (developed and tested on macOS 27, Apple Silicon)
- Python >= 3.11 (`uv tool install` fetches one if needed)
- [uv](https://docs.astral.sh/uv/) (or `pipx`)
- libusb: `brew install libusb`
- For the helper: Apple's `/usr/bin/python3` (it ships with the Command Line Tools; if `helper install` says it is the Command Line Tools stub, run `xcode-select --install`)

## Install from zero

```sh
brew install libusb
uv tool install "git+https://github.com/sebastianspicker/fm350-usb@v0.1.0a1#subdirectory=fm350mac"
# or: pipx install "git+https://github.com/sebastianspicker/fm350-usb@v0.1.0a1#subdirectory=fm350mac"
fm350mac --version
```

The `v0.1.0a1` tag is created when the release is published. Until then,
install from the branch with `@alpha-0.1` instead of `@v0.1.0a1`. Do not
install without a ref: the default branch (`main`) is still 0.1.0.

If `fm350mac: command not found` after `uv tool install`, run
`uv tool update-shell` and restart the shell.

From a clone (development setup):

```sh
git clone https://github.com/sebastianspicker/fm350-usb
cd fm350-usb/fm350mac
uv sync
uv run fm350mac --help        # or .venv/bin/fm350mac
```

### Install the root helper (once)

`up` needs the helper unless you pass `--no-helper` and run as root. Read
[the trust model](#privilege-separation-and-the-trust-model) first: this step
runs the package as root once.

```sh
# 1. See what it would do and print the helper's sha256. Needs no sudo;
#    --allowed-uid is required without sudo.
fm350mac helper install --dry-run --allowed-uid "$(id -u)"

# 2. Compare the printed "helper sha256" with the value in the release notes
#    and the CHANGELOG, then install with it pinned. Use the absolute path of
#    the installed command.
sudo "$(command -v fm350mac)" helper install --expect-sha256 <sha256>

# 3. Check.
fm350mac helper status
```

`--expect-sha256 HEX` makes `helper install` refuse (`helper sha256 mismatch:
expected ..., got ...; refusing to install`) unless the helper file's sha256
is `HEX`. `helper status` also prints `helper sha256 :` for the installed
file; that is a consistency check against what you installed, not an
integrity proof (see [the trust model](#privilege-separation-and-the-trust-model)).

Never use `sudo uv run fm350mac ...`: it runs uv as root and leaves
root-owned files in the project's `.venv`. From a clone, use the absolute
path of the venv's command instead: `sudo "$PWD/.venv/bin/fm350mac" helper install`.

`helper install` takes the uid that may use the helper from `$SUDO_UID` (the
user who ran `sudo`); `--allowed-uid UID` overrides it. It prints every step
before doing it and refuses to continue if `/usr/bin/python3` is missing, is
not root-owned, or if the helper file does not compile under it.

After upgrading `fm350mac`, run `helper install` again: `up` warns when the
installed helper's version differs from the driver's, and `helper status`
says "installed helper is out of date" when the installed file differs from
the packaged one.

## First run

Plug in the modem (with antennas and a SIM) and work up from the commands
that need no root:

```sh
fm350mac doctor                 # read-only checks, one [OK]/[WARN]/[INFO] line each
fm350mac status --redact        # SIM, registration, signal, serving cell (cell ID/TAC masked)
fm350mac up --apn <apn> --route-host <ip>
```

Then, in a second terminal, send traffic to the host you routed:

```sh
ping <ip>
```

`<ip>` is a single IPv4 address (for example a public DNS resolver or a host
you control). Only traffic to that address goes through the modem. Ctrl-C
stops the session cleanly: routes are removed, the bridge stops, the PDP
context is deactivated, RNDIS is halted. On shutdown `up` prints a `stats:`
line and a `perf:` line (see [Measuring real data usage](#measuring-real-data-usage)).

If the `status` output says there is no cell at all, check the antenna
pigtails before anything else; see the
[Dell guide's troubleshooting section](https://github.com/sebastianspicker/fm350-usb/blob/v0.1.0a1/docs/dell-dw5931e-usb.md#no-cells-at-all-cesq-all-99255-gtccinfo-empty).

## Metered SIM warning

`up` without `--route-host` and without `--default-route` routes nothing
through the modem (it logs "no routes requested: nothing will use the
tunnel").

- `--route-host IP` (repeatable, at most 8, unicast IPv4) adds a host route `IP -> utunN`. Only traffic to those hosts uses the SIM. This is the safe way to test on a metered plan.
- `--default-route` saves the current default route and replaces it with one through the tunnel. **From then on every connection of the Mac goes over the cellular link:** system updates, cloud sync, backups, browser tabs, everything. It is experimental and unverified on hardware. The saved default route is restored on shutdown.
- `--dns` publishes the modem's DNS servers through `scutil` and requires `--default-route` (without it, the carrier's resolver would be queried over your normal uplink, so `up` refuses). Also unverified on hardware.

### Measuring real data usage

The real usage of the SIM is the `stats:` line `up` logs on shutdown:

```
stats: rx=<packets>/<bytes>B tx=<packets>/<bytes>B drops=<n> tx_stalls=<n>
```

It counts IP bytes that crossed the tunnel. macOS's own utun counters
(`netstat -ib`) count received bytes twice, so do not use them. The
carrier's accounting may differ somewhat from IP bytes.

A second line, `perf: ...`, summarises USB transfer latency and pool use for
tuning. See "Performance and tuning" in the
[design notes](https://github.com/sebastianspicker/fm350-usb/blob/v0.1.0a1/docs/macos-driver.md#performance-and-tuning).

## Commands

```sh
fm350mac probe                       # RNDIS init + query OIDs, then halt (no root)
fm350mac at 'ATI' 'AT+CPIN?'         # send raw AT commands (no root); --redact masks identifiers
fm350mac status                      # SIM/registration/cell/thermal status (no root)
fm350mac doctor                      # read-only diagnostic checks (no root)
fm350mac connect --apn internet      # define + activate a PDP context (no root)
fm350mac disconnect                  # deactivate the PDP context (no root)
fm350mac up --apn internet --route-host 1.1.1.1   # the full session
sudo "$(command -v fm350mac)" up --no-helper --apn internet --route-host 1.1.1.1   # the same without the helper: runs everything as root
fm350mac up --loopback               # smoke test with an in-process fake modem, no SIM
fm350mac async-selftest --yes        # live check of the async USB pools; ends with a USB reset
fm350mac helper install|uninstall|status
```

`--verbose` (before the subcommand) enables debug logging and tracebacks.

### `status` and `doctor`

`status` prints a readable snapshot: SIM, LTE and NR registration, operator
and access technology, serving cell (band, EARFCN, PCI, RSRP, RSRQ), NR signal
when the modem measures a 5G carrier, neighbour cells by band, and
temperature.

- `--raw` also prints the AT responses as the modem sent them.
- `--json` prints machine-readable output (not combinable with `--watch`).
- `--redact` masks the cell ID and TAC. Use it before you paste output anywhere public: with the operator code, those two values locate you to within a few hundred metres in public cell databases. With `--raw` it also masks phone numbers, IMSI/IMEI/ICCID and IP addresses.
- `--watch [SECONDS]` refreshes every 2 s (or the interval you give). Handy for aiming antennas; Ctrl-C stops it.

`doctor` reads the settings that matter on OEM modules and explains them:
firmware image (`_5025` = Dell DW5931e), DIPC mode, FCC lock, `GTFMODE`, USB
mode, RAT mode, antenna tuner, radio and SIM state, and whether any cell is
measured. It prints one `[OK]`, `[WARN]` or `[INFO]` line per check and exits
1 if anything is a warning. It only sends read commands.

Signal values are decoded per 3GPP TS 27.007, reporting the lower bound of
each range the modem returns.

### `up` options

| Option | Meaning |
|---|---|
| `--apn APN` | APN (required unless `--loopback`) |
| `--route-host IP` | host route through the tunnel; repeatable, max 8 |
| `--default-route` | experimental: route all traffic through the tunnel |
| `--dns` | experimental: publish the modem's DNS via `scutil` (needs `--default-route`) |
| `--dry-run` | no side effects: only read-only AT queries, nothing run on the system; prints the AT and system commands it would run; needs no root and no helper |
| `--redact` | mask the assigned IP, DNS servers and IMEI in logs |
| `--no-helper` | do not use the root helper; run the direct path (needs `sudo`) |
| `--loopback` | in-process fake modem: no SIM, no USB. Answers ARP/ICMP for any destination (addressed as `192.0.2.2`) and adds only a host route to `198.51.100.1`; ignores `--default-route`/`--route-host` |
| `--supervise` / `--no-supervise` | reconnect supervisor (default on) or a plain wait loop |
| `--reenum-timeout SECONDS` | how long to wait for the modem to come back after a USB disconnect (default 180) |
| `--io async\|sync` | async (default) or the frozen, unsupported sync fallback |
| `--rx-urbs N` / `--tx-urbs N` | bulk transfers kept in flight per direction with `--io async` (1..64, default 8) |
| `--cid N`, `--pdp IP` | PDP context id; PDP type (only `IP`) |

With the supervisor on, `up` polls registration and the PDP context every 10 s
with read-only AT commands, reconnects on loss with exponential backoff, and
watches the data path. If the USB device disappears (the FM350's firmware is
known to crash and re-enumerate), `up` removes routes and DNS, waits for the
modem to come back, checks that it is the same modem (IMEI), and rebuilds the
session. The design notes describe these mechanisms in detail. The rules that
decide when `up` gives up or keeps going:

- **Stall rule.** The data path counts as stalled when the modem refuses OUT transfers (at least 20 new tx timeouts) while rx stays flat for 60 s. Traffic the modem accepted but nobody answered (a host that drops ICMP, SYN retries, one-way UDP) is not a stall, and neither is an idle session. The first stall cycles the PDP context; a second one in a row fails the bridge and `up` rebuilds the session.
- **Stall-triggered rebuilds are uncapped by design.** Other bridge failures with the modem still on the bus get at most 3 RNDIS-level rebuilds in a row (then `bridge failed: ... (after 3 rebuilds in a row; giving up)`, exit 2; a session that stays up for 10 minutes resets the count). A rebuild forced by a stall is not counted, so a backup link that is down keeps being retried.
- **Keepalive watchdog.** The bridge sends a keepalive every 5 s and fails if no *successful* acknowledgement arrives for 15 s. An acknowledgement with an error status does not count.
- **RNDIS rebuild settle.** An RNDIS-level rebuild pauses 3 s between halting RNDIS and initialising it again.
- **Rebuild budget.** A failed bring-up during a rebuild is retried (5 s doubling to 60 s) until 600 s have passed since the session was lost, the re-enumeration wait included. After that: `could not rebuild the session within 600s; giving up`, exit 3.
- **utun buffers.** The async bridge raises the utun socket's `SO_SNDBUF` (our writes towards the Mac) to 1 MiB so a burst from the modem does not hit `ENOBUFS`, and leaves `SO_RCVBUF` (the outbound queue) at the system default to avoid added upload latency.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | ok (also: a running session stopped cleanly with Ctrl-C/SIGTERM; for `doctor`, no warnings) |
| 1 | error (printed as `fm350mac: <cause>. <next step>` or a command-specific message; for `doctor`, at least one warning) |
| 2 | bridge failure or usage error |
| 3 | the modem did not come back after a disconnect, or the rebuild budget (600 s, measured from the loss of the session) was exhausted |
| 4 | the modem's identity changed (a different IMEI came back; the session is not rebuilt) |
| 130 | interrupted while bringing the session up or waiting for the modem |

## Privilege separation and the trust model

USB access (libusb claim, RNDIS, AT) never needs root; only creating the
`utun` interface and setting routes/DNS do. `fm350mac` splits those apart.

| | Main process (`fm350mac up`) | Helper (`fm350mac-helper`) |
|---|---|---|
| Runs as | you (no root) | root, via a LaunchDaemon |
| Interpreter | the tool's own Python | `/usr/bin/python3 -I -S` (Apple's, root-owned, isolated mode) |
| Code | everything: USB, RNDIS, AT, bridge, supervisor | one stdlib-only file, `fm350mac/helper/fm350mac_helper.py`, copied to `/usr/local/libexec/fm350mac-helper` (root, 0755) |
| Does | data path | creates the `utun`, passes its file descriptor over a Unix socket, sets address/routes/DNS, undoes all of it when the connection closes, including after `kill -9` of the main process |

The honest version of the trust model:

- **At install time, the whole package runs as root once.** `sudo "$(command -v fm350mac)" helper install` executes the installed `fm350mac` and its Python environment as root to copy one file and write a plist. That interpreter and those packages are owned by you, so anything that can write to them as your user decides what runs as root. **Inspecting the helper file or running `--dry-run` does not limit this**: `sudo ... helper install` runs the user-owned interpreter and packages as root either way, and `--dry-run` runs them too (as you). What helps:
  - Compare the `helper sha256:` that `helper install --dry-run` prints with the value published in the release notes and the [CHANGELOG](https://github.com/sebastianspicker/fm350-usb/blob/v0.1.0a1/CHANGELOG.md), and install with `--expect-sha256 <sha256>`. That pins the one file that stays on the system as root. It does not cover the code that runs during the install itself.
  - When `helper install` runs as root from a user-owned Python, it prints `WARNING: running user-owned code as root (...)`. Take it seriously: whoever can write there controls what the install runs and writes as root.
  - Python run as root can write root-owned `__pycache__` files into your venv or tool environment. They are harmless but can get in the way of a later `uv tool uninstall` or `rm`; remove them with `sudo` if needed.
  - Install only from the release tag, into an environment only you can write to.
- **Afterwards only the copied helper file runs as root**, under `/usr/bin/python3 -I -S`, so nothing user-writable (your Homebrew prefix, the tool's environment, any dependency) is on its import path. `install` also refuses unsafe directories (symlinks, non-root owners, group/other-writable) under the install path.
- **The helper does what `up` needs, as root, for one user.** The socket `/var/run/fm350mac-helper.sock` is mode 0600 and owned by the installing uid, and the helper additionally checks the peer's credentials against that uid. Any process running as that user can therefore ask the helper to create a utun, set an address, add up to 8 host routes, swap the default route and set DNS. It runs fixed commands (`/sbin/ifconfig`, `/sbin/route`, `/usr/sbin/scutil`) with validated arguments and no shell, and it cannot run arbitrary code or commands. Treat the account that may use the helper accordingly: a hostile process running as you could redirect your traffic.
- **Nothing is signed or notarized.** The installed helper is a plain file. `helper status` prints its sha256 and compares it byte for byte with the packaged one; that is a consistency check, not an integrity proof, because both come from the same user-owned environment.
- The helper reports its version; `up` warns when it differs from the driver's.
- `--no-helper` runs everything, including your whole Python environment, as root. Prefer the helper.

### What gets installed

| What | Where |
|---|---|
| Helper | `/usr/local/libexec/fm350mac-helper` (root, 0755) |
| LaunchDaemon plist | `/Library/LaunchDaemons/de.fm350mac.helper.plist` |
| Socket | `/var/run/fm350mac-helper.sock` (0600, owned by the installing uid), created by launchd |
| Log | `/var/log/fm350mac-helper.log` |

The plist has `RunAtLoad` and `KeepAlive` both off: launchd starts the helper
on the first connection to the socket (the first start of `/usr/bin/python3`
can take a couple of seconds). Once started it stays resident and idle until
you uninstall it or reboot, which is why `helper install` first runs
`launchctl bootout` to replace a running helper.

### If `up` is killed (`kill -9`)

With the helper, nothing to do: closing the connection makes the helper undo
everything it configured for it (DNS, routes, address, interface) in reverse
order. If a default route is somehow left behind, see
[Troubleshooting](#troubleshooting). With `--no-helper`, teardown is
best-effort and does not survive `SIGKILL`.

## Uninstall / remove everything

Stop a running `fm350mac up` first (Ctrl-C in its terminal).

```sh
sudo "$(command -v fm350mac)" helper uninstall     # preview first with: fm350mac helper uninstall --dry-run
uv tool uninstall fm350mac                          # or: pipx uninstall fm350mac
brew uninstall libusb                               # optional: other tools may use it
```

If you only ever used `--no-helper` (run as `sudo "$(command -v fm350mac)" up --no-helper ...`),
there is no helper to remove: skip the first command.

`helper uninstall` boots out the LaunchDaemon, then removes the plist, the
helper and its log file. It leaves the directory `/usr/local/libexec` alone.
It exits 1 and prints `uninstall incomplete: the files were removed, but see
the errors above.` when:

- `launchctl bootout` fails for a reason other than "not loaded" (`launchctl bootout failed: ... (removing the files anyway)`);
- the job is still loaded after the files are gone (`... it is still loaded. Run 'sudo /bin/launchctl bootout system/de.fm350mac.helper' or reboot.`).

If the socket is left behind it also prints `WARNING: the helper socket /var/run/fm350mac-helper.sock still exists`;
remove it by hand once the job is gone.

**Manual fallback** (if the command is gone or fails). As an administrator,
first unload the LaunchDaemon with
`sudo launchctl bootout system/de.fm350mac.helper`; an error saying it is not
loaded is fine. Then remove these files with `sudo rm`:
`/Library/LaunchDaemons/de.fm350mac.helper.plist`,
`/usr/local/libexec/fm350mac-helper` and `/var/log/fm350mac-helper.log`. If
`/var/run/fm350mac-helper.sock` still exists after the bootout, remove it too.

**Check that nothing is left:**

```sh
launchctl print system/de.fm350mac.helper    # should fail: could not find service
ls -l /Library/LaunchDaemons/de.fm350mac.helper.plist /usr/local/libexec/fm350mac-helper \
      /var/log/fm350mac-helper.log /var/run/fm350mac-helper.sock   # all: No such file or directory
netstat -rn | grep -i utun                   # no routes via a utun you did not expect
scutil --dns                                 # no resolver pointing at the modem's DNS servers
```

## Troubleshooting

Messages are printed to stderr. Run with `--verbose` (before the
subcommand) for details and tracebacks.

| Message (start) | Cause | What to do |
|---|---|---|
| `libusb not found. Install it: brew install libusb` | libusb is not installed or not in `/opt/homebrew` or `/usr/local` | `brew install libusb` |
| `FM350 (0e8d:7126/7127) not found. ...` | modem not enumerated, or in a different USB mode | check cable, adapter and power; `system_profiler SPUSBDataType` must list it; see the [Dell guide](https://github.com/sebastianspicker/fm350-usb/blob/v0.1.0a1/docs/dell-dw5931e-usb.md) |
| `the modem is in use by another process ...` | another tool (for example `fm350_diag`) or a second `fm350mac up` holds the USB interfaces | close it and retry |
| `USB error: ...` | any other libusb failure | replug the modem and retry; `--verbose` for details |
| `unknown AT interface for USB product id ... The modem is in an unsupported USB mode ...` | the USB product id has no known AT interface | pass `--iface N` to `fm350mac at` |
| `<cmd>: timed out waiting for a final result code` (`at`; exit 1) | the modem sent no `OK`/`ERROR` for that command: wedged or gone | unplug and replug the modem |
| `status: no final result code for 'AT...' within Ns` (also `doctor:`, `connect:`, `disconnect:`; exit 1) | the same, in those commands. `status --watch` prints `status: ...; retrying` and keeps going. `connect`'s PDP setup step prints the text without the `connect:` prefix | unplug and replug the modem |
| `modem did not answer: no final result code for 'AT...' within Ns` (`up`; exit 1) | an AT query during `up` timed out | unplug and replug the modem |
| `the modem did not answer (...)` | the same condition in other commands | unplug and replug the modem |
| `RNDIS control channel did not respond; unplug/replug the modem` | RNDIS control exchange timed out | unplug and replug |
| `no SIM detected: check the SIM tray` | `AT+CPIN?` reports NOT INSERTED | reseat the SIM |
| `SIM is PIN-locked: unlock it in a phone first (or disable the PIN there), or with fm350mac at 'AT+CPIN="<pin>"' -- note that the PIN then ends up in your shell history; 3 wrong PINs lock the SIM` | SIM PIN is set | preferably unlock or disable the PIN in a phone. Typing it into `fm350mac at` works, but the shell keeps it in its history (the tool masks it in its own output) |
| `SIM is PUK-locked: unlock with the PUK in a phone` | too many wrong PINs | unlock in a phone |
| `SIM not ready (...)` | any other SIM state | `fm350mac status`, `fm350mac doctor` |
| `AT+CGDCONT failed: ...` / `AT+CGACT=1,1 failed: ...` | the modem refused the PDP context (wrong APN, no registration, no coverage) | check `--apn`, run `fm350mac status` |
| `no IP address assigned` | context active but no address | check the APN and registration |
| `no IPv4 address assigned (IPv6-only context; ...)` | the carrier gave only IPv6 (`connect`) | use an IPv4 APN; the data path is IPv4-only |
| `fm350mac up needs either the root helper or root. ...` | helper not installed or unreachable, and not running as root | install the helper (above), or pass `--no-helper` with `sudo` |
| `fm350mac up --no-helper needs root: re-run it with sudo.` | `--no-helper` without root | `sudo "$(command -v fm350mac)" up --no-helper ...` |
| `helper: the helper is not installed (no socket); run: sudo "$(command -v fm350mac)" helper install` | no socket at `/var/run/fm350mac-helper.sock` | run the install command |
| `helper: the helper is not running (connect timed out); see /var/log/fm350mac-helper.log` | launchd did not accept the connection in time | read the log; retry (the first start of `/usr/bin/python3` can take a few seconds); reinstall |
| `helper: the helper is not running (connection refused); see /var/log/fm350mac-helper.log` | launchd could not start the helper | read the log; reinstall |
| `helper: the helper is not running (hello timed out); see /var/log/fm350mac-helper.log` | connected, but the helper did not answer the `hello` | read the log; retry; reinstall |
| `helper: permission denied connecting to /var/run/fm350mac-helper.sock (socket owner uid N, your uid M); if the uids match, the connection was blocked by a sandbox/privacy setting` | the socket belongs to another uid, or something sandboxes the process. If N differs from M the text continues `; the helper was installed for another user: reinstall it as this user with sudo "$(command -v fm350mac)" helper install` | if the uids differ, reinstall as the user who will run `up`; if they match, run `up` from a terminal that is not sandboxed |
| `helper: helper protocol mismatch: ...; reinstall the helper` | old helper, new driver | `sudo "$(command -v fm350mac)" helper install` |
| `helper: the helper did not answer correctly: ...` | garbled reply | see the log; reinstall |
| `helper error: <reason>` | the helper refused a request (for example more than 8 host routes) or closed the connection mid-session. Reinstalling would not change it | read the reason; `fm350mac helper status` |
| `helper error: <reason>. Check fm350mac helper status; reinstall with sudo "$(command -v fm350mac)" helper install` | the reason is a protocol or version mismatch (old helper, new driver) | reinstall the helper |
| `installed helper is X, driver is Y: run sudo ... helper install` (warning) | helper and driver versions differ | reinstall the helper |
| `fm350mac helper install must be run with sudo.` | not root | use `sudo "$(command -v fm350mac)" helper install` |
| `no $SUDO_UID ... pass --allowed-uid <uid> explicitly` | no sudo, for example with `--dry-run` | add `--allowed-uid "$(id -u)"` |
| `/usr/bin/python3 not found; refusing to install without the system Python` / `/usr/bin/python3 isn't root-owned (uid N); refusing to install` | the system Python is missing or has been replaced | restore Apple's `/usr/bin/python3`; the helper is not installed without it |
| `/usr/bin/python3 is the Command Line Tools stub, not a real Python: run xcode-select --install first` | the Command Line Tools are not installed | `xcode-select --install`, then retry |
| `helper sha256 mismatch: expected X, got Y; refusing to install` | the packaged helper differs from the `--expect-sha256` value | do not install. Check that you installed the release tag and that the value is the one published for it |
| `WARNING: running user-owned code as root (...)` | `helper install` is run as root from a Python you own | expected; see [the trust model](#privilege-separation-and-the-trust-model), and use `--expect-sha256` |
| `launchctl bootout failed: ... (removing the files anyway)` / `... it is still loaded. Run 'sudo /bin/launchctl bootout system/de.fm350mac.helper' or reboot.` (`helper uninstall`, exit 1) | launchd would not unload the helper | run the command shown, or reboot |
| `WARNING: the helper socket /var/run/fm350mac-helper.sock still exists` | the socket survived the uninstall | remove it by hand once the job is unloaded |
| `launchctl bootstrap failed: ...` | launchd refused the plist | re-run `helper install`; check `launchctl print system/de.fm350mac.helper` |
| `--dns requires --default-route: ...` | `--dns` alone would query the carrier's resolver over your normal uplink | add `--default-route`, or drop `--dns` |
| `--route-host: at most 8 hosts are supported` | too many `--route-host` | use at most 8 |
| `modem identity changed: expected IMEI ... (exit 4)` | after re-enumeration a different IMEI answered | check which modem is attached; restart `up` |
| `modem did not re-enumerate within Ns; giving up` (exit 3) | the modem did not come back | replug; raise `--reenum-timeout` |
| `could not rebuild the session within 600s; giving up (last error: ...)` (exit 3) | rebuild retries ran out | replug, check the SIM and signal, restart `up` |
| `bridge failed: ...` (exit 2) | the data path failed (also after 3 rebuilds in a row; stall-triggered rebuilds are not counted) | replug and restart; `--verbose` |
| `RNDIS: device max_transfer_size ... too small ...` | unexpected RNDIS parameters | report it, with `--verbose` output |
| `async-selftest ends with a USB reset ... Re-run with --yes` | safety prompt | add `--yes` |
| `interrupted; shutting down` (exit 130) | Ctrl-C before the session was up | none |

### A default route was left behind

This should not happen with the helper (it restores the default route when
the connection closes) and is more likely after `--no-helper` plus `kill -9`,
or if the helper itself was killed. Symptoms: no internet, and
`netstat -rn | grep default` shows a `utunN` default route.

1. Check: `netstat -rn | grep default` and `route -n get default`.
2. Remove the stale route: `sudo route delete default`. If macOS does not rebuild a default route by itself, switch Wi-Fi (or your Ethernet service) off and on, or re-add it by hand: `sudo route add default <gateway>`, using the gateway from your network settings.
3. If `--dns` was used, remove the DNS key: `sudo scutil` and enter `remove State:/Network/Service/fm350mac/DNS`, then `quit`.
4. Check again with `netstat -rn` and `scutil --dns`.

## For contributors

```sh
cd fm350mac
uv sync
uvx ruff check . ../tools
```

The helper file must run under the system `/usr/bin/python3` (3.9, `-I -S`),
so it stays a single stdlib-only file.

## Glossary

Terms used on this page, defined in the [shared glossary](https://github.com/sebastianspicker/fm350-usb/blob/v0.1.0a1/docs/glossary.md): [APN](https://github.com/sebastianspicker/fm350-usb/blob/v0.1.0a1/docs/glossary.md#apn), [AT command](https://github.com/sebastianspicker/fm350-usb/blob/v0.1.0a1/docs/glossary.md#at-command), [Band / EARFCN / PCI](https://github.com/sebastianspicker/fm350-usb/blob/v0.1.0a1/docs/glossary.md#band--earfcn--pci), [Cell ID / TAC](https://github.com/sebastianspicker/fm350-usb/blob/v0.1.0a1/docs/glossary.md#cell-id--tac), [DIPC mode](https://github.com/sebastianspicker/fm350-usb/blob/v0.1.0a1/docs/glossary.md#dipc-mode), [FCC lock](https://github.com/sebastianspicker/fm350-usb/blob/v0.1.0a1/docs/glossary.md#fcc-lock), [LaunchDaemon](https://github.com/sebastianspicker/fm350-usb/blob/v0.1.0a1/docs/glossary.md#launchdaemon), [libusb](https://github.com/sebastianspicker/fm350-usb/blob/v0.1.0a1/docs/glossary.md#libusb), [OEM image](https://github.com/sebastianspicker/fm350-usb/blob/v0.1.0a1/docs/glossary.md#oem-image), [PDP context / data session](https://github.com/sebastianspicker/fm350-usb/blob/v0.1.0a1/docs/glossary.md#pdp-context--data-session), [RAT](https://github.com/sebastianspicker/fm350-usb/blob/v0.1.0a1/docs/glossary.md#rat), [RNDIS](https://github.com/sebastianspicker/fm350-usb/blob/v0.1.0a1/docs/glossary.md#rndis), [RSRP / RSRQ / SINR](https://github.com/sebastianspicker/fm350-usb/blob/v0.1.0a1/docs/glossary.md#rsrp--rsrq--sinr), [utun](https://github.com/sebastianspicker/fm350-usb/blob/v0.1.0a1/docs/glossary.md#utun).
