"""Tests for `fm350mac helper install|uninstall|status` (helper_admin.py).
No sudo, and nothing under /usr/local, /Library or /var/run is ever touched:
every filesystem/launchctl/py_compile call is either skipped by --dry-run or
goes through an injected fake or a temporary directory this test owns.

Chowning to root (uid 0) is impossible without real root, so the actual
mutating helpers (_ensure_dir_tree/_write_root_file) and the safety checks
(_check_dir_safe) take an injectable "expected/target owner" that tests set
to their own uid/gid to exercise the exact same code paths for real.

_check_dir_safe() walks every existing ancestor of a path. pytest's
tmp_path fixture lives deep under a real, root-owned system prefix
(/private/var/folders/.../T/...), so absolute tmp_path-based paths can't be
used to test "every ancestor is owned by *my* uid" -- some ancestors really
are root's. Tests that need that instead `monkeypatch.chdir(tmp_path)` and
use a relative Path, whose `.parents` stop at "." (resolved against cwd),
so the walk never leaves the temp directory.
"""

from __future__ import annotations

import argparse
import os
import subprocess

from fm350mac import helper_admin


def _args(dry_run=True, allowed_uid=None):
    return argparse.Namespace(dry_run=dry_run, allowed_uid=allowed_uid)


def _ok(argv):
    return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")


def _fail(argv, stderr="boom"):
    return subprocess.CompletedProcess(argv, 1, stdout="", stderr=stderr)


# --- item 5: SUDO_UID / --allowed-uid / refusing uid 0 ----------------------


def test_resolve_allowed_uid_uses_explicit_value(monkeypatch):
    monkeypatch.delenv("SUDO_UID", raising=False)
    assert helper_admin._resolve_allowed_uid(42) == 42


def test_resolve_allowed_uid_uses_sudo_uid_env_when_no_explicit_value(monkeypatch):
    monkeypatch.setenv("SUDO_UID", "777")
    assert helper_admin._resolve_allowed_uid(None) == 777


def test_resolve_allowed_uid_requires_something_when_sudo_uid_is_absent(monkeypatch):
    monkeypatch.delenv("SUDO_UID", raising=False)
    try:
        helper_admin._resolve_allowed_uid(None)
        assert False, "expected HelperInstallError"
    except helper_admin.HelperInstallError as exc:
        assert "--allowed-uid" in str(exc)


def test_resolve_allowed_uid_rejects_non_numeric_sudo_uid(monkeypatch):
    monkeypatch.setenv("SUDO_UID", "not-a-number")
    try:
        helper_admin._resolve_allowed_uid(None)
        assert False, "expected HelperInstallError"
    except helper_admin.HelperInstallError as exc:
        assert "SUDO_UID" in str(exc)


def test_resolve_allowed_uid_refuses_explicit_root():
    try:
        helper_admin._resolve_allowed_uid(0)
        assert False, "expected HelperInstallError"
    except helper_admin.HelperInstallError as exc:
        assert "uid 0" in str(exc) or "root" in str(exc)


def test_resolve_allowed_uid_refuses_root_from_sudo_uid_env(monkeypatch):
    monkeypatch.setenv("SUDO_UID", "0")
    try:
        helper_admin._resolve_allowed_uid(None)
        assert False, "expected HelperInstallError"
    except helper_admin.HelperInstallError:
        pass


def test_install_refuses_without_sudo_uid_or_explicit_allowed_uid(capsys, monkeypatch):
    monkeypatch.delenv("SUDO_UID", raising=False)
    rc = helper_admin.cmd_helper_install(_args(dry_run=True, allowed_uid=None))
    assert rc == 1
    assert "--allowed-uid" in capsys.readouterr().err


def test_install_refuses_allowed_uid_zero(capsys):
    rc = helper_admin.cmd_helper_install(_args(dry_run=True, allowed_uid=0))
    assert rc == 1
    err = capsys.readouterr().err
    assert "uid 0" in err or "root" in err


# --- install: root check, dry-run, compile check ----------------------------


def test_install_dry_run_needs_no_root_and_changes_nothing(capsys):
    rc = helper_admin.cmd_helper_install(
        _args(dry_run=True, allowed_uid=501),
        geteuid=lambda: 501,  # not root
        compile_runner=lambda argv, **kw: _ok(argv),
        launchctl_runner=lambda argv, **kw: _ok(argv),
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "dry run" in out
    assert str(helper_admin.INSTALL_PATH) in out
    assert str(helper_admin.PLIST_PATH) in out


def test_install_without_sudo_refuses(capsys):
    rc = helper_admin.cmd_helper_install(_args(dry_run=False, allowed_uid=501), geteuid=lambda: 501)
    assert rc == 1
    assert "sudo" in capsys.readouterr().err


def test_install_dry_run_reports_allowed_uid_from_sudo_uid_env(capsys, monkeypatch):
    monkeypatch.setenv("SUDO_UID", "777")
    rc = helper_admin.cmd_helper_install(
        _args(dry_run=True), geteuid=lambda: 0,
        compile_runner=lambda argv, **kw: _ok(argv), launchctl_runner=lambda argv, **kw: _ok(argv),
    )
    assert rc == 0
    assert "allowed uid: 777" in capsys.readouterr().out


def test_install_explicit_allowed_uid_overrides_sudo_uid_env(capsys, monkeypatch):
    monkeypatch.setenv("SUDO_UID", "777")
    rc = helper_admin.cmd_helper_install(
        _args(dry_run=True, allowed_uid=42), geteuid=lambda: 0,
        compile_runner=lambda argv, **kw: _ok(argv), launchctl_runner=lambda argv, **kw: _ok(argv),
    )
    assert rc == 0
    assert "allowed uid: 42" in capsys.readouterr().out


def test_install_refuses_if_helper_fails_to_compile(capsys):
    rc = helper_admin.cmd_helper_install(
        _args(dry_run=True, allowed_uid=501), geteuid=lambda: 0,
        compile_runner=lambda argv, **kw: _fail(argv, "SyntaxError: nope"),
        launchctl_runner=lambda argv, **kw: _ok(argv),
    )
    assert rc == 1
    assert "failed to compile" in capsys.readouterr().err


# --- item 1: symlink-following install --------------------------------------


def test_check_dir_safe_accepts_a_normal_directory_owned_by_the_expected_uid(tmp_path, monkeypatch):
    # A relative path's ancestors stop at "." (resolved against cwd), so this
    # doesn't walk up into the real (root-owned) /private/var/... prefix
    # tmp_path sits under -- see the module-level note in this file's intro.
    monkeypatch.chdir(tmp_path)
    d = helper_admin.Path("libexec")
    d.mkdir(mode=0o755)
    assert helper_admin._check_dir_safe(d, expected_uid=os.getuid()) is None


def test_check_dir_safe_rejects_a_directory_not_owned_by_the_expected_uid(tmp_path):
    d = tmp_path / "libexec"
    d.mkdir(mode=0o755)
    err = helper_admin._check_dir_safe(d, expected_uid=0)
    assert err is not None
    assert "root-owned" in err


def test_check_dir_safe_rejects_a_group_or_other_writable_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    d = helper_admin.Path("libexec")
    d.mkdir(mode=0o777)
    os.chmod(d, 0o777)  # mkdir's mode is affected by umask; force it
    err = helper_admin._check_dir_safe(d, expected_uid=os.getuid())
    assert err is not None
    assert "writable" in err


def test_check_dir_safe_rejects_a_symlinked_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    real_dir = helper_admin.Path("real")
    real_dir.mkdir(mode=0o755)
    link = helper_admin.Path("link")
    link.symlink_to(real_dir)
    # Every real ancestor here (".", i.e. tmp_path, and "real") is owned by
    # us, so the only thing that can make this fail is the symlink itself.
    err = helper_admin._check_dir_safe(link, expected_uid=os.getuid())
    assert err is not None
    assert "symlink" in err


def test_check_dir_safe_allows_missing_components():
    missing = helper_admin.Path("/no/such/path/at/all")
    assert helper_admin._check_dir_safe(missing, expected_uid=os.getuid()) is None


def test_check_leaf_safe_rejects_an_existing_symlink(tmp_path):
    target = tmp_path / "target"
    target.write_text("x")
    link = tmp_path / "leaf"
    link.symlink_to(target)
    err = helper_admin._check_leaf_safe(link)
    assert err is not None
    assert "symlink" in err


def test_check_leaf_safe_allows_a_missing_or_regular_file(tmp_path):
    assert helper_admin._check_leaf_safe(tmp_path / "missing") is None
    regular = tmp_path / "regular"
    regular.write_text("x")
    assert helper_admin._check_leaf_safe(regular) is None


def test_write_root_file_replaces_a_symlink_destination_atomically(tmp_path):
    """os.rename() replaces the symlink's directory entry outright, it never
    follows it -- so the symlink's target is left completely untouched.
    """
    evil_target = tmp_path / "evil_target"
    evil_target.write_text("do not touch me")
    dest = tmp_path / "dest"
    dest.symlink_to(evil_target)

    helper_admin._write_root_file(dest, b"installed content", 0o644, uid=os.getuid(), gid=os.getgid())

    assert not dest.is_symlink()
    assert dest.read_bytes() == b"installed content"
    assert evil_target.read_text() == "do not touch me"
    st = dest.stat()
    assert oct(st.st_mode & 0o777) == oct(0o644)
    # No leftover temp file: only the two real files remain.
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted([evil_target.name, dest.name])


def test_write_root_file_leaves_no_temp_file_behind():
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as d:
        dest = Path(d) / "dest"
        helper_admin._write_root_file(dest, b"data", 0o600, uid=os.getuid(), gid=os.getgid())
        assert sorted(os.listdir(d)) == ["dest"]


def test_ensure_dir_tree_only_creates_missing_components(tmp_path):
    existing = tmp_path / "existing"
    existing.mkdir(mode=0o750)
    os.chmod(existing, 0o750)
    before = existing.stat()

    target = existing / "a" / "b"
    helper_admin._ensure_dir_tree(target, uid=os.getuid(), gid=os.getgid())

    assert target.is_dir()
    assert (existing / "a").stat().st_mode & 0o777 == 0o755
    after = existing.stat()
    assert before.st_mode == after.st_mode  # the pre-existing directory was never touched


def test_install_refuses_when_install_dir_is_not_owned_by_root(capsys, tmp_path):
    """A real (non-mocked) reproduction of "/usr/local is user-owned on
    Intel Homebrew": install_dir here is a perfectly normal directory this
    test process owns -- which is never root, so the default expected_uid=0
    check must refuse it.
    """
    install_dir = tmp_path / "libexec"
    install_dir.mkdir()
    rc = helper_admin.cmd_helper_install(
        _args(dry_run=True, allowed_uid=501),
        geteuid=lambda: 0,
        install_dir=install_dir,
        compile_runner=lambda argv, **kw: _ok(argv),
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "unsafe install directory" in err
    assert "root-owned" in err


def test_install_refuses_when_install_path_is_a_symlink(capsys, tmp_path, monkeypatch):
    install_dir = tmp_path / "libexec"
    install_dir.mkdir()
    evil_target = tmp_path / "evil"
    evil_target.write_text("x")
    install_path = install_dir / "fm350mac-helper"
    install_path.symlink_to(evil_target)

    # Only the leaf-symlink check is under test here: pretend the
    # directories themselves are safe (already covered by the ancestor
    # tests above), same as a real root-owned /usr/local/libexec would be.
    monkeypatch.setattr(helper_admin, "_check_dir_safe", lambda path, expected_uid=0: None)

    rc = helper_admin.cmd_helper_install(
        _args(dry_run=True, allowed_uid=501),
        geteuid=lambda: 0,
        install_dir=install_dir,
        install_path=install_path,
        compile_runner=lambda argv, **kw: _ok(argv),
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "symlink" in err
    assert evil_target.read_text() == "x"


def test_install_refuses_when_plist_path_is_a_symlink(capsys, tmp_path, monkeypatch):
    install_dir = tmp_path / "libexec"
    install_dir.mkdir()
    evil_target = tmp_path / "evil.plist"
    evil_target.write_text("x")
    plist_path = tmp_path / "de.fm350mac.helper.plist"
    plist_path.symlink_to(evil_target)

    monkeypatch.setattr(helper_admin, "_check_dir_safe", lambda path, expected_uid=0: None)

    rc = helper_admin.cmd_helper_install(
        _args(dry_run=True, allowed_uid=501),
        geteuid=lambda: 0,
        install_dir=install_dir,
        plist_path=plist_path,
        compile_runner=lambda argv, **kw: _ok(argv),
    )
    assert rc == 1
    assert "symlink" in capsys.readouterr().err


def test_install_real_end_to_end_with_injected_owner_creates_root_wheel_like_files(tmp_path, monkeypatch):
    """A full (non-dry-run) install into a temp root, using the test's own
    uid/gid in place of root:wheel (chowning to real root needs real root --
    see the module docstring) -- exercises _ensure_dir_tree/_write_root_file
    and the plist/launchctl wiring for real. Relative install_dir/plist_path
    (see the chdir note above _check_dir_safe's tests) so the ancestor-safety
    check doesn't walk into the real, root-owned /private/var/... prefix
    tmp_path sits under.
    """
    monkeypatch.chdir(tmp_path)
    install_dir = helper_admin.Path("libexec")
    plist_path = helper_admin.Path("de.fm350mac.helper.plist")
    launchctl_calls = []

    def fake_launchctl(argv, **kw):
        launchctl_calls.append(argv)
        return _ok(argv)

    rc = helper_admin.cmd_helper_install(
        _args(dry_run=False, allowed_uid=501),
        geteuid=lambda: 0,
        install_dir=install_dir,
        plist_path=plist_path,
        compile_runner=lambda argv, **kw: _ok(argv),
        launchctl_runner=fake_launchctl,
        owner_uid=os.getuid(),
        owner_gid=os.getgid(),
    )
    assert rc == 0
    install_path = install_dir / "fm350mac-helper"
    assert install_path.read_bytes() == helper_admin._HELPER_SOURCE.read_bytes()
    assert install_path.stat().st_mode & 0o777 == 0o755
    assert plist_path.exists()
    assert plist_path.stat().st_mode & 0o777 == 0o644
    assert "501" in plist_path.read_text()
    assert launchctl_calls == [["launchctl", "bootstrap", "system", str(plist_path)]]


# --- item 2: TOCTOU between the compile check and the install ---------------


def test_read_helper_source_rejects_a_symlink(tmp_path):
    target = tmp_path / "real.py"
    target.write_text("print('hi')\n")
    link = tmp_path / "link.py"
    link.symlink_to(target)
    try:
        helper_admin._read_helper_source(link)
        assert False, "expected HelperInstallError"
    except helper_admin.HelperInstallError as exc:
        assert "isn't a regular file" in str(exc) or "symlink" in str(exc)


def test_read_helper_source_rejects_oversize_files(tmp_path):
    big = tmp_path / "big.py"
    big.write_bytes(b"x" * (helper_admin._MAX_HELPER_SOURCE_BYTES + 1))
    try:
        helper_admin._read_helper_source(big)
        assert False, "expected HelperInstallError"
    except helper_admin.HelperInstallError as exc:
        assert "larger than" in str(exc)


def test_install_uses_the_bytes_read_before_the_source_was_modified(tmp_path, monkeypatch):
    """Mutating the source file *after* it was read (simulating another
    process winning a TOCTOU race) must not change what gets installed:
    both the compile check and the final install use the same in-memory
    bytes read once at the start.
    """
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "fm350mac_helper.py"
    source.write_text("original_marker = 1\n")
    install_dir = helper_admin.Path("libexec")
    plist_path = helper_admin.Path("de.fm350mac.helper.plist")

    def mutate_source_after_read():
        source.write_text("mutated_marker = 2\n")

    rc = helper_admin.cmd_helper_install(
        _args(dry_run=False, allowed_uid=501),
        geteuid=lambda: 0,
        helper_source=source,
        install_dir=install_dir,
        plist_path=plist_path,
        compile_runner=lambda argv, **kw: _ok(argv),
        launchctl_runner=lambda argv, **kw: _ok(argv),
        after_source_read_hook=mutate_source_after_read,
        owner_uid=os.getuid(),
        owner_gid=os.getgid(),
    )
    assert rc == 0
    installed = (install_dir / "fm350mac-helper").read_text()
    assert "original_marker" in installed
    assert "mutated_marker" not in installed
    assert source.read_text() == "mutated_marker = 2\n"  # the source itself was indeed changed


def test_compile_check_runs_against_the_same_bytes_that_get_installed(tmp_path):
    """The compile_runner is handed a path to a private temp copy, not
    `helper_source` itself -- assert its content matches what's read, not
    whatever might currently be on disk at `helper_source`.
    """
    source = tmp_path / "fm350mac_helper.py"
    source.write_text("marker_v1 = True\n")
    seen = {}

    def fake_compile(argv, **kw):
        checked_path = argv[-1]
        seen["content"] = open(checked_path, "rb").read()
        seen["path"] = checked_path
        return _ok(argv)

    rc = helper_admin.cmd_helper_install(
        _args(dry_run=True, allowed_uid=501),
        geteuid=lambda: 0,
        helper_source=source,
        compile_runner=fake_compile,
    )
    assert rc == 0
    assert seen["content"] == b"marker_v1 = True\n"
    assert seen["path"] != str(source)  # a private temp copy, not the source itself


# --- uninstall / status (unaffected by this round, kept for regression) ----


def test_uninstall_dry_run_needs_no_root(capsys):
    rc = helper_admin.cmd_helper_uninstall(_args(dry_run=True), geteuid=lambda: 501)
    assert rc == 0
    out = capsys.readouterr().out
    assert "dry run" in out
    assert str(helper_admin.PLIST_PATH) in out


def test_uninstall_without_sudo_refuses(capsys):
    rc = helper_admin.cmd_helper_uninstall(_args(dry_run=False), geteuid=lambda: 501)
    assert rc == 1
    assert "sudo" in capsys.readouterr().err


class _FakeHelperClient:
    def __init__(self, pid=4242):
        self.pid = pid
        self.closed = False

    def close(self):
        self.closed = True


def test_status_reports_unreachable_when_the_probe_fails(capsys):
    rc = helper_admin.cmd_helper_status(None, helper_probe=lambda: None)
    assert rc == 1
    assert "not reachable" in capsys.readouterr().out


def test_status_reports_reachable_pid_when_the_probe_succeeds(capsys):
    fake = _FakeHelperClient(pid=4242)
    rc = helper_admin.cmd_helper_status(None, helper_probe=lambda: fake)
    assert rc == 0
    out = capsys.readouterr().out
    assert "reachable" in out
    assert "4242" in out
    assert fake.closed


def test_status_reports_missing_files_when_nothing_is_installed(capsys, monkeypatch):
    monkeypatch.setattr(helper_admin, "INSTALL_PATH", helper_admin.INSTALL_PATH.__class__("/nonexistent/fm350mac-helper"))
    monkeypatch.setattr(helper_admin, "PLIST_PATH", helper_admin.PLIST_PATH.__class__("/nonexistent/de.fm350mac.helper.plist"))
    rc = helper_admin.cmd_helper_status(None, helper_probe=lambda: None)
    assert rc == 1
    out = capsys.readouterr().out
    assert "missing" in out
