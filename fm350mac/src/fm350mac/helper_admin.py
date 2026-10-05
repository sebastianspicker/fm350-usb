"""``fm350mac helper install|uninstall|status``: manage the root LaunchDaemon
that runs ``helper/fm350mac_helper.py`` (see docs/macos-driver.md,
"Privilege separation").

``install``/``uninstall`` change root-owned files and must be run with sudo
(``--dry-run`` needs neither root nor sudo, and only prints the plan).
``status`` never needs root.

**Socket ownership:** launchd creates the ``Sockets`` entry's file as root
before the helper ever runs, so the unprivileged main process couldn't
otherwise connect to a root:wheel, mode-0600 socket. The plist's
``Sockets`` dictionary supports a ``SockPathOwner`` key (alongside
``SockPathName``/``SockPathMode``) that tells launchd to ``chown`` the
socket to that uid right after creating it -- so it's chosen here over the
alternative (giving the helper ``RunAtLoad``/``KeepAlive`` true and having
it bind+chown its own socket at startup), since that alternative would start
a root process at every boot rather than on demand. Note that with socket
activation the helper is only *started* by the first connection; once
activated it stays resident (idle, just listening) until ``bootout``/
``uninstall``/reboot. That is why ``install`` boots out any running helper
before bootstrapping: otherwise a reinstall would leave the old code running.

**Install safety (`install` runs as root):** every destination is written
atomically (temp file in the same directory, ``O_EXCL|O_NOFOLLOW``,
``fchown``/``fchmod`` before it has a name anyone else can open, then
``rename()`` into place -- which replaces a symlink rather than following
it), and every directory involved -- including *existing* ancestors of
``INSTALL_DIR``, which on an Intel Homebrew Mac can be user-owned -- is
checked to be a real, root-owned, non-group/other-writable directory before
anything is written under it. The helper source is read once (refusing a
symlink, capped in size) and those exact bytes are what's both compile
tested and installed, so nothing can swap the file in between.
"""

from __future__ import annotations

import argparse
import errno
import os
import pwd
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable, Optional

from . import helper_client

_HELPER_SOURCE = Path(__file__).parent / "helper" / "fm350mac_helper.py"
INSTALL_DIR = Path("/usr/local/libexec")
INSTALL_PATH = INSTALL_DIR / "fm350mac-helper"
PLIST_PATH = Path("/Library/LaunchDaemons/de.fm350mac.helper.plist")
PLIST_LABEL = "de.fm350mac.helper"
PYTHON3 = "/usr/bin/python3"
SOCKET_PATH = helper_client.HELPER_SOCKET_PATH  # /var/run/fm350mac-helper.sock
_LOG_PATH = "/var/log/fm350mac-helper.log"
LAUNCHCTL = "/bin/launchctl"

# The helper is one smallish, hand-written file; anything bigger than this
# is not it, and reading it into memory is meant to be cheap.
_MAX_HELPER_SOURCE_BYTES = 256 * 1024


class HelperInstallError(Exception):
    """A safety check failed; installation must be refused."""


# See cmd_helper_install: bootout is asynchronous.
_BOOTOUT_WAIT_POLLS = 20
_BOOTOUT_WAIT_INTERVAL_S = 0.5
_BOOTSTRAP_ATTEMPTS = 3
_BOOTSTRAP_RETRY_INTERVAL_S = 1.0

_UID_NO_CHANGE = 2**32 - 1  # (uid_t)-1


def _print_step(msg: str) -> None:
    print(f"-> {msg}")


def _resolve_allowed_uid(explicit: Optional[int]) -> int:
    """The uid allowed to use the helper: an explicit ``--allowed-uid``, or
    ``$SUDO_UID`` (the user who ran ``sudo``) if ``--allowed-uid`` wasn't
    given. Raises HelperInstallError if neither is usable, or if the result
    is uid 0 -- the allowed uid must be an unprivileged user, never root
    (an "allowed uid" of 0 would make the whole helper pointless).
    """
    if explicit is not None:
        uid, source = explicit, "--allowed-uid"
    else:
        sudo_uid = os.environ.get("SUDO_UID")
        if sudo_uid is None:
            raise HelperInstallError(
                "no $SUDO_UID (were you not invoked via plain 'sudo'?) and no --allowed-uid given; "
                "pass --allowed-uid <uid> explicitly"
            )
        try:
            uid = int(sudo_uid)
        except ValueError:
            raise HelperInstallError(f"$SUDO_UID is not a number: {sudo_uid!r}") from None
        source = "$SUDO_UID"
    if uid == 0:
        raise HelperInstallError(f"refusing uid 0 (root) as the helper's allowed uid (from {source})")
    # uid_t is unsigned 32-bit and (uid_t)-1 means "no owner change" to
    # chown(): such a uid could never own the socket or match a peer.
    if not 0 < uid < _UID_NO_CHANGE:
        raise HelperInstallError(f"uid {uid} (from {source}) is out of range (1..{_UID_NO_CHANGE - 1})")
    return uid


def _plist_xml(allowed_uid: int, install_path: Path) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
\t<key>Label</key>
\t<string>{PLIST_LABEL}</string>
\t<key>ProgramArguments</key>
\t<array>
\t\t<string>{PYTHON3}</string>
\t\t<string>-I</string>
\t\t<string>-S</string>
\t\t<string>{install_path}</string>
\t\t<string>--allowed-uid</string>
\t\t<string>{allowed_uid}</string>
\t</array>
\t<key>RunAtLoad</key>
\t<false/>
\t<key>KeepAlive</key>
\t<false/>
\t<key>StandardOutPath</key>
\t<string>{_LOG_PATH}</string>
\t<key>StandardErrorPath</key>
\t<string>{_LOG_PATH}</string>
\t<key>Sockets</key>
\t<dict>
\t\t<key>Listener</key>
\t\t<dict>
\t\t\t<key>SockPathName</key>
\t\t\t<string>{SOCKET_PATH}</string>
\t\t\t<key>SockPathMode</key>
\t\t\t<integer>384</integer>
\t\t\t<key>SockPathOwner</key>
\t\t\t<integer>{allowed_uid}</integer>
\t\t</dict>
\t</dict>
</dict>
</plist>
"""


# --- filesystem safety (symlinks, ownership, TOCTOU) ------------------------


def _check_dir_safe(path: Path, expected_uid: int = 0) -> Optional[str]:
    """Check ``path`` and every existing ancestor of it: each must be a
    real directory, not a symlink, owned by ``expected_uid`` (always 0/root
    in production; tests pass their own uid to exercise this without being
    root), and not writable by group or other. Components that don't exist
    yet are fine (they'll be created, ``expected_uid``:wheel 0755, by
    ``_ensure_dir_tree`` -- see that function's docstring for why only those
    are ever touched). Returns an error message, or None if everything
    existing is safe.
    """
    # The filesystem root itself is always uid-0-owned regardless of who
    # everything under it is meant to belong to, and nothing can ever change
    # that -- checking it against `expected_uid` would always fail for a
    # non-root `expected_uid` (only ever used in tests) for no safety
    # benefit, so it's excluded; every other ancestor is still checked.
    ancestors = [p for p in reversed(path.parents) if p != Path(p.anchor)] + [path]
    for ancestor in ancestors:
        try:
            st = os.lstat(ancestor)
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(st.st_mode):
            return f"{ancestor} is a symlink"
        if not stat.S_ISDIR(st.st_mode):
            return f"{ancestor} exists and isn't a directory"
        if st.st_uid != expected_uid:
            owner_desc = "root-owned" if expected_uid == 0 else f"owned by uid {expected_uid}"
            return f"{ancestor} isn't {owner_desc} (owner is uid {st.st_uid}); refusing to install under it"
        if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            return f"{ancestor} is group- or other-writable (mode {oct(stat.S_IMODE(st.st_mode))}); refusing to install under it"
    return None


def _check_leaf_safe(path: Path) -> Optional[str]:
    """``path`` is a destination file we're about to (over)write atomically
    via ``_write_root_file`` -- that itself never follows a symlink there,
    but refuse outright rather than silently replacing one.
    """
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(st.st_mode):
        return f"{path} already exists and is a symlink; refusing to replace it"
    return None


def _ensure_dir_tree(path: Path, uid: int = 0, gid: int = 0) -> None:
    """Create ``path`` and any missing parents, owned ``uid``:``gid`` (always
    root:wheel in production; tests inject their own uid/gid, since chowning
    to root needs real root). Only components that don't already exist are
    created or touched; an existing ancestor (e.g. a Homebrew-owned
    ``/usr/local`` on Intel Macs) is never chowned/chmoded here --
    ``_check_dir_safe(path)`` must have already verified every existing
    ancestor is safe to install under.
    """
    missing = []
    current = path
    while not os.path.exists(current):
        missing.append(current)
        current = current.parent
    for component in reversed(missing):
        os.mkdir(component, 0o755)
        os.chown(component, uid, gid)
        os.chmod(component, 0o755)


def _write_root_file(dest: Path, data: bytes, mode: int, uid: int = 0, gid: int = 0) -> None:
    """Atomically install ``data`` as ``dest``, owned ``uid``:``gid`` (always
    root:wheel in production; tests inject their own uid/gid) with ``mode``:
    written into a private temp file in the same directory first
    (``O_EXCL|O_NOFOLLOW``, created 0600 so nothing else can open it while
    it's being written), ``fsync``ed, given its final owner/mode while it
    still has no name anyone else could race to open, then ``rename()``d
    into place -- ``rename()`` replaces whatever was at ``dest`` outright,
    it never follows a symlink there.
    """
    tmp_path = dest.parent / f".{dest.name}.tmp{os.getpid()}"
    fd = os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        try:
            os.write(fd, data)
            os.fsync(fd)
            os.fchown(fd, uid, gid)
            os.fchmod(fd, mode)
        finally:
            os.close(fd)
        os.rename(str(tmp_path), str(dest))
    except Exception:
        try:
            os.unlink(str(tmp_path))
        except OSError:
            pass
        raise


def _read_helper_source(path: Path) -> bytes:
    """Read the helper source exactly once: refuses a symlink (``O_NOFOLLOW``),
    refuses anything that isn't a small regular file, and returns the bytes
    read. Both the compile check and the actual install use these same
    bytes (never re-reading ``path``), so nothing can swap the file in
    between (TOCTOU).
    """
    try:
        fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        raise HelperInstallError(f"helper source not found: {path}") from None
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise HelperInstallError(f"{path} is a symlink; refusing to install it") from None
        raise HelperInstallError(f"could not open {path}: {exc}") from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise HelperInstallError(f"{path} isn't a regular file (symlink?)")
        if st.st_size > _MAX_HELPER_SOURCE_BYTES:
            raise HelperInstallError(f"{path} is larger than {_MAX_HELPER_SOURCE_BYTES} bytes; refusing to install it")
        chunks = []
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


# --- install / uninstall / status -------------------------------------------


def cmd_helper_install(
    args: argparse.Namespace,
    *,
    geteuid=os.geteuid,
    launchctl_runner=subprocess.run,
    compile_runner=subprocess.run,
    helper_source: Path = _HELPER_SOURCE,
    install_dir: Optional[Path] = None,
    install_path: Optional[Path] = None,
    plist_path: Optional[Path] = None,
    after_source_read_hook: Optional[Callable[[], None]] = None,
    owner_uid: int = 0,
    owner_gid: int = 0,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Copy the helper file to ``install_path``, write the LaunchDaemon
    plist, and bootstrap it. Needs sudo; ``--dry-run`` prints the plan
    without touching anything and needs neither sudo nor root (every safety
    check below is still performed, so a bad --dry-run reports exactly why
    a real install would be refused).

    ``install_dir``/``install_path``/``plist_path`` default to the real
    system paths; tests point them at a temporary root instead.
    ``owner_uid``/``owner_gid`` (tests only -- production is always root:wheel,
    the default) control both what the safety checks require existing
    ancestors to be owned by, and what gets chowned into the destinations,
    since actually chowning to root needs real root.
    ``after_source_read_hook`` (tests only) runs right after the helper
    source is read, before it's compiled/installed -- used to prove that
    mutating the source file afterwards has no effect on what gets
    installed.
    """
    install_dir = Path(install_dir) if install_dir is not None else INSTALL_DIR
    install_path = Path(install_path) if install_path is not None else (install_dir / "fm350mac-helper")
    plist_path = Path(plist_path) if plist_path is not None else PLIST_PATH
    dry_run = args.dry_run

    if not dry_run and geteuid() != 0:
        print("fm350mac helper install must be run with sudo.", file=sys.stderr)
        return 1

    try:
        allowed_uid = _resolve_allowed_uid(args.allowed_uid)
        _print_step(f"allowed uid: {allowed_uid}")

        if not Path(PYTHON3).is_file():
            raise HelperInstallError(f"{PYTHON3} not found; refusing to install without the system Python")
        py_owner = os.stat(PYTHON3).st_uid
        if py_owner != 0:
            raise HelperInstallError(f"{PYTHON3} isn't root-owned (uid {py_owner}); refusing to install")

        for check_dir, what in ((install_dir, "install directory"), (plist_path.parent, "LaunchDaemon directory")):
            err = _check_dir_safe(check_dir, expected_uid=owner_uid)
            if err:
                raise HelperInstallError(f"unsafe {what}: {err}")
        for leaf in (install_path, plist_path):
            err = _check_leaf_safe(leaf)
            if err:
                raise HelperInstallError(err)

        data = _read_helper_source(helper_source)
        if after_source_read_hook is not None:
            after_source_read_hook()

        _print_step(f"checking {helper_source.name} compiles under {PYTHON3} -I -S")
        with tempfile.TemporaryDirectory(prefix="fm350mac-helper-check-") as check_dir:
            os.chmod(check_dir, 0o700)
            check_copy = os.path.join(check_dir, "fm350mac_helper.py")
            with open(check_copy, "wb") as f:
                f.write(data)
            check = compile_runner([PYTHON3, "-I", "-S", "-m", "py_compile", check_copy], capture_output=True, text=True)
        if check.returncode != 0:
            raise HelperInstallError(f"helper failed to compile under {PYTHON3} -I -S:\n{check.stderr}")
    except HelperInstallError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    _print_step(f"create {install_dir} if missing (root:wheel 0755)")
    _print_step(f"install {install_path} (root:wheel 0755)")
    _print_step(f"write {plist_path} (root:wheel 0644)")
    _print_step(f"{LAUNCHCTL} bootout system/{PLIST_LABEL} (ignore failure: replaces a running helper)")
    _print_step(f"{LAUNCHCTL} bootstrap system {plist_path}")

    if dry_run:
        print("(dry run: nothing was changed)")
        return 0

    _ensure_dir_tree(install_dir, uid=owner_uid, gid=owner_gid)
    _write_root_file(install_path, data, 0o755, uid=owner_uid, gid=owner_gid)
    _write_root_file(plist_path, _plist_xml(allowed_uid, install_path).encode(), 0o644, uid=owner_uid, gid=owner_gid)

    # A helper that's already running (it stays resident once activated)
    # would otherwise keep serving the old code: boot it out first. Failure
    # just means nothing was loaded.
    launchctl_runner([LAUNCHCTL, "bootout", f"system/{PLIST_LABEL}"], capture_output=True, text=True)
    # bootout returns before launchd has finished unloading the old job; a
    # bootstrap in that window fails with "5: Input/output error" (seen on
    # macOS 27). Wait until the job is really gone, then retry a few times.
    for _ in range(_BOOTOUT_WAIT_POLLS):
        probe = launchctl_runner([LAUNCHCTL, "print", f"system/{PLIST_LABEL}"], capture_output=True, text=True)
        if probe.returncode != 0:
            break
        sleep(_BOOTOUT_WAIT_INTERVAL_S)

    for attempt in range(1, _BOOTSTRAP_ATTEMPTS + 1):
        result = launchctl_runner([LAUNCHCTL, "bootstrap", "system", str(plist_path)], capture_output=True, text=True)
        if result.returncode == 0:
            break
        if attempt < _BOOTSTRAP_ATTEMPTS:
            sleep(_BOOTSTRAP_RETRY_INTERVAL_S)
    else:
        print(f"launchctl bootstrap failed: {result.stderr.strip()}", file=sys.stderr)
        return 1

    print(f"installed. The helper will accept connections from uid {allowed_uid}.")
    return 0


def cmd_helper_uninstall(
    args: argparse.Namespace,
    *,
    geteuid=os.geteuid,
    launchctl_runner=subprocess.run,
    install_path: Optional[Path] = None,
    plist_path: Optional[Path] = None,
    log_path: Optional[Path] = None,
) -> int:
    """Reverse ``install``: ``launchctl bootout``, then remove the plist, the
    installed helper binary and the helper's log file. Needs sudo;
    ``--dry-run`` needs neither.
    """
    install_path = Path(install_path) if install_path is not None else INSTALL_PATH
    plist_path = Path(plist_path) if plist_path is not None else PLIST_PATH
    log_path = Path(log_path) if log_path is not None else Path(_LOG_PATH)
    dry_run = args.dry_run
    if not dry_run and geteuid() != 0:
        print("fm350mac helper uninstall must be run with sudo.", file=sys.stderr)
        return 1

    _print_step(f"{LAUNCHCTL} bootout system/{PLIST_LABEL}")
    _print_step(f"remove {plist_path}")
    _print_step(f"remove {install_path}")
    _print_step(f"remove {log_path}")

    if dry_run:
        print("(dry run: nothing was changed)")
        return 0

    result = launchctl_runner([LAUNCHCTL, "bootout", f"system/{PLIST_LABEL}"], capture_output=True, text=True)
    if result.returncode != 0:
        print(f"launchctl bootout: {result.stderr.strip()} (continuing to remove files anyway)", file=sys.stderr)

    for path in (plist_path, install_path, log_path):
        try:
            path.unlink()
        except FileNotFoundError:
            pass

    print("uninstalled.")
    return 0


def _describe_path(path: Path) -> str:
    try:
        st = path.stat()
    except FileNotFoundError:
        return "missing"
    owner = pwd.getpwuid(st.st_uid).pw_name if st.st_uid == 0 else str(st.st_uid)
    return f"present (owner={owner}, mode={oct(stat.S_IMODE(st.st_mode))})"


def cmd_helper_status(
    _args: argparse.Namespace,
    *,
    helper_probe=helper_client.probe,
    install_path: Optional[Path] = None,
    plist_path: Optional[Path] = None,
    helper_source: Path = _HELPER_SOURCE,
) -> int:
    """Report file presence/ownership, socket presence, whether the installed
    helper matches the packaged source, and a hello round trip. Never needs
    root.
    """
    install_path = Path(install_path) if install_path is not None else INSTALL_PATH
    plist_path = Path(plist_path) if plist_path is not None else PLIST_PATH
    print(f"helper binary : {install_path}: {_describe_path(install_path)}")
    print(f"LaunchDaemon  : {plist_path}: {_describe_path(plist_path)}")
    print(f"socket        : {SOCKET_PATH}: {_describe_path(Path(SOCKET_PATH))}")
    try:
        installed_bytes = install_path.read_bytes()
        packaged_bytes = Path(helper_source).read_bytes()
    except OSError:
        pass  # not installed (already reported above) or source unreadable: nothing to compare
    else:
        if installed_bytes != packaged_bytes:
            print("installed helper is out of date — run `fm350mac helper install`")

    client = helper_probe()
    if client is None:
        print("helper        : not reachable (not installed, not running, or a protocol mismatch)")
        return 1
    print(f"helper        : reachable (pid {client.pid})")
    client.close()
    return 0
