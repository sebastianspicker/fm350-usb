# fm350mac: our own macOS data path for the FM350-GL

Design notes for `fm350mac`, a user-space macOS data path for the Fibocom
FM350-GL 5G modem: the architecture, the alternatives we rejected, and why.
For the package itself (install, commands, tests), see
[fm350mac/README.md](../fm350mac/README.md).

This is a design record, written for the technically curious and for anyone
contributing to `fm350mac`. It keeps full technical depth; if you only want
to run the tool, the [fm350mac README](../fm350mac/README.md) is the shorter,
task-focused door in.

## In short

- macOS ships no driver for RNDIS, the only USB data mode the FM350-GL
  offers. Something has to speak RNDIS to the modem and hand the resulting
  IP packets to macOS — that something is `fm350mac`.
- **Chosen approach (option 5 below):** talk to the modem directly over USB
  with libusb, and exchange IP packets with macOS through a `utun` interface
  (the same kind of virtual network device VPNs use), doing the
  Ethernet‑framing and ARP work ourselves in user space. No kernel
  extension, no DriverKit entitlement, no SIP changes.
- Goal: IP connectivity from the FM350-GL on macOS (Apple Silicon, macOS 27)
  without third-party drivers, kernel extensions or SIP changes, using only
  code in this repo. Main use: bench-testing SIM, registration and
  throughput before the dongle goes on the Flint 2, and as an ad-hoc Mac
  uplink. The production failover path is still the router (see
  [setup-guide.md](setup-guide.md)).
- Status (2026-09-25): the scaffold is implemented and reviewed. 101 unit
  tests pass (327 by 2026-09-26) and `fm350mac probe` (USB enumeration + RNDIS init) works
  against real hardware. The actual data path (`up`) has **not run live
  yet** — it needs a SIM and root (or the root helper described below).
- Biggest caveat: everything about live throughput, including the ~150 Mbps
  figure used below as a design threshold, is an estimate, not a
  measurement.

## Facts measured on this Mac

| Fact | Value | Source |
|---|---|---|
| USB modes available | only 40/41 (both RNDIS); no ECM/NCM | `AT+GTUSBMODE=?` → `(40,41)` |
| macOS built-in USB net drivers | ECM, NCM only | `kextstat`, `/System/Library/DriverExtensions` |
| RNDIS control iface | 0, class `02/02/ff`, interrupt IN `0x82` (64 B) | USB descriptors |
| RNDIS data iface | 1, class `0a/00/00`, bulk IN `0x81` / OUT `0x01` (1024 B, SuperSpeed) | USB descriptors |
| libusb can claim ifaces 0+1 without root | yes | probe script |
| `REMOTE_NDIS_INITIALIZE` | OK, RNDIS 1.0, medium 802.3 | probe |
| MaxPacketsPerTransfer / MaxTransferSize / alignment | **1** / 2048 / 8 bytes | INITIALIZE_CMPLT |
| Device MAC (`OID_802_3_PERMANENT_ADDRESS`) | `00:00:11:12:13:14` (fixed/fake) | probe |
| Max frame size / link speed / media | 1500 / 1 Gbps / connected (even without SIM) | probe |
| AT port | iface 6, bulk OUT `0x06` / IN `0x87` | `tools/fm350_at.py` |

In plain terms: the modem only offers RNDIS (Microsoft's USB networking
protocol) over USB, macOS only ships drivers for the other two USB
networking types (ECM/NCM), and a normal program can already talk to the
RNDIS interfaces over USB without needing root — only the last step, handing
packets to macOS's own network stack, needs anything privileged.

The data plane is Ethernet framing around what is really an IP pipe. The
network assigns the IP (`AT+CGPADDR`) and DNS (`AT+GTDNS`). DHCP on the
FM350's RNDIS is unreliable.

## Options considered

| # | Approach | Verdict |
|---|---|---|
| 1 | Kernel extension (HoRNDIS-style) | Rejected. Needs Reduced Security on Apple Silicon, breaks on OS updates, kernel panics |
| 2 | DriverKit dext (`USBDriverKit` + `NetworkingDriverKit`) | Rejected for now. Needs Apple-granted DriverKit entitlements and a paid developer account, and system-extension approval. The cleanest end state, but too heavy for this project |
| 3 | Network Extension (`NEPacketTunnelProvider`) | Rejected. Needs the NE entitlement and signing; built for VPNs, awkward for a USB device |
| 4 | libusb + `feth` pair + BPF (L2, TetherKit's approach) | Partial fit. Works without entitlements, but needs `feth` ioctls that aren't in the public SDK, plus BPF and MAC juggling. More moving parts than we need |
| 5 | libusb + `utun` (L3) + userspace ARP/Ethernet shim | **Chosen.** Public, stable API (`PF_SYSTEM`/`utun_control`, what every VPN uses). No entitlements. Root is needed only to create `utun` and set routes. A cellular link is L3 anyway, so we strip or add Ethernet headers ourselves and answer ARP in user space |
| 6 | Only use the router | Still the production plan, but it doesn't give a Mac bench path |

In short: options 1–3 all need something Apple has to grant (a kernel
extension exemption, DriverKit entitlements, or a VPN-style entitlement);
option 4 works but pulls in unsupported APIs for no real benefit here.
Option 5 uses only public, stable APIs and needs root for the smallest
possible piece of the job.

## Architecture (option 5)

```text
           macOS IP stack  (routes, DNS via scutil)
                 │  IP packets (4-byte AF header)
           ┌─────┴──────┐
           │  utunN     │  utun.py  – PF_SYSTEM socket, point-to-point
           └─────┬──────┘
   tx thread ▲   │ ▼  rx thread                 bridge.py
           ┌─────┴──────┐
           │ L2 shim    │  ethernet.py – add/strip Ethernet, learn peer MAC,
           │            │               answer ARP for our IP, drop non-IP
           └─────┬──────┘
           ┌─────┴──────┐
           │ RNDIS      │  rndis.py (pure codec) + rndis_device.py (init/query/set/halt,
           │            │               keepalive, PACKET_MSG framing)
           └─────┬──────┘
           ┌─────┴──────┐
           │ USB (libusb│  usb_transport.py – claim ifaces 0/1, ctrl encapsulated
           │  via pyusb)│               cmd/resp, interrupt notify, bulk in/out
           └─────┬──────┘
                 │                    control plane: at.py on iface 6
               FM350  ◄────────────── CGDCONT / CGACT / CGPADDR / GTDNS
```

> **Note (stale text, C2):** the bottom box above still reads "USB (libusb
> via pyusb)". That reflects the original scaffold. pyusb has since been
> replaced everywhere by our own ctypes binding to libusb, `usb_async.py`
> (see "Async USB I/O" below) — the [fm350mac README](../fm350mac/README.md)
> is explicit that the current code uses "our own ctypes binding
> (`usb_async.py`) -- no pyusb". The rest of the diagram (RNDIS, the L2 shim,
> utun) is unchanged by that rewrite.

### Session flow (`sudo fm350mac up --apn <apn>`)

1. **AT (iface 6):** `AT+CPIN?` = READY → `AT+CGDCONT=1,"IP","<apn>"` → `AT+CGACT=1,1` → `AT+CGPADDR=1` → our IPv4 → `AT+GTDNS=1` → DNS servers.
2. **RNDIS (ifaces 0/1):** INITIALIZE (MaxTransferSize 0x4000) → query MAC/MTU → SET `OID_GEN_CURRENT_PACKET_FILTER` = directed | multicast | broadcast (0x0B).
3. **utun:** open → `ifconfig utunN inet <ip> <peer> mtu 1500 up`, with peer = a placeholder like `<ip>` (point-to-point; the gateway is implicit).
4. **Routes:** with `--route-host IP` (repeatable, max 8; unicast IPv4 only), add a host route `IP -> utunN` for each (`route add -host IP -interface utunN`); this is the way to test on a metered SIM, since only those hosts use the tunnel. With `--default-route`, save the current default → `route add default -interface utunN`. Everything is removed on exit, routes and DNS first, and also while waiting for the modem to re-enumerate (so the Mac is never left routing into a dead utun); they are re-added after the rebuild.
5. **DNS:** the modem's DNS servers are always queried and logged (`DNS=[...]` or `DNS=<none returned>`). Only with `--dns` is a `State:/Network/Service/fm350mac/DNS` key published via `scutil` (removed on exit). `--dns` requires `--default-route`: without it the carrier resolver would be queried over the normal uplink, so `up` refuses. If `--dns` is given but the modem returned no servers, a warning is logged and DNS is left unchanged.

`up --dry-run` has no side effects: it sends only read-only AT queries (`CPIN?`, `CGSN`, `CGACT?`, `CGPADDR`, `GTDNS`; no `CGDCONT`/`CGACT` writes), does no RNDIS init/halt and runs no system command. It prints the AT commands and system commands it would run (using a placeholder address if the PDP context isn't already active).

Shutdown order (SIGINT/SIGTERM/SIGHUP handlers are installed before the first network change): routes/DNS, then the bridge, then AT deactivate, then RNDIS halt.
6. **Pump:** rx thread = bulk IN → RNDIS PACKET_MSG decode → Ethernet strip / ARP reply → utun write. tx thread = utun read → Ethernet wrap (dst = learned peer MAC, fallback broadcast; src = device MAC) → PACKET_MSG → bulk OUT.
7. **Keepalive:** answer device `KEEPALIVE_MSG` with `KEEPALIVE_CMPLT`, and send our own every 5 s on the control channel.
8. **Teardown (SIGINT/SIGTERM/SIGHUP):** remove DNS key and restore routes, stop the bridge, `AT+CGACT=0,1`, RNDIS HALT, release interfaces, then close utun.

> **Note (stale text, C3):** the heading above still shows `sudo fm350mac
> up`. That was accurate before privilege separation existed. With the root
> helper installed (see "Privilege separation" below), `up` needs no `sudo`
> at all — the helper does only the utun/route/DNS steps, as root, on
> request [fm350mac README]. `--no-helper` keeps the original `sudo`
> behaviour shown here as a fallback. The AT/RNDIS/pump steps themselves
> never needed root.

### Throughput expectation

The device takes 1 packet per transfer, so throughput is bound by packets
per second. Python with two threads is enough for bench tests; libusb calls
release the GIL through ctypes. The first milestone is correctness, then we
measure with `iperf3`. If the result is well under about 150 Mbps and that
matters, port only `bridge.py` and the USB hot path to Swift or Rust behind
the same interfaces. The control plane stays in Python.

## Package layout

```text
fm350mac/                 Python ≥3.11 project, uv-managed (pyproject.toml)
  src/fm350mac/
    rndis.py              constants, message encode/decode, PACKET_MSG pack/unpack (pure)
    ethernet.py           Ethernet header add/strip, ARP parse/reply (pure)
    usb_transport.py      device discovery + libusb backend, iface claim, endpoints
    rndis_device.py       RNDIS control state machine on top of usb_transport
    at.py                 AT command channel (moved from tools/fm350_at.py)
    utun.py               utun open/read/write (AF header handling)
    netconfig.py          ifconfig/route/scutil wrappers, dry-run capable, restore on exit
    bridge.py             rx/tx threads, stats, shutdown
    cli.py                `fm350mac probe|at|status|doctor|connect|disconnect|up|async-selftest|helper`
  tests/                  pytest: rndis codec, ethernet/ARP, utun framing, netconfig dry-run
```

`probe`, `at` and `status` need no root. `up` needs root (utun + routes).

> **Note (stale text, C2):** this layout describes the original scaffold,
> built directly on pyusb. `usb_transport.py`'s pyusb backend and the rest of
> that USB layer were superseded by the async ctypes binding in
> `usb_async.py`, covered in full in "Async USB I/O" below — see the
> [fm350mac README](../fm350mac/README.md), which confirms the shipped code
> has "no pyusb". The module boundaries above (RNDIS/ethernet/utun/netconfig
> as separate, independently testable pieces) are still how the code is
> organised.

## Milestones

1. Done: `probe`, RNDIS init + OID queries (done manually; now in the package).
2. `status`/`connect`: AT data session, print IP and DNS. Done; verified live with a Telekom DE SIM (2026-10-05).
3. `up` with `--no-default-route`: utun + pump, then `ping -b utunN 1.1.1.1`.
4. `--default-route` + DNS + clean teardown.
5. Measure with iperf3 and decide whether the hot path needs porting.
6. IPv6 (`IPV4V6` PDP, RA via the modem): later.

## Async USB I/O: our own ctypes binding to libusb (decided 2026-09-25)

This is the current, canonical design for the USB layer — it replaced the
pyusb-based scaffold shown above.

**Why:** synchronous pyusb transfers cost ~106 µs each (measured), which caps
a single-transfer-per-packet path at ~9 k pkt/s (≈100 Mbps at 1400 B). Linux
`usbnet` keeps many URBs (USB Request Blocks — the unit of one in-flight USB
transfer) in flight per direction. Libusb's async API does the same.

**Why our own binding:** we chose it over the alternatives because it needs
no new dependency and gives us full control. Rejected alternatives:
`python-libusb1` (mature, but an extra dependency) and several threads
running sync pyusb (packets can be reordered on RX).

### Module `usb_async.py` (replaces pyusb everywhere)

- `Libusb`: loads `libusb-1.0.dylib` via ctypes (same search order as today). Declares `argtypes`/`restype` for every function used: `libusb_init_context` (fallback `libusb_init`), `libusb_exit`, `libusb_get_device_list`/`free_device_list`, `libusb_get_device_descriptor`, `libusb_open`/`close`, `libusb_get_active_config_descriptor`/`free_config_descriptor` (endpoint discovery), `libusb_claim_interface`/`release_interface`, `libusb_clear_halt`, `libusb_reset_device`, `libusb_control_transfer`, `libusb_bulk_transfer`, `libusb_interrupt_transfer`, `libusb_alloc_transfer`/`free_transfer`/`submit_transfer`/`cancel_transfer`, `libusb_handle_events_timeout_completed`, `libusb_error_name`, `libusb_get_version`.
- `LibusbTransfer(ctypes.Structure)` mirrors `struct libusb_transfer` field by field (`dev_handle`, `flags` u8, `endpoint` u8, `type` u8, `timeout` c_uint, `status` c_int, `length` c_int, `actual_length` c_int, `callback`, `user_data`, `buffer`, `num_iso_packets` c_int). ctypes natural alignment gives 64 bytes on LP64. `libusb_fill_bulk_transfer` is `static inline` in the header, so we fill the fields ourselves.
- **Layout test:** `tests/test_usb_async_layout.py` compiles a small C program with `cc` against `/opt/homebrew/include/libusb-1.0/libusb.h` that prints `sizeof`/`offsetof` for every field, and compares them with `ctypes.sizeof`/`Field.offset`. It is skipped (with a reason) only if no compiler or header is present.
- `UsbDevice`: one open handle per process, shared by the RNDIS ifaces (0/1) and the AT iface (6). Ctx-managed, releases claimed ifaces on close. It has sync helpers (`control_in/out`, `bulk_in/out`, `interrupt_in`) that map libusb errors to typed exceptions: `UsbTimeout`, `UsbNoDevice`, `UsbPipeError`, `UsbError(code, name)`.
- `AsyncEndpoint`: a pool of N pre-allocated transfers plus buffers for one endpoint.
  - **Lifetime rules (a mistake here crashes a root process):** transfers, buffers and the single `CFUNCTYPE` callback object are allocated once and kept referenced in the pool until *every* transfer has reported a final status after cancel. Never free or resize a buffer while its transfer is in flight. `free_transfer` only after its callback ran with a non-resubmitted status. No `LIBUSB_TRANSFER_FREE_*` flags (Python owns the memory).
  - The callback must never raise into C: wrap its body in try/except, log, and mark the pool failed.
  - Status handling: `COMPLETED` → deliver (IN) / release slot (OUT). `TIMED_OUT` → IN: resubmit; OUT: count stall, release slot. `CANCELLED` → retire. `NO_DEVICE` → mark failed (device gone), retire. `STALL` → schedule `clear_halt` from a non-event thread, then resubmit. `ERROR`/`OVERFLOW` → count, resubmit with backoff; fatal after K consecutive.
- `EventLoop`: one thread runs `libusb_handle_events_timeout_completed(ctx, 100 ms)` until stopped. All callbacks run on it, so **RX order is preserved**. `stop()` = cancel all pools, then keep handling events until every pool reports all transfers retired (bounded wait, e.g. 2 s; if exceeded, log loudly, skip freeing and leak rather than free memory that's still in flight).

### New data path `AsyncBridge` (same public API as `Bridge`: start/stop/failed/stats)

- **RX:** `--rx-urbs` (default 8) bulk-IN transfers of 16 KiB on 0x81. On completion: `unpack_packets` → strip → utun write (non-blocking; count drops on EAGAIN) → ARP handling → resubmit immediately. The utun write happens on the event thread, which keeps the order.
- **TX:** the tx thread reads utun and wraps into PACKET_MSG (+1 pad byte when the length is a multiple of wMaxPacketSize, and drop if > max_transfer_size). It takes a free OUT transfer from a pool of `--tx-urbs` (default 8) and submits it. If none is free (all in flight / modem stalled), drop and count `tx_stalls` (rate-limited log). OUT timeout 500 ms.
- **Control:** a 1-deep async interrupt-IN transfer on 0x82 increments a counted condition, once per `RESPONSE_AVAILABLE` notification. The control thread then does exactly one `GET_ENCAPSULATED_RESPONSE` per counted notification (never poll without a notification: that crashed the modem firmware), handles KEEPALIVE/INDICATE_STATUS, and sends our 5 s keepalive. The FM350 sends the RNDIS-spec encoding of RESPONSE_AVAILABLE (`01 00 00 00 00 00 00 00`), not the CDC `A1 01 …` form; both are accepted. It also sends more notifications than it has responses, so some GETs return the spec's 1-byte `00` "nothing pending" reply, which is ignored (verified on hardware 2026-10-05).
- `cli up --io async|sync` (default async). `Bridge` (sync) stays as the fallback until async is proven with a SIM.

### Verification without a SIM

- Unit tests: layout test; pool state machine with a fake `Libusb` that records submits/cancels and lets the test fire callbacks with each status (COMPLETED, TIMED_OUT, CANCELLED, NO_DEVICE, STALL); lifetime test that the pool frees nothing before all transfers retire; AsyncBridge RX ordering (callbacks in order → utun writes in order) and TX pool exhaustion → drops/stalls.
- Live (read-only, no hammering): `fm350mac async-selftest`:
  1. open, claim 0/1, RNDIS init, 8 RX transfers pending for 3 s (expect 0 frames), cancel → all 8 retire as CANCELLED, no leaks/crash;
  2. submit 5 ARP frames on the async OUT pool, expect 3 COMPLETED and 2 TIMED_OUT (the known no-bearer queue of 3);
  3. halt, release.
  Then `probe`/`status` must still work (the modem is healthy).
- Throughput gain: measured only with a SIM (iperf3, sync vs async).

## Privilege separation (decided 2026-09-25)

**Problem:** `sudo fm350mac up` runs Homebrew Python, the project `.venv` and libusb as root. All of them are user-writable, so anything running under your account can plant code that then runs as root. Measured: USB access (libusb claim, RNDIS, AT) **doesn't need root**. Only utun creation and ifconfig/route/scutil do.

**Design:** split into an unprivileged main process and a tiny root helper.

| | Main process `fm350mac` | Helper `fm350mac-helper` |
|---|---|---|
| Runs as | you | root, via a LaunchDaemon |
| Interpreter | Homebrew/.venv Python (any) | **`/usr/bin/python3`** (Apple CLT 3.9.6, root-owned; no user-writable code on its path, `-I -S` isolated mode) |
| Code | everything: USB, RNDIS, AT, bridge, supervisor | one file, stdlib only, Python 3.9 compatible, installed root:wheel 0755 at `/usr/local/libexec/fm350mac-helper` |
| Does | data path | creates utun, passes its fd over the socket, sets address/routes/DNS, restores everything on disconnect |

- **Transport:** a Unix socket `/var/run/fm350mac-helper.sock` (created by launchd via the plist `Sockets` key, mode 0600, owner = the installing user; alternatively the helper creates it itself). The helper checks the peer uid with `LOCAL_PEERCRED` against `AllowedUID` fixed at install time, and rejects everyone else.
- **Protocol:** one JSON object per line, request→response, max 4 KiB per message. Unknown fields and ops are rejected. Ops:
  - `hello {version}` → `{version, pid}`
  - `open_utun {}` → the fd via `SCM_RIGHTS` + `{ifname}`. Max 1 per connection.
  - `set_address {ip}`: ip must pass the same checks as `at.valid_assigned_ipv4` (the helper has its own copy; no import from the package).
  - `reconfigure_address {old_ip, new_ip}`
  - `add_host_route {dest}`: dest must be a single unicast IPv4 host (not 0/8, 127/8, 169.254/16, multicast, 240/4 or broadcast). Used by `up --route-host` and loopback mode. Max 8 per connection, no duplicates; deleted newest first on teardown.
  - `clear_host_routes {}`: delete this connection's host routes now (used while the modem is re-enumerating).
  - `set_default_route {enable}`: capture/restore semantics exactly as in `NetConfig` (idempotent; never captures an interface it owns).
  - `set_dns {servers: [≤3 IPv4]}` / `clear_dns {}`
  - `teardown {}`
- **Automatic cleanup:** the helper tracks all changes per connection. When the connection closes for any reason (including SIGKILL or a crash of the main process), it undoes them in reverse order and closes the utun. That removes the "SIGKILL leaves routes/DNS behind" problem.
- Commands run with fixed argv lists via absolute paths (`/sbin/ifconfig`, `/sbin/route`, `/usr/sbin/scutil`), with a minimal environment and no shell.
- **Main process:** `HelperClient` implements the existing `Utun` + `NetConfig` interfaces over the socket, so cli/bridge/supervisor don't change. `up` no longer needs root when the helper is installed. `--no-helper` keeps the current sudo mode as a fallback.
- **Install/uninstall** (run these with sudo; they print every step first and have `--dry-run`): `fm350mac helper install` copies the helper file, writes `/Library/LaunchDaemons/de.fm350mac.helper.plist` (root:wheel 0644, `ProgramArguments = [/usr/bin/python3, -I, -S, /usr/local/libexec/fm350mac-helper, --allowed-uid, <uid>]`), then `/bin/launchctl bootout system/de.fm350mac.helper` (failure ignored) and `/bin/launchctl bootstrap system …`. The bootout matters: once launchd has started the helper (on the first connection) it stays resident, so a reinstall would otherwise keep running the old code. `helper uninstall` reverses it (and removes `/var/log/fm350mac-helper.log`). `helper status` shows whether it's loaded and reachable, and prints "installed helper is out of date — run `fm350mac helper install`" if the installed file differs from the packaged one. Connections that stay silent for 300 s before opening a utun are dropped (the timeout only applies until the utun is open; a live session may idle for hours); SIGTERM unwinds through the connection's cleanup (one teardown).
- **Tests:** the helper's request validation and state machine are unit-tested with a fake command runner, **also executed under `/usr/bin/python3` (3.9)**. The fd passing is tested with a socketpair and a pipe fd. Client/helper end to end runs in-process with the fake runner. The only live root step is running `helper install` and then `up --loopback` without sudo.

This design has since run live end to end — see the [bench log's privilege
separation entry](bench-log.md), which found no gaps beyond what's listed
under "What's not proven yet". For everyday setup commands (`helper
install`, `helper status`, cleaning up after `kill -9`), see the
[fm350mac README](../fm350mac/README.md), which documents the same design
from a user's point of view.

## What's not proven yet

- The real data path (`up` against the actual FM350-GL) has run live only
  briefly (2026-10-05, Telekom DE SIM, `up --route-host`: ping, HTTPS, a 1 MB
  download). Long sessions, re-enumeration recovery and `--default-route` /
  `--dns` on hardware are still verified only by unit tests and
  `up --loopback` [fm350mac README, Limitations].
- The async I/O rewrite's whole point — higher throughput than the ~9 k
  pkt/s / ~100 Mbps synchronous ceiling — has not been measured; only the
  synchronous figure above comes from a real measurement. The "~150 Mbps"
  threshold used above to decide whether to port the hot path is a design
  estimate, not a result [fm350mac README, Limitations].
- IPv6 (`IPV4V6` PDP context, router advertisements from the modem) is
  still just a milestone on the list above, not implemented.
- The design is macOS/Apple Silicon only, by choice: none of the rejected
  options (kernel extension, DriverKit, Network Extension, Linux/Windows
  parity) are being pursued.

## Glossary

Terms used on this page, defined in the [shared glossary](glossary.md): [AT command](glossary.md#at-command), [AT port](glossary.md#at-port), [APN](glossary.md#apn), [PDP context / data session](glossary.md#pdp-context--data-session), [RNDIS](glossary.md#rndis), [libusb](glossary.md#libusb), [utun](glossary.md#utun), [LaunchDaemon](glossary.md#launchdaemon), [Failover / failback](glossary.md#failover--failback), [ARP](glossary.md#arp), [GIL](glossary.md#gil), [URB](glossary.md#urb), [kext / kernel extension](glossary.md#kext--kernel-extension), [SIP](glossary.md#sip).
