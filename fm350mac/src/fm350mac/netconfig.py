"""ifconfig/route/scutil wrappers to bring a utun interface up as the FM350's
data path, and to cleanly restore whatever was changed on teardown.

All external commands are run as argument lists (never through a shell), and
every command run is recorded on the instance so tests can assert on it
without touching the network. ``runner`` (default ``subprocess.run``) is
injectable so tests can fake command execution without a real subprocess.
"""

from __future__ import annotations

import ipaddress
import logging
import re
import subprocess

_log = logging.getLogger(__name__)

_DNS_KEY = "State:/Network/Service/fm350mac/DNS"

# Absolute paths only: never resolved through $PATH (this runs as root on the
# --no-helper path).
IFCONFIG = "/sbin/ifconfig"
ROUTE = "/sbin/route"
SCUTIL = "/usr/sbin/scutil"
COMMAND_TIMEOUT_S = 10

MAX_HOST_ROUTES = 8  # same per-connection limit as the root helper

_BROADCAST = ipaddress.IPv4Address("255.255.255.255")
_THIS_NETWORK = ipaddress.ip_network("0.0.0.0/8")


def valid_unicast_ipv4(value: object) -> str | None:
    """The normalized address if ``value`` is a usable unicast IPv4 address,
    else None. Same rules as the root helper's ``valid_assigned_ipv4``
    (deliberately duplicated: the helper can't import this package): rejects
    0/8, 127/8, 169.254/16, multicast and reserved 240/4 (incl. broadcast).
    """
    if not isinstance(value, str):
        return None
    try:
        addr = ipaddress.IPv4Address(value)
    except ValueError:
        return None
    if (
        addr.is_unspecified or addr.is_multicast or addr.is_loopback or addr.is_link_local
        or addr.is_reserved or addr == _BROADCAST or addr in _THIS_NETWORK
    ):
        return None
    return str(addr)


def validate_route_host(value: str) -> str:
    """``value`` as a normalized IPv4 host-route destination. Raises ValueError."""
    normalized = valid_unicast_ipv4(value)
    if normalized is None:
        raise ValueError(f"invalid route host (need a unicast IPv4 address): {value!r}")
    return normalized


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
        self._host_routes: list[str] = []

    def _run(self, argv: list[str]) -> str:
        """Run ``argv`` (or just record it in dry-run mode) and return stdout."""
        self.commands.append(argv)
        if self.dry_run:
            _log.info("[dry-run] %s", " ".join(argv))
            return ""
        result = self._runner(argv, capture_output=True, text=True, check=True, timeout=COMMAND_TIMEOUT_S)
        return result.stdout

    def configure_interface(self, ifname: str, ip: str, mtu: int = 1500) -> None:
        """Bring up ``ifname`` as a point-to-point interface with ``ip``."""
        self._run([IFCONFIG, ifname, "inet", ip, ip, "mtu", str(mtu), "up"])
        self._configured_iface = ifname

    def reconfigure_address(self, ifname: str, old_ip: str, new_ip: str) -> None:
        """Change a running point-to-point interface's address.

        Used when the modem reassigns an IP mid-session (reconnect after a
        loss of registration): the old alias is removed first, since macOS
        otherwise stacks addresses on the same interface rather than
        replacing one.
        """
        self._run([IFCONFIG, ifname, "inet", old_ip, "delete"])
        self._run([IFCONFIG, ifname, "inet", new_ip, new_ip, "mtu", "1500", "up"])
        self._configured_iface = ifname

    def add_host_route(self, ifname: str, host_ip: str) -> None:
        """Add a host route to ``host_ip`` via ``ifname`` (loopback mode's
        smoke-test route, and ``up --route-host``). At most
        ``MAX_HOST_ROUTES``; adding the same host twice is a no-op. All are
        removed (newest first) by ``remove_host_routes()``/``teardown()``.
        """
        host_ip = validate_route_host(host_ip)
        if host_ip in self._host_routes:
            return
        if len(self._host_routes) >= MAX_HOST_ROUTES:
            raise ValueError(f"at most {MAX_HOST_ROUTES} host routes are supported")
        self._run([ROUTE, "add", "-host", host_ip, "-interface", ifname])
        self._host_routes.append(host_ip)

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
                self._run([ROUTE, "delete", "default"])
            try:
                self._run([ROUTE, "add", "default", "-interface", ifname])
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
                self._run([ROUTE, "add", "default", "-interface", ifname])
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
                [ROUTE, "-n", "get", "default"], capture_output=True, text=True, check=True, timeout=COMMAND_TIMEOUT_S
            ).stdout
            gw_match = re.search(r"gateway:\s*(\S+)", out)
            if_match = re.search(r"interface:\s*(\S+)", out)
            prev_gateway = gw_match.group(1) if gw_match else None
            prev_iface = if_match.group(1) if if_match else None
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            _log.warning("could not read previous default route", exc_info=True)

        # Never capture a route through the interface we're about to own: if
        # it's already the "current default", there is no real previous
        # default to restore (guards a caller that skipped teardown()
        # between two add_default_route() calls for the same interface).
        if prev_iface == ifname:
            prev_gateway = None
            prev_iface = None

        if prev_gateway:
            self._prev_default_restore_cmd = [ROUTE, "add", "default", prev_gateway]
        elif prev_iface:
            self._prev_default_restore_cmd = [ROUTE, "add", "default", "-interface", prev_iface]
        else:
            self._prev_default_restore_cmd = None

        if self._prev_default_restore_cmd is not None:
            self._run([ROUTE, "delete", "default"])
            self._default_route_restore_pending = True
        try:
            self._run([ROUTE, "add", "default", "-interface", ifname])
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
            out = self._runner(
                [ROUTE, "-n", "get", "default"], capture_output=True, text=True, check=True, timeout=COMMAND_TIMEOUT_S
            ).stdout
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
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
        """Publish ``servers`` as the DNS resolver via a dynamic-store key.

        Every server is re-validated here (IPv4 only, same rules as the root
        helper) as defence in depth, since the text ends up in an scutil
        script. Raises ValueError.
        """
        if not servers:
            return
        validated = []
        for server in servers:
            normalized = valid_unicast_ipv4(server)
            if normalized is None:
                raise ValueError(f"invalid DNS server (need a unicast IPv4 address): {server!r}")
            validated.append(normalized)
        servers = validated
        script = (
            f"d.init\n"
            f"d.add ServerAddresses * {' '.join(servers)}\n"
            f"set {_DNS_KEY}\n"
        )
        self._run_scutil(script)
        self._dns_set = True

    def _run_scutil(self, script: str) -> str:
        self.commands.append([SCUTIL] + script.strip().splitlines())
        if self.dry_run:
            _log.info("[dry-run] scutil <<EOF\n%sEOF", script)
            return ""
        result = self._runner(
            [SCUTIL], input=script, capture_output=True, text=True, check=True, timeout=COMMAND_TIMEOUT_S
        )
        return result.stdout

    def clear_dns(self) -> None:
        """Remove the DNS key we published, if any. Best-effort, idempotent."""
        if self._dns_set:
            self._dns_set = False
            try:
                self._run_scutil(f"remove {_DNS_KEY}\n")
            except Exception:
                _log.exception("failed to remove DNS key")

    def remove_default_route(self) -> None:
        """Remove our default route and restore the previous one. Best-effort;
        a failed cleanup stays pending for a later retry. Idempotent.
        """
        if not (self._default_route_added or self._default_route_restore_pending):
            return
        try:
            exists, current_iface = self._current_default_iface()
        except Exception:
            _log.exception("could not check current default route; leaving it unchanged")
            return
        if exists and current_iface != self._default_route_ifname:
            # A different service has installed a default route. It
            # owns that route; our old gateway is stale now.
            self._default_route_added = False
            self._default_route_restore_pending = False
            self._default_route_ifname = None
            self._prev_default_restore_cmd = None
            return
        if exists:
            try:
                self._run([ROUTE, "delete", "default"])
            except Exception:
                _log.exception("failed to delete our default route")
                return
        self._default_route_added = False
        self._default_route_ifname = None
        self._restore_previous_default()

    def remove_host_routes(self) -> None:
        """Delete every host route we added, newest first. Best-effort.

        Same as the root helper: a route is only forgotten once its delete
        succeeded (or it was already gone), so a failed delete stays tracked
        for a later retry instead of being re-added on top of itself.
        """
        for host_ip in list(reversed(self._host_routes)):
            try:
                self._run([ROUTE, "delete", "-host", host_ip])
            except Exception as exc:
                if "not in table" in str(getattr(exc, "stderr", "") or ""):
                    _log.info("host route to %s already gone", host_ip)
                else:
                    _log.exception("failed to delete host route to %s", host_ip)
                    continue
            self._host_routes.remove(host_ip)

    def teardown(self) -> None:
        """Undo whatever was configured, in reverse order: DNS, default
        route, host routes (newest first), then the interface.

        Best-effort: a failure in one step is logged but never skips the
        rest. Failed route cleanup stays pending for a later retry; completed
        steps remain idempotent.
        """
        self.clear_dns()
        self.remove_default_route()
        self.remove_host_routes()
        if self._configured_iface:
            iface, self._configured_iface = self._configured_iface, None
            try:
                self._run([IFCONFIG, iface, "down"])
            except Exception:
                _log.exception("failed to bring down %s", iface)
