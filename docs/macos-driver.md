# fm350mac: our own macOS data path for the FM350-GL

Design notes for `fm350mac`, a user-space macOS data path for the Fibocom
FM350-GL 5G modem: the architecture, the alternatives we rejected, and why.
For the package itself (install, commands), see
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
- Goal: IP connectivity from the FM350-GL on macOS (developed on Apple Silicon, macOS 27)
  without third-party drivers, kernel extensions or SIP changes, using only
  code in this repo. Main use: bench-testing SIM, registration and
  throughput before the dongle goes on the Flint 2, and as an ad-hoc Mac
  uplink. The production failover path is still the router (see
  [setup-guide.md](setup-guide.md)).
- Status (release 0.1.0a1, alpha): verified on hardware on 2026-10-05 (FM350-GL / Dell DW5931e, Telekom DE SIM, LTE B3/B7, macOS 27, Apple Silicon): USB enumeration, AT, `status`/`doctor`/`probe`, `up --route-host` (ping, HTTPS, capped `iperf3`), `helper install`/`helper status`, clean teardown, a 10-minute idle soak, automatic recovery after unplug/replug (57 s) and 50 MB transfers (42.1 Mbit/s down, 34.7 Mbit/s up). Not verified on hardware: `--default-route`, `--dns`, sessions longer than 10 minutes, the keepalive watchdog and stall detection actually firing, rebuild retry after a failed bring-up, the RNDIS-level rebuild, and Intel Macs. Not supported: IPv6 (IPv4 only) and more than one modem. See "What's not proven yet".
- Biggest caveat: live throughput has only been measured in short, capped runs on a weak cell (roughly 10-20 Mbit/s down, 7-15 Mbit/s up, driver CPU 3-8%). That is a radio-limited figure, not a capacity figure.

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
           │  utunN     │  utun.py  – PF_SYSTEM socket, point-to-point, read non-blocking
           └─────┬──────┘                (created by the root helper, fd passed to us)
   tx thread ▲   │ ▼  EventLoop thread          async_bridge.py (--io async)
           ┌─────┴──────┐
           │ L2 shim    │  ethernet.py – add/strip Ethernet, learn peer MAC,
           │            │               answer ARP for our IP, drop non-IP
           └─────┬──────┘
           ┌─────┴──────┐
           │ RNDIS      │  rndis.py (pure codec) + rndis_device.py (init/query/set/halt)
           │            │               PACKET_MSG framing, keepalive
           └─────┬──────┘
           ┌─────┴──────┐
           │ libusb     │  usb_async.py – our own ctypes binding: transfer pools,
           │ (ctypes)   │               event loop, claim ifaces 0/1/6, control + bulk + interrupt
           └─────┬──────┘
                 │                    control plane: at.py on iface 6
               FM350  ◄────────────── CGDCONT / CGACT / CGPADDR / GTDNS

   Root helper (separate process, LaunchDaemon):  creates the utun, sets address/routes/DNS
   on request over a Unix socket, undoes everything when the connection closes.
   Supervisor (supervisor.py): polls registration/PDP state, detects stalls, reconnects.
```

The pieces, in the order a packet or a failure meets them:

- **Root helper.** The only part that runs as root. It creates the `utun`, hands its file descriptor to the unprivileged process and applies address, routes and DNS on request (see "Privilege separation"). Everything else, including USB, runs as the user.
- **Async bridge** (`async_bridge.py`, the default). Keeps several bulk transfers in flight per direction. RX completions arrive on one libusb event thread and are written to the utun in order; a tx thread reads the utun and submits OUT transfers; a control thread handles the RNDIS control channel (below). Details in "Async USB I/O".
- **Notification-driven control channel.** The control thread never polls: it does exactly one `GET_ENCAPSULATED_RESPONSE` per `RESPONSE_AVAILABLE` interrupt notification (polling crashed the modem firmware).
- **Keepalive watchdog.** We send an RNDIS keepalive every 5 s and the modem answers with `KEEPALIVE_CMPLT` through that same notification path. If no *successful* acknowledgement arrives for 3 intervals (15 s), the control side is wedged: the bridge fails and the rebuild path (below) takes over. A `KEEPALIVE_CMPLT` with an error status is logged but not counted as an ack. It only looks at timestamps and never issues an extra GET.
- **Backpressure.** When all OUT transfers are busy, the tx thread waits up to 1 s for a free slot instead of dropping, so the utun's kernel queue fills and TCP sees the real link rate. Only a modem that stops draining (no slot for 1 s) causes drops, counted as `tx_stalls`.
- **ARP queue.** ARP replies are built on the event thread, which must never wait for an OUT slot. If none is free, the reply goes into a small bounded queue (4) and the tx thread sends it before its next utun packet.
- **Supervisor stall detection.** Besides polling registration and the PDP context every 10 s, the supervisor watches the packet counters: if the modem refuses OUT transfers (at least 20 new tx timeouts) while rx stays flat for 60 s, it cycles the PDP context once; if the stall persists through another window it fails the bridge. Traffic the modem accepted but nobody answered (a host that drops ICMP, SYN retries, one-way UDP) is not a stall, and neither is an idle link. A growing OUT-timeout count also triggers an early poll, at least 5 s after the previous one.
- **Rebuild retry.** A failed bridge ends in one of two rebuilds, see "Failure handling and rebuilds" below.

### Session flow (`fm350mac up --apn <apn>`)

1. **Helper:** unless `--no-helper` or `--dry-run`, connect to the helper (`hello`; a version mismatch only warns). `up` needs no `sudo` with the helper installed.
2. **AT (iface 6):** read the IMEI (identity pinning), `AT+CPIN?` = READY → `AT+CGDCONT=1,"IP","<apn>"` → `AT+CGACT=1,1` → `AT+CGPADDR=1` → our IPv4 → `AT+GTDNS=1` → DNS servers.
3. **RNDIS (ifaces 0/1):** INITIALIZE (MaxTransferSize 0x4000, clamped to the device's reported size) → query MAC/MTU → SET `OID_GEN_CURRENT_PACKET_FILTER` = directed | multicast | broadcast (0x0B).
4. **utun:** open (via the helper) → `ifconfig utunN inet <ip> <ip> mtu 1500 up` (point-to-point; the gateway is implicit).
5. **Routes:** with `--route-host IP` (repeatable, max 8; unicast IPv4 only), add a host route `IP -> utunN` for each (`route add -host IP -interface utunN`); this is the way to test on a metered SIM, since only those hosts use the tunnel. With `--default-route`, save the current default → `route add default -interface utunN` (unverified on hardware). With neither, nothing uses the tunnel and `up` warns. Everything is removed on exit, routes and DNS first, and also while waiting for the modem to re-enumerate (so the Mac is never left routing into a dead utun); they are re-added after the rebuild.
6. **DNS:** the modem's DNS servers are always queried and logged (`DNS=[...]` or `DNS=<none returned>`). Only with `--dns` is a `State:/Network/Service/fm350mac/DNS` key published via `scutil` (removed on exit). `--dns` requires `--default-route`: without it the carrier resolver would be queried over the normal uplink, so `up` refuses. If `--dns` is given but the modem returned no servers, a warning is logged and DNS is left unchanged. Unverified on hardware.
7. **Pump:** RX = bulk IN → RNDIS PACKET_MSG decode → Ethernet strip / ARP reply → utun write. TX = utun read → Ethernet wrap (dst = learned peer MAC, fallback broadcast; src = device MAC) → PACKET_MSG → bulk OUT.
8. **Keepalive and supervision:** answer device `KEEPALIVE_MSG` with `KEEPALIVE_CMPLT`, send our own every 5 s; the supervisor polls and watches for stalls.
9. **Teardown (SIGINT/SIGTERM/SIGHUP):** remove DNS key and restore routes, stop the bridge, log `stats:` and `perf:`, `AT+CGACT=0,1`, RNDIS HALT, release interfaces, then close the utun (with the helper, closing the connection also undoes anything left).

`up --dry-run` has no side effects: it sends only read-only AT queries (`CPIN?`, `CGSN`, `CGACT?`, `CGPADDR`, `GTDNS`; no `CGDCONT`/`CGACT` writes), does no RNDIS init/halt and runs no system command. It prints the AT commands and system commands it would run (using a placeholder address if the PDP context isn't already active). It needs neither root nor the helper.

Shutdown order (SIGINT/SIGTERM/SIGHUP handlers are installed before the first network change): routes/DNS, then the bridge, then AT deactivate, then RNDIS halt.

### Failure handling and rebuilds

All of this is exercised with `up --loopback`; none of it has run on hardware yet.

- **Device loss** (the FM350's firmware is known to crash and re-enumerate under real network conditions). The utun stays, routes and DNS are removed, `up` waits up to `--reenum-timeout` (default 180 s) for the modem, waits 15 s for its firmware to settle, then rebuilds AT, RNDIS and the bridge. The modem's IMEI is pinned at first bring-up: if a different IMEI comes back, `up` refuses to rebuild (exit 4). A different USB port only logs a warning. If the modem does not return in time: exit 3.
- **Bridge failure with the modem still on the bus** (watchdog, stall detection, an endpoint error). A bounded RNDIS-level rebuild without waiting for re-enumeration: at most 3 in a row (the counter resets after a session was stable for 10 minutes), then exit 2. Rebuilds forced by the supervisor's stall detection are not counted toward that cap, by design: a backup link that is down keeps being retried. The rebuild pauses 3 s between the RNDIS HALT and the re-INIT so the modem settles.
- **Rebuild retry.** While rebuilding, a failed bring-up step (SIM, AT, RNDIS, USB) is retried with backoff (5 s doubling to 60 s) until 600 s have passed since the session was lost (the budget is measured from the loss, re-enumeration wait included) instead of ending the session; after that, exit 3. The first bring-up still fails fast. A successful rebuild starts a fresh budget.
- **Exit codes:** 0 ok (including a clean Ctrl-C of a running session), 1 error, 2 bridge failure or usage error, 3 modem did not come back or rebuild budget exhausted, 4 modem identity changed, 130 interrupted during bring-up or the re-enumeration wait.

### Throughput expectation

The device takes 1 packet per transfer, so throughput is bound by packets per second, and in practice by the radio link. On 2026-10-05, on a weak LTE B3/B7 cell with a Telekom DE SIM, capped `iperf3` runs gave roughly 10-20 Mbit/s down and 7-15 Mbit/s up at 3-8% driver CPU, so the driver was not the limit. Those are short runs on one cell, not a capacity figure; nothing faster has been measured, and no speed target is claimed. If a faster link ever shows the Python hot path as the ceiling, only `async_bridge.py` and the USB hot path would be ported (Swift or Rust) behind the same interfaces; the control plane stays in Python. See "Performance and tuning" below for how to tell the pool, the modem and the CPU apart.

## Package layout

```text
fm350mac/                 Python >=3.11 project, uv-managed (pyproject.toml, hatchling)
  src/fm350mac/
    usb_async.py          our ctypes libusb binding: device, sync helpers, async transfer pools, EventLoop
    usb_transport.py      device discovery, interface claim, endpoints, RNDIS control transfers
    rndis.py              constants, message encode/decode, PACKET_MSG pack/unpack (pure)
    rndis_device.py       RNDIS control state machine (init/query/set/halt)
    ethernet.py           Ethernet header add/strip, ARP parse/reply (pure)
    async_bridge.py       the default data path: RX/TX pools, control thread, keepalive watchdog
    bridge.py             the frozen sync data path (--io sync, unsupported)
    at.py                 AT command channel and parsers
    cellinfo.py           status/doctor decoders (3GPP TS 27.007)
    utun.py               utun open/read/write (AF header handling, non-blocking read)
    netconfig.py          ifconfig/route/scutil wrappers (direct/root path), dry-run capable
    helper_client.py      the unprivileged side of the helper protocol (HelperClient, HelperNetConfig)
    helper_admin.py       `helper install|uninstall|status`
    helper/fm350mac_helper.py   the root helper: one stdlib-only Python 3.9 file, never imported
    supervisor.py         reconnect supervisor, stall detection
    loopback.py           in-process fake modem for `up --loopback`
    redact.py             masking for logs and output
    cli.py                `fm350mac probe|at|status|doctor|connect|disconnect|up|async-selftest|helper`
```

`probe`, `at`, `status`, `doctor` and `up` need no root when the helper is installed; `helper install|uninstall` need `sudo`.

## Milestones

1. Done: `probe`, RNDIS init + OID queries.
2. Done: `status`/`connect` (AT data session, IP and DNS), verified live with a Telekom DE SIM (2026-10-05).
3. Done: `up` with `--route-host`: utun + pump, verified live 2026-10-05 (ping, HTTPS, capped iperf3, clean teardown).
4. Done: the root helper, installed and checked on hardware (`helper install`, `helper status`).
5. Open: `--default-route` + `--dns` on hardware.
6. Done 2026-10-05: 10-minute soak and unplug/replug recovery on hardware. Open: longer sessions; the keepalive watchdog, stall detection and rebuild retry actually firing on hardware (so far only the healthy path was observed there).
7. Open: Intel Macs.
8. Later, not planned for 0.1.0a1: IPv6 (`IPV4V6` PDP, RA via the modem), more than one modem.

## Async USB I/O: our own ctypes binding to libusb (decided 2026-09-25)

This is the current, canonical design for the USB layer. It replaced the
original pyusb-based scaffold; pyusb is no longer used anywhere in the package.

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
- **Struct layout:** the field offsets and sizes follow `struct libusb_transfer` in `/opt/homebrew/include/libusb-1.0/libusb.h` (64 bytes on LP64).
- `UsbDevice`: one open handle per process, shared by the RNDIS ifaces (0/1) and the AT iface (6). Ctx-managed, releases claimed ifaces on close. It has sync helpers (`control_in/out`, `bulk_in/out`, `interrupt_in`) that map libusb errors to typed exceptions: `UsbTimeout`, `UsbNoDevice`, `UsbPipeError`, `UsbError(code, name)`.
- `AsyncEndpoint`: a pool of N pre-allocated transfers plus buffers for one endpoint.
  - **Lifetime rules (a mistake here crashes a root process):** transfers, buffers and the single `CFUNCTYPE` callback object are allocated once and kept referenced in the pool until *every* transfer has reported a final status after cancel. Never free or resize a buffer while its transfer is in flight. `free_transfer` only after its callback ran with a non-resubmitted status. No `LIBUSB_TRANSFER_FREE_*` flags (Python owns the memory).
  - The callback must never raise into C: wrap its body in try/except, log, and mark the pool failed.
  - Status handling: `COMPLETED` → deliver (IN) / release slot (OUT). `TIMED_OUT` → IN: resubmit; OUT: count stall, release slot. `CANCELLED` → retire. `NO_DEVICE` → mark failed (device gone), retire. `STALL` → schedule `clear_halt` from a non-event thread, then resubmit. `ERROR`/`OVERFLOW` → count, resubmit with backoff; fatal after K consecutive.
- `EventLoop`: one thread runs `libusb_handle_events_timeout_completed(ctx, 100 ms)` until stopped. All callbacks run on it, so **RX order is preserved**. `stop()` = cancel all pools, then keep handling events until every pool reports all transfers retired (bounded wait, e.g. 2 s; if exceeded, log loudly, skip freeing and leak rather than free memory that's still in flight).

### New data path `AsyncBridge` (same public API as `Bridge`: start/stop/failed/stats)

- **RX:** `--rx-urbs` (default 8) bulk-IN transfers of 16 KiB on 0x81. On completion: `unpack_packets` → strip → utun write (non-blocking; count drops on EAGAIN) → ARP handling → resubmit immediately. The utun write happens on the event thread, which keeps the order.
- **TX:** the tx thread reads utun (non-blocking `recv`, `poll` only when empty) and wraps into PACKET_MSG (+1 pad byte when the length is a multiple of wMaxPacketSize, and drop if > max_transfer_size). It takes a free OUT transfer from a pool of `--tx-urbs` (default 8) and submits it. If none is free it waits up to 1 s for one to retire (backpressure into the utun's kernel queue, so TCP sees the real link rate), using a generation counter under a condition so a slot freed between the failed submit and the wait is never missed (no lost wakeup). Only if none frees up in time (modem stalled) does it drop and count `tx_stalls` (rate-limited log). OUT timeout 500 ms. ARP replies are built on the event thread; if no OUT slot is free they are queued (small bounded queue) to the tx thread, which sends them before its next utun packet, since the event thread must never wait for a slot.
- **Control:** a 1-deep async interrupt-IN transfer on 0x82 increments a counted condition, once per `RESPONSE_AVAILABLE` notification. The control thread then does exactly one `GET_ENCAPSULATED_RESPONSE` per counted notification (never poll without a notification: that crashed the modem firmware), handles KEEPALIVE/INDICATE_STATUS, and sends our 5 s keepalive. The FM350 sends the RNDIS-spec encoding of RESPONSE_AVAILABLE (`01 00 00 00 00 00 00 00`), not the CDC `A1 01 …` form; both are accepted. It also sends more notifications than it has responses, so some GETs return the spec's 1-byte `00` "nothing pending" reply, which is ignored (verified on hardware 2026-10-05).
- `cli up --io async|sync` (default async). `Bridge` (sync, `bridge.py`) is a frozen fallback and unsupported in 0.1.0a1: it had one unexplained download stall in 6 runs (the missing data never reached the driver), and the keepalive watchdog, ARP queue and stall counters described here only exist in the async bridge.

### Performance and tuning

Measured in pure Python (no hardware): RX costs about 1.1 µs per packet, TX about 4 µs per packet including the utun `recv`. Filling a transfer struct on every submit cost about 0.74 µs (0.33 µs of it the `ctypes.cast`), so the static fields (`dev_handle`, `endpoint`, `type`, `timeout`, `callback`, `user_data`, buffer pointer) are now set once per slot at pool creation and a submit only sets `length` (about 0.08 µs). The utun fd is read non-blocking (`recv` first, `poll` for up to 0.5 s only on EAGAIN), because `recv` on a socket in timeout mode does a `poll()` first (0.84 µs against 0.43 µs). At start the bridge also logs the utun socket's `SO_RCVBUF`/`SO_SNDBUF` at DEBUG and tries to raise `SO_SNDBUF` (our writes into the utun) to 1 MiB so a burst from the modem does not hit `ENOBUFS`. `SO_RCVBUF` (the outbound queue the kernel holds for us) stays at the system default, because a deep queue there only adds upload latency; a refused `SO_SNDBUF` request is non-fatal.

The CPU cost is not the limit; the modem's OUT latency is. With `N` OUT transfers in flight, TX tops out at about `pps ≈ N_tx_urbs / OUT_latency`. For example 8 URBs at 1 ms per transfer allow about 8000 packets/s, roughly 96 Mbit/s at 1500 bytes. Once the pool is the limit, the tx thread waits for slots (`tx_slot_waits` rises).

At shutdown the driver logs one INFO line, and with `--verbose` an extra DEBUG `stats-detail:` line (the INFO `stats:` line is unchanged; tools parse it):

```
perf: out_latency[n=… mean=…us max=…us <100us:… <250us:… <500us:… <1000us:… <2000us:… <5000us:… >=5000us:…] tx_inflight_max=A/B tx_slot_waits=… tx_slot_wait=…ms rx_urbs=… rx_frames_per_urb mean=… max=…
```

- `out_latency`: submit-to-completion time of completed OUT transfers, in fixed buckets (failed or timed-out transfers are not included; they show up in `tx_timeouts`).
- `tx_inflight_max=A/B`: the most OUT transfers in flight at once, out of `--tx-urbs` (B). A below B means the pool never limited TX.
- `tx_slot_waits` / `tx_slot_wait`: how often the tx thread found every slot busy, and the total time it then waited.
- `rx_frames_per_urb`: Ethernet frames per completed RX URB (mean and max); a mean near 1 means `--rx-urbs` is not what limits RX.

To test the pool size on hardware, run the same iperf3 upload with `--tx-urbs 4`, `8`, `16` and compare `tx_inflight_max`, `tx_slot_waits` and the upper `out_latency` buckets: if `tx_inflight_max` hits the limit, `tx_slot_waits` is high and throughput grows with `--tx-urbs`, the pool was the ceiling; if latency buckets shift right as URBs are added, the modem is the ceiling and more URBs only add queueing. The defaults are unchanged until that has been measured.

### Verification without a SIM

- Live (read-only, no hammering): `fm350mac async-selftest --yes` (the flag, or an interactive "yes", confirms the USB reset at the end):
  1. open, claim 0/1, RNDIS init, 8 RX transfers pending for 3 s (expect 0 frames), cancel → all 8 retire as CANCELLED, no leaks/crash;
  2. submit 5 ARP frames on the async OUT pool, expect 3 COMPLETED and 2 TIMED_OUT (the known no-bearer queue of 3);
  3. halt, release.
  Then `probe`/`status` must still work (the modem is healthy).
- Throughput gain: measured only with a SIM (iperf3, sync vs async).

## Privilege separation (decided 2026-09-25)

**Problem:** running the whole data path under `sudo` (the original design) would run Homebrew Python, the project `.venv` and libusb as root. All of them are user-writable, so anything running under your account can plant code that then runs as root. Measured: USB access (libusb claim, RNDIS, AT) **doesn't need root**. Only utun creation and ifconfig/route/scutil do.

**Design:** split into an unprivileged main process and a tiny root helper.

| | Main process `fm350mac` | Helper `fm350mac-helper` |
|---|---|---|
| Runs as | you | root, via a LaunchDaemon |
| Interpreter | Homebrew/.venv Python (any) | **`/usr/bin/python3`** (Apple CLT 3.9.6, root-owned; no user-writable code on its path, `-I -S` isolated mode) |
| Code | everything: USB, RNDIS, AT, bridge, supervisor | one file, stdlib only, Python 3.9 compatible, installed root:wheel 0755 at `/usr/local/libexec/fm350mac-helper` |
| Does | data path | creates utun, passes its fd over the socket, sets address/routes/DNS, restores everything on disconnect |

- **Transport:** a Unix socket `/var/run/fm350mac-helper.sock` (created by launchd via the plist `Sockets` key, mode 0600, owner = the installing user; alternatively the helper creates it itself). The helper checks the peer uid with `LOCAL_PEERCRED` against `AllowedUID` fixed at install time, and rejects everyone else.
- **Protocol:** one JSON object per line, request→response, max 4 KiB per message. Unknown fields and ops are rejected. Ops:
  - `hello {version}` → `{version, helper_version, pid}`. `helper_version` is the release version of the helper file; `up` and `helper status` warn when it is missing (an older helper) or differs from the driver's.
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
- **Install/uninstall** (run these with sudo; they print every step first and have `--dry-run`): `fm350mac helper install` copies the helper file, writes `/Library/LaunchDaemons/de.fm350mac.helper.plist` (root:wheel 0644, `ProgramArguments = [/usr/bin/python3, -I, -S, /usr/local/libexec/fm350mac-helper, --allowed-uid, <uid>]`), then `/bin/launchctl bootout system/de.fm350mac.helper` (failure ignored) and `/bin/launchctl bootstrap system …`. The bootout matters: once launchd has started the helper (on the first connection) it stays resident, so a reinstall would otherwise keep running the old code. `helper install --expect-sha256 HEX` refuses to install unless the helper file's sha256 is `HEX`; the sha256 is printed as `helper sha256:` (also by `--dry-run`). `helper uninstall` reverses it (and removes `/var/log/fm350mac-helper.log`); it exits 1 if `launchctl bootout` really fails or the job is still loaded afterwards, and warns if the socket file is left behind. `helper status` shows whether it's loaded and reachable, prints the installed file's `helper sha256 :` (a consistency check, not an integrity proof), and prints "installed helper is out of date — run `fm350mac helper install`" if the installed file differs from the packaged one. Connections that stay silent for 300 s before opening a utun are dropped (the timeout only applies until the utun is open; a live session may idle for hours); SIGTERM unwinds through the connection's cleanup (one teardown).
- **Tests:** the helper's request validation and state machine are unit-tested with a fake command runner, **also executed under `/usr/bin/python3` (3.9)**. The fd passing is tested with a socketpair and a pipe fd. Client/helper end to end runs in-process with the fake runner. The only live root step is running `helper install` and then `up --loopback` without sudo.

**Trust model, stated plainly.**

- Installing runs the package as root once: `sudo "$(command -v fm350mac)" helper install` executes the installed `fm350mac` and its Python environment as root, to read one file, check that it compiles under `/usr/bin/python3 -I -S`, and write the helper and its plist. That interpreter and those packages are owned by the user, so anything that can write to them decides what runs as root for that step. Inspecting the helper file or running `helper install --dry-run --allowed-uid <uid>` does not limit this: both use the same user-owned code. The mitigation is to compare the printed `helper sha256:` with the value published in the release notes and the CHANGELOG and install with `--expect-sha256 <sha256>`, which pins the one file that stays on the system as root. `helper install` also warns (`WARNING: running user-owned code as root`) when run as root from a user-owned Python, and root-owned `__pycache__` files may be written into the user's venv. `sudo uv run` must not be used: it runs uv as root and leaves root-owned files in the project's `.venv`.
- After that, only the copied helper file runs as root, with nothing user-writable on its interpreter or import path. The install step writes it atomically and refuses symlinks and directories that are not root-owned or are group/other-writable.
- The helper is not harmless: any process running as the allowed uid can ask it, as root, to create a utun, set an address, add up to 8 host routes, replace the default route and set DNS. It runs fixed argv lists without a shell and validates every argument, so it cannot be made to run arbitrary commands, but a hostile process running as that user could redirect the Mac's traffic. The socket (mode 0600, owned by the installing uid, plus the `LOCAL_PEERCRED` check) keeps other users out.
- Nothing is code-signed or notarized.

This design has since run live end to end — see the [bench log's privilege
separation entry](bench-log.md), which found no gaps beyond what's listed
under "What's not proven yet". For everyday setup commands (`helper
install`, `helper status`, cleaning up after `kill -9`), see the
[fm350mac README](../fm350mac/README.md), which documents the same design
from a user's point of view.

## What's not proven yet

Everything verified so far was verified on one unit on 2026-10-05 (Telekom DE SIM, LTE B3/B7, macOS 27, Apple Silicon): USB enumeration, AT, `status`/`doctor`/`probe`, `up --route-host` with ping, HTTPS and capped `iperf3`, `helper install`/`status`, and clean teardown (routes removed, PDP deactivated). Not verified on hardware:

- `--default-route` and `--dns`.
- Sessions longer than 10 minutes (a 10-minute idle soak passed).
- Recovery when the modem re-enumerates on its own (firmware crash). Unplug/replug recovery is verified: session rebuilt automatically 57 s after the unplug.
- The resilience features actually firing: keepalive watchdog, supervisor stall detection, rebuild retry after a failed bring-up, RNDIS-level rebuild. On hardware only the healthy path (watchdog not firing) has been observed.
- Intel Macs. The libusb loader also looks in `/usr/local`, but that is untested.
- Throughput above about 40 Mbit/s. 50 MB transfers reached 42.1 Mbit/s down and 34.7 Mbit/s up on LTE B8, radio-limited; the measured USB OUT latency (mean 184 µs) puts the 8-URB TX ceiling near 43k packets/s, far above that.

Not supported:

- IPv6. The data path is IPv4 only (`IPV4V6` PDP contexts and router advertisements from the modem are not implemented).
- More than one modem.
- Anything other than macOS: the design is macOS-only by choice, and none of the rejected options (kernel extension, DriverKit, Network Extension, Linux/Windows parity) are being pursued.
- `--io sync`, which is a frozen fallback.

## Glossary

Terms used on this page, defined in the [shared glossary](glossary.md): [AT command](glossary.md#at-command), [AT port](glossary.md#at-port), [APN](glossary.md#apn), [PDP context / data session](glossary.md#pdp-context--data-session), [RNDIS](glossary.md#rndis), [libusb](glossary.md#libusb), [utun](glossary.md#utun), [LaunchDaemon](glossary.md#launchdaemon), [Failover / failback](glossary.md#failover--failback), [ARP](glossary.md#arp), [GIL](glossary.md#gil), [URB](glossary.md#urb), [kext / kernel extension](glossary.md#kext--kernel-extension), [SIP](glossary.md#sip).
