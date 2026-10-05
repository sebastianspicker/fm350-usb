# Changelog

All notable changes to `fm350mac`, the macOS driver in this repository, are
documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the version follows [PEP 440](https://peps.python.org/pep-0440/) (the
first release is an alpha). The OpenWrt scripts and the documentation are not
versioned separately.

## [0.1.0a1] - unreleased

First alpha release of `fm350mac`. See the
[README](https://github.com/sebastianspicker/fm350-usb/blob/v0.1.0a1/fm350mac/README.md)
for installation, limitations and the trust model. The `v0.1.0a1` tag is
created at release; until then install from the branch with `@alpha-0.1`.

Helper sha256: <filled in at release>

Current value of `fm350mac/src/fm350mac/helper/fm350mac_helper.py` while
unreleased (recompute with `shasum -a 256` on the file before tagging):
`4973bc87fc9cbb5df508321e538884984ad38c6ee88e8ceaf8206eafde3bb5be`.
Compare it with the `helper sha256:` printed by `fm350mac helper install
--dry-run` and install with `--expect-sha256`. `helper status` also shows the
installed file's sha256, but that is a consistency check, not an integrity
proof.

### Added

- **Data path.** `fm350mac up` bridges the FM350-GL's USB RNDIS interface to a macOS `utun` interface in user space, using our own ctypes binding to libusb (no pyusb, no kernel extension, no DriverKit). The default `--io async` keeps several bulk transfers in flight per direction (`--rx-urbs`, `--tx-urbs`), preserves RX order, applies backpressure into the utun when all OUT transfers are busy, and queues ARP replies for the tx thread.
- **`--route-host IP`** (repeatable, max 8): route single IPv4 hosts through the tunnel, the safe way to test on a metered SIM.
- **Root helper.** `fm350mac helper install|uninstall|status` manages a LaunchDaemon (`de.fm350mac.helper`) that runs one stdlib-only file under `/usr/bin/python3 -I -S`. It creates the utun, sets address, routes and DNS, and undoes them when the connection closes, so `up` runs without root. The socket is mode 0600, owned by the installing uid, with a peer-credential check. The helper reports its version and `up` warns on a mismatch. `helper install --dry-run` prints the plan without changes.
- **Resilience (async bridge and supervisor).** A keepalive watchdog fails the bridge when the modem stops answering keepalives; a notification-driven control channel (one `GET_ENCAPSULATED_RESPONSE` per notification); supervisor stall detection (tx growing while rx is flat for 60 s cycles the PDP context once, then fails the bridge); an RNDIS-level rebuild after a bridge failure with the modem still on the bus; rebuild retry with backoff for up to 10 minutes; device-loss recovery after a USB re-enumeration with IMEI pinning.
- **Instrumentation.** A `perf:` line at shutdown (OUT latency histogram, in-flight maximum, slot waits, RX frames per URB), plus a `stats-detail:` debug line with `--verbose`. The `stats:` line, IP bytes only, is the figure to use for real SIM usage.
- **Error UX.** Expected failures print `fm350mac: <cause>. <next step>` instead of a traceback (libusb missing, modem not found or busy, helper missing or unreachable, SIM PIN/PUK/not inserted, AT or RNDIS timeouts). `--verbose` keeps tracebacks. `up` exit codes: 0 ok, 1 error, 2 bridge failure or usage, 3 modem did not come back or rebuild budget exhausted, 4 modem identity changed, 130 interrupted.
- `status`, `doctor`, `probe`, `at`, `connect`, `disconnect`, `up --loopback` (in-process fake modem) and `async-selftest` (needs `--yes`).
- Packaging: dynamic version, PEP 639 license metadata, classifiers, project URLs; CI with a Python 3.11-3.13 matrix, a wheel build check and a tag-triggered pre-release workflow.

### Changed (review fixes)

- **Trust model.** The docs no longer claim that inspecting the helper file or `--dry-run` limits what runs as root: `sudo fm350mac helper install` runs the user-owned interpreter and packages as root. New: `helper install` prints `helper sha256: ...` and accepts `--expect-sha256 HEX` (refuses on a mismatch); `helper status` prints the installed file's sha256 (a consistency check, not an integrity proof); a root install from a user-owned Python prints a `WARNING: running user-owned code as root` message. Root-owned `__pycache__` files may be written into the user's venv.
- **Install ref.** The install commands and project URLs point at the `v0.1.0a1` tag instead of `main` (still 0.1.0).
- **Helper errors.** A permission-denied connect names the socket owner uid and your uid. The "reinstall the helper" hint is shown only for a protocol or version mismatch, not for operational refusals. The probe distinguishes `connect timed out`, `connection refused` and `hello timed out`.
- **`--no-helper` without root** prints `... --no-helper needs root: re-run it with sudo.`
- **`helper uninstall`** exits 1 when `launchctl bootout` really fails (not when the job was simply not loaded) or the job is still loaded afterwards, and warns when the socket file is left behind.
- **SIM PIN.** The PIN-locked message recommends unlocking in a phone first and warns that a PIN typed into `fm350mac at` ends up in the shell history. A SIM PIN is always masked in output.
- **AT timeouts** are reported per command (`at`: `<cmd>: timed out waiting for a final result code`; `status`, `doctor`, `connect`, `disconnect`: `<command>: no final result code for 'AT...' within Ns`; `up`: `modem did not answer: ...`).
- **Stall rule.** A stall needs at least 20 new tx timeouts with rx flat for 60 s. Traffic the modem accepted but nobody answered is not a stall. Stall-triggered rebuilds are not capped (a backup link keeps being retried); other bridge failures keep the cap of 3 in a row.
- **Keepalive watchdog** counts only successful acknowledgements; a `KEEPALIVE_CMPLT` with an error status does not reset it.
- **Rebuild.** An RNDIS-level rebuild pauses 3 s between halting and re-initialising. The 600 s rebuild budget is measured from the loss of the session.
- **utun buffers.** `SO_SNDBUF` is raised to 1 MiB; `SO_RCVBUF` stays at the system default.
- **Async tx lost wakeup** (a slot that retired just before the wait left the tx thread sleeping 50 ms) is fixed, and the benchmark script no longer ends `iperf3 -n` runs on whole-second boundaries.
- **Release workflow.** Build and publish are separate jobs (the publish job only downloads the artifact and creates the pre-release); the tagged commit must be on `main` or an `alpha-*` branch; the build backend (`hatchling==1.32.4`) and uv are pinned.
- **Docs.** `fm350mac: command not found` after `uv tool install` is covered (`uv tool update-shell`); troubleshooting texts were checked against the code.

### Verified on hardware (2026-10-05)

FM350-GL / Dell DW5931e, Telekom DE SIM, LTE B3/B7, macOS 27, Apple Silicon: USB enumeration, AT, `status`/`doctor`/`probe`, `up --route-host` with ping, HTTPS and capped `iperf3` (async roughly 10-20 Mbit/s down and 7-15 Mbit/s up on a weak cell, driver CPU 3-8%), `helper install` and `helper status`, and clean teardown (routes removed, PDP context deactivated).

### Known limitations

- Verified on hardware (2026-10-05, FM350-GL / DW5931e, Telekom DE): `up --route-host`, a 10-minute idle soak (keepalives acknowledged every 5 s, no watchdog false alarm), automatic recovery after unplugging and replugging the modem (57 s), 50 MB transfers at 42.1 Mbit/s down and 34.7 Mbit/s up, the non-blocking utun read path.
- Not verified on hardware: `--default-route`, `--dns`, sessions longer than 10 minutes, the keepalive watchdog and stall detection actually firing, rebuild retry after a failed bring-up, the RNDIS-level rebuild, and Intel Macs.
- Unplugging the modem logs a few `ERROR` lines from the USB layer (`LIBUSB_ERROR_NOT_FOUND`/`NO_DEVICE`) before the "modem disconnected" warning; they are expected and harmless.
- IPv4 only; no IPv6 data path.
- One modem only.
- `--io sync` is a frozen fallback and unsupported. It had one unexplained download stall in 6 runs.
- macOS counts received bytes twice in utun interface counters; the `stats:` line is the real usage.
- The helper and the package are not code-signed or notarized. Installing the helper runs the package as root once; see the trust model in the README.
- Throughput was only measured in short, capped runs on a weak cell.
- `--expect-sha256` pins only the helper file that stays installed as root. The install step itself still runs the user-owned Python environment as root.
- Stall-triggered rebuilds are uncapped by design: with a dead link `up` keeps retrying until you stop it. The 600 s budget limits failed bring-up attempts, not stalls of an established session.
- `status` output reveals the serving cell (cell ID, TAC) unless you pass `--redact`.
- Not published to PyPI: install from the `v0.1.0a1` tag (or `@alpha-0.1` before the tag exists).

[0.1.0a1]: https://github.com/sebastianspicker/fm350-usb/tree/v0.1.0a1
