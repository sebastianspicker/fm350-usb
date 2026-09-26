# Security policy

## Reporting a vulnerability

Please report security problems privately, not in a public issue. Use the **Report a vulnerability** button on this repository's **Security** tab (GitHub private vulnerability reporting). Include what you found, how to reproduce it, and which component and version (commit) it affects.

This is a hobby project maintained in spare time, so there's no guaranteed response time. We'll acknowledge valid reports and credit you in the fix unless you'd rather stay anonymous.

## What's in scope

The parts of this repo that run with elevated privileges or change system configuration:

- **`fm350mac` root helper** (`fm350mac/src/fm350mac/helper/`): runs as root through a LaunchDaemon and accepts requests over a Unix socket from the unprivileged `fm350mac` process. Anything that lets another local user, or a malicious modem, run code as root or change routes or DNS beyond what the helper is meant to do is in scope.
- **`fm350mac helper install|uninstall`** and **`fm350mac up --no-helper`** (run with `sudo`).
- **Router scripts** in `openwrt/` (`install.sh`, `uninstall.sh`, `fm350-watchdog`, `fm350-status.sh`), which run as root on the router.
- **`tools/fm350_diag.py`**, which sends commands to the modem, including shell commands over ADB.

## Known and by design (not vulnerabilities in this project)

- **The FM350-GL exposes a root ADB shell over USB with no authentication.** That's the module's firmware, not something this project adds. Treat anything with USB access to the module as root on the modem. See [docs/dell-dw5931e-usb.md](docs/dell-dw5931e-usb.md).
- **Modem backups contain the IMEI and the RF calibration** of one physical unit. The tools write them only to your computer (`backups/`, ignored by git). Never publish them.
- **Status output and diagnostic reports can reveal your location** through the serving cell's ID and TAC. The tools redact these by default or with `--redact` / `-x`. Check the output before you post it anywhere.
