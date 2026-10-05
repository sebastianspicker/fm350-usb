# Security policy

## Reporting a vulnerability

Please report security problems privately, not in a public issue. Use the **Report a vulnerability** button on this repository's **Security** tab (GitHub private vulnerability reporting). Include what you found, how to reproduce it, and which component and version (commit) it affects.

This is a hobby project maintained in spare time, so there's no guaranteed response time. We'll acknowledge valid reports and credit you in the fix unless you'd rather stay anonymous.

## Supported versions

Only the latest alpha release of `fm350mac` (currently 0.1.0a1) receives security fixes. There are no backports to older alphas. The router scripts and tools are fixed on the default branch only.

## What's in scope

The parts of this repo that run with elevated privileges or change system configuration:

- **`fm350mac` root helper** (`fm350mac/src/fm350mac/helper/`): runs as root through a LaunchDaemon and accepts requests over a Unix socket from the unprivileged `fm350mac` process. Anything that lets another local user, or a malicious modem, run code as root or change routes or DNS beyond what the helper is meant to do is in scope.
- **`fm350mac helper install|uninstall`** and **`fm350mac up --no-helper`** (run with `sudo`).
- **Router scripts** in `openwrt/` (`install.sh`, `uninstall.sh`, `fm350-watchdog`, `fm350-status.sh`), which run as root on the router.
- **`tools/fm350_diag.py`**, which sends commands to the modem, including shell commands over ADB.

## Known and by design (not vulnerabilities in this project)

- **The FM350-GL exposes a root ADB shell over USB with no authentication.** That's the module's firmware, not something this project adds. Treat anything with USB access to the module as root on the modem. See [docs/dell-dw5931e-usb.md](docs/dell-dw5931e-usb.md).
- **Modem backups contain the IMEI and the RF calibration** of one physical unit. The tools write them only to your computer (`backups/`, ignored by git). Never publish them.
- **Status output and diagnostic reports can reveal your location** through the serving cell's ID and TAC. `tools/fm350_diag.py` redacts by default. `fm350mac status`, `at` and `up` redact only with `--redact` (a SIM PIN is always masked). The router's `fm350-status.sh` redacts only with `-x`. Check the output before you post it anywhere.

## Trust model of the root helper

Installing the helper runs the `fm350mac` package as root once; afterwards only the copied helper file runs as root, and it serves only the uid it was installed for. What the helper can and cannot do is written down in the [fm350mac README](fm350mac/README.md#privilege-separation-and-the-trust-model) and the [design notes](docs/macos-driver.md#privilege-separation-decided-2026-09-25). Nothing is code-signed or notarized.

Inspecting the helper file or running `helper install --dry-run` does **not** limit what runs as root at install time: `sudo fm350mac helper install` runs the user-owned venv interpreter and packages as root, and anything that can write there as your user controls that step. The mitigations are:

- Compare the `helper sha256:` that `helper install` prints (also with `--dry-run`) with the value published in the release notes and the CHANGELOG, and install with `--expect-sha256 <sha256>`. This pins the one file that stays on the system as root, not the code that runs during the install.
- `helper install` warns (`WARNING: running user-owned code as root`) when run as root from a user-owned Python.
- Python run as root may write root-owned `__pycache__` files into your venv.
- `helper status` prints the installed file's sha256, but that is a consistency check against the packaged file, not an integrity proof.

Install only from the release tag, into an environment only you can write to. Reports about this install step are in scope (see above).
