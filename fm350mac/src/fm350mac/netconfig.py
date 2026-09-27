"""ifconfig/route/scutil wrappers to bring a utun interface up as the FM350's
data path, and to cleanly restore whatever was changed on teardown.

All external commands are run as argument lists (never through a shell), and
every command run is recorded on the instance so tests can assert on it
without touching the network. ``runner`` (default ``subprocess.run``) is
injectable so tests can fake command execution without a real subprocess.
"""

from __future__ import annotations

import logging
import re
import subprocess

_log = logging.getLogger(__name__)

_DNS_KEY = "State:/Network/Service/fm350mac/DNS"


class NetConfig:
    """Runs (or, in dry-run mode, only records) the network setup commands."""

    def __init__(self, dry_run: bool = False, runner=subprocess.run) -> None:
        self.dry_run = dry_run
        self._runner = runner
        self.commands: list[list[str]] = []
        self._prev_default_restore_cmd: list[str] | None = None
        self._default_route_added = False
        self._default_route_restore_pending = False
        self._default_route_ifname: str | None = None
        self._dns_set = False
        self._configured_iface: str | None = None
        self._host_route: str | None = None

    def _run(self, argv: list[str]) -> str:
        """Run ``argv`` (or just record it in dry-run mode) and return stdout."""
        self.commands.append(argv)
        if self.dry_run:
            _log.info("[dry-run] %s", " ".join(argv))
            return ""
        result = self._runner(argv, capture_output=True, text=True, check=True)
        return result.stdout

    def configure_interface(self, ifname: str, ip: str, mtu: int = 1500) -> None:
        """Bring up ``ifname`` as a point-to-point interface with ``ip``."""
        self._run(["ifconfig", ifname, "inet", ip, ip, "mtu", str(mtu), "up"])
        self._configured_iface = ifname

    def reconfigure_address(self, ifname: str, old_ip: str, new_ip: str) -> None:
        """Change a running point-to-point interface's address.

        Used when the modem reassigns an IP mid-session (reconnect after a
        loss of registration): the old alias is removed first, since macOS
        otherwise stacks addresses on the same interface rather than
        replacing one.
        """
        self._run(["ifconfig", ifname, "inet", old_ip, "delete"])
        self._run(["ifconfig", ifname, "inet", new_ip, new_ip, "mtu", "1500", "up"])
        self._configured_iface = ifname

    def add_host_route(self, ifname: str, host_ip: str) -> None:
        """Add a host route to ``host_ip`` via ``ifname`` (used by loopback
        mode, which must never touch the default route).
        """
        self._run(["route", "add", "-host", host_ip, "-interface", ifname])
        self._host_route = host_ip

    def add_default_route(self, ifname: str) -> None:
        """Replace the default route with one through ``ifname``.

        The previous default (by gateway if it had one, else by interface)
        is saved and restored on teardown. Reading the current default route
        is read-only and always attempted (even in dry-run), since it needs
        no root and doesn't mutate anything; only the actual route/ifconfig
        changes are skipped in dry-run mode. If there was no previous
        default route at all, teardown just removes ours.

        Idempotent: if our route for ``ifname`` is already installed (e.g.
        ``up --supervise`` rebuilding a session after a USB re-enumeration,
        without a full teardown() in between), this is a no-op and the
        "previous default" is never re-captured -- re-reading it at that
        point would just see our own route and "restoring" it on teardown
        would leave the Mac's real default gateway gone for good. If
        ``ifname`` differs from what's currently installed (a new utun after
        a rebuild), the route is repointed without touching the original
        captured default.
        """
        if self._default_route_added:
            exists, current_iface = self._current_default_iface()
            if exists and current_iface != self._default_route_ifname:
                _log.warning(
                    "default route moved from %s to %s; leaving the replacement route untouched",
                    self._default_route_ifname,
                    current_iface or "an unknown interface",
                )
                return
            if exists and self._default_route_ifname == ifname:
                return
            if exists:
                self._run(["route", "delete", "default"])
            try:
                self._run(["route", "add", "default", "-interface", ifname])
            except Exception:
                # Our old route is gone; leave the original gateway available
                # for teardown if installing the replacement fails.
                self._default_route_added = False
                self._default_route_ifname = None
                self._restore_previous_default()
                raise
            self._default_route_ifname = ifname
            return

        # A previous add failed after deleting the original route, and its
        # immediate rollback failed too. Do not re-read an empty route table
        # and overwrite the saved restore command: a successful retry still
        # has to restore that original route during teardown.
        if self._default_route_restore_pending:
            try:
                self._run(["route", "add", "default", "-interface", ifname])
            except Exception:
                self._restore_previous_default()
                raise
            self._default_route_restore_pending = False
            self._default_route_added = True
            self._default_route_ifname = ifname
            return

        prev_gateway = None
        prev_iface = None
        try:
            out = self._runner(
                ["route", "-n", "get", "default"], capture_output=True, text=True, check=True
            ).stdout
            gw_match = re.search(r"gateway:\s*(\S+)", out)
            if_match = re.search(r"interface:\s*(\S+)", out)
            prev_gateway = gw_match.group(1) if gw_match else None
            prev_iface = if_match.group(1) if if_match else None
        except subprocess.CalledProcessError:
            _log.warning("could not read previous default route", exc_info=True)

        # Never capture a route through the interface we're about to own: if
        # it's already the "current default", there is no real previous
        # default to restore (guards a caller that skipped teardown()
        # between two add_default_route() calls for the same interface).
        if prev_iface == ifname:
            prev_gateway = None
            prev_iface = None

        if prev_gateway:
            self._prev_default_restore_cmd = ["route", "add", "default", prev_gateway]
        elif prev_iface:
            self._prev_default_restore_cmd = ["route", "add", "default", "-interface", prev_iface]
        else:
            self._prev_default_restore_cmd = None

        if self._prev_default_restore_cmd is not None:
            self._run(["route", "delete", "default"])
            self._default_route_restore_pending = True
        try:
            self._run(["route", "add", "default", "-interface", ifname])
        except Exception:
            self._restore_previous_default()
            raise
        self._default_route_restore_pending = False
        self._default_route_added = True
        self._default_route_ifname = ifname

    def _current_default_iface(self) -> tuple[bool, str | None]:
        """Return whether a default route exists and its interface, if known."""
        if self.dry_run and self._default_route_added:
            # No command changed the real route during a dry run. Inspect the
            # route we would have installed, not the host's unchanged route.
            return True, self._default_route_ifname
        try:
            out = self._runner(["route", "-n", "get", "default"], capture_output=True, text=True, check=True).stdout
        except subprocess.CalledProcessError:
            return False, None
        match = re.search(r"interface:\s*(\S+)", out)
        return True, match.group(1) if match else None

    def _restore_previous_default(self) -> None:
        if self._prev_default_restore_cmd is None:
            self._default_route_restore_pending = False
            return
        try:
            self._run(self._prev_default_restore_cmd)
        except Exception:
            self._default_route_restore_pending = True
            _log.exception("failed to restore previous default route")
        else:
            self._default_route_restore_pending = False
            self._prev_default_restore_cmd = None

    def set_dns(self, servers: list[str]) -> None:
        """Publish ``servers`` as the DNS resolver via a dynamic-store key."""
        if not servers:
            return
        script = (
            f"d.init\n"
            f"d.add ServerAddresses * {' '.join(servers)}\n"
            f"set {_DNS_KEY}\n"
        )
        self._run_scutil(script)
        self._dns_set = True

    def _run_scutil(self, script: str) -> str:
        self.commands.append(["scutil"] + script.strip().splitlines())
        if self.dry_run:
            _log.info("[dry-run] scutil <<EOF\n%sEOF", script)
            return ""
        result = self._runner(["scutil"], input=script, capture_output=True, text=True, check=True)
        return result.stdout

    def teardown(self) -> None:
        """Undo whatever was configured, in reverse order.

        Best-effort: a failure in one step is logged but never skips the
        rest. Failed route cleanup stays pending for a later retry; completed
        steps remain idempotent.
        """
        if self._dns_set:
            self._dns_set = False
            try:
                self._run_scutil(f"remove {_DNS_KEY}\n")
            except Exception:
                _log.exception("failed to remove DNS key")
        if self._default_route_added or self._default_route_restore_pending:
            try:
                exists, current_iface = self._current_default_iface()
            except Exception:
                _log.exception("could not check current default route; leaving it unchanged")
            else:
                if exists and current_iface != self._default_route_ifname:
                    # A different service has installed a default route. It
                    # owns that route; our old gateway is stale now.
                    self._default_route_added = False
                    self._default_route_restore_pending = False
                    self._default_route_ifname = None
                    self._prev_default_restore_cmd = None
                else:
                    if exists:
                        try:
                            self._run(["route", "delete", "default"])
                        except Exception:
                            _log.exception("failed to delete our default route")
                            exists = True
                        else:
                            exists = False
                    if not exists:
                        self._default_route_added = False
                        self._default_route_ifname = None
                        self._restore_previous_default()
        if self._host_route:
            host_ip, self._host_route = self._host_route, None
            try:
                self._run(["route", "delete", "-host", host_ip])
            except Exception:
                _log.exception("failed to delete host route to %s", host_ip)
        if self._configured_iface:
            iface, self._configured_iface = self._configured_iface, None
            try:
                self._run(["ifconfig", iface, "down"])
            except Exception:
                _log.exception("failed to bring down %s", iface)
