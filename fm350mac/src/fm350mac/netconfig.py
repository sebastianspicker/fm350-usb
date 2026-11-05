"""ifconfig/route/scutil wrappers to bring a utun interface up as the FM350's
data path, and to cleanly restore whatever was changed on teardown.

A thin adapter around the root helper's ``NetState`` (one implementation of
the capture/restore logic for both the helper and ``up --no-helper``/
``--dry-run``). All external commands are run as argument lists (never
through a shell), and every command run is recorded on ``commands`` so tests
can assert on it without touching the network. ``runner`` (default
``subprocess.run``) is injectable so tests can fake command execution
without a real subprocess.
"""

from __future__ import annotations

import logging
import subprocess

from .helper.fm350mac_helper import (
    COMMAND_TIMEOUT_S,
    IFCONFIG,
    MAX_HOST_ROUTES,
    ROUTE,
    SCUTIL,
    HelperProtocolError,
    NetState,
)
from .helper.fm350mac_helper import valid_assigned_ipv4 as valid_unicast_ipv4

__all__ = [
    "COMMAND_TIMEOUT_S", "IFCONFIG", "MAX_HOST_ROUTES", "ROUTE", "SCUTIL",
    "NetConfig", "valid_unicast_ipv4", "validate_route_host",
]

_log = logging.getLogger(__name__)


def validate_route_host(value: str) -> str:
    """``value`` as a normalized IPv4 host-route destination. Raises ValueError."""
    normalized = valid_unicast_ipv4(value)
    if normalized is None:
        raise ValueError(f"invalid route host (need a unicast IPv4 address): {value!r}")
    return normalized


class _NetState(NetState):
    """NetState that, in dry-run mode, only records (never runs) the
    mutating commands. Reading the current default route is read-only and
    still really runs, so the dry-run plan shows what would be restored.
    """

    def __init__(self, dry_run: bool, runner) -> None:
        super().__init__(None, runner=runner)  # ifname is bound by NetConfig._for()
        self.dry_run = dry_run

    def _run(self, argv: list[str]) -> str:
        if not self.dry_run:
            return super()._run(argv)
        self.commands.append(list(argv))
        _log.info("[dry-run] %s", " ".join(argv))
        return ""

    def _run_scutil(self, script: str) -> str:
        if not self.dry_run:
            return super()._run_scutil(script)
        self.commands.append([SCUTIL] + script.strip().splitlines())
        _log.info("[dry-run] scutil <<EOF\n%sEOF", script)
        return ""

    def _current_default_iface(self) -> tuple[bool, str | None]:
        if self.dry_run and self._default_route_added:
            # No command changed the real route during a dry run. Inspect the
            # route we would have installed, not the host's unchanged route.
            return True, self.ifname
        return super()._current_default_iface()


class NetConfig:
    """Runs (or, in dry-run mode, only records) the network setup commands
    for one utun interface (``up`` keeps the same utun across rebuilds).
    """

    def __init__(self, dry_run: bool = False, runner=subprocess.run) -> None:
        self.dry_run = dry_run
        self._state = _NetState(dry_run, runner)
        self.commands: list[list[str]] = self._state.commands

    def _for(self, ifname: str) -> _NetState:
        if self._state.ifname is None:
            self._state.ifname = ifname
        elif ifname != self._state.ifname:
            raise ValueError(f"this NetConfig manages {self._state.ifname}, not {ifname}")
        return self._state

    def configure_interface(self, ifname: str, ip: str) -> None:
        """Bring up ``ifname`` as a point-to-point interface with ``ip``."""
        self._for(ifname).configure_interface(ip)

    def reconfigure_address(self, ifname: str, old_ip: str, new_ip: str) -> None:
        """Change a running point-to-point interface's address (``old_ip``
        must be the configured one; else ValueError).

        Used when the modem reassigns an IP mid-session (reconnect after a
        loss of registration): the old alias is removed first, since macOS
        otherwise stacks addresses on the same interface rather than
        replacing one.
        """
        try:
            self._for(ifname).reconfigure_address(old_ip, new_ip)
        except HelperProtocolError as exc:
            raise ValueError(str(exc)) from exc

    def add_host_route(self, ifname: str, host_ip: str) -> None:
        """Add a host route to ``host_ip`` via ``ifname`` (loopback mode's
        smoke-test route, and ``up --route-host``). At most
        ``MAX_HOST_ROUTES``; adding the same host twice is a no-op. All are
        removed (newest first) by ``remove_host_routes()``/``teardown()``.
        """
        host_ip = validate_route_host(host_ip)
        try:
            self._for(ifname).add_host_route(host_ip)
        except HelperProtocolError as exc:
            raise ValueError(str(exc)) from exc

    def add_default_route(self, ifname: str) -> None:
        """Replace the default route with one through ``ifname``.

        The previous default (by gateway if it had one, else by interface)
        is saved and restored on teardown. Reading the current default route
        is read-only and always attempted (even in dry-run), since it needs
        no root and doesn't mutate anything; only the actual route/ifconfig
        changes are skipped in dry-run mode. If there was no previous
        default route at all, teardown just removes ours.

        Idempotent: if our route is already installed (e.g. ``up
        --supervise`` rebuilding a session after a USB re-enumeration,
        without a full teardown() in between), this is a no-op and the
        "previous default" is never re-captured -- re-reading it at that
        point would just see our own route and "restoring" it on teardown
        would leave the Mac's real default gateway gone for good.
        """
        self._for(ifname).enable_default_route()

    def remove_default_route(self) -> None:
        """Remove our default route and restore the previous one. Best-effort;
        a failed cleanup stays pending for a later retry. Idempotent.
        """
        self._state.disable_default_route()

    def remove_host_routes(self) -> None:
        """Delete every host route we added, newest first. Best-effort; a
        failed delete stays tracked for a later retry.
        """
        self._state.remove_host_routes()

    def set_dns(self, servers: list[str]) -> None:
        """Publish ``servers`` as the DNS resolver via a dynamic-store key.

        Every server is re-validated here (IPv4 only, same rules as the root
        helper) as defence in depth, since the text ends up in an scutil
        script. Raises ValueError.
        """
        validated = []
        for server in servers:
            normalized = valid_unicast_ipv4(server)
            if normalized is None:
                raise ValueError(f"invalid DNS server (need a unicast IPv4 address): {server!r}")
            validated.append(normalized)
        self._state.set_dns(validated)

    def clear_dns(self) -> None:
        """Remove the DNS key we published, if any. Best-effort, idempotent."""
        self._state.clear_dns()

    def teardown(self) -> None:
        """Undo whatever was configured, in reverse order: DNS, default
        route, host routes (newest first), then the interface. Best-effort:
        a failure in one step is logged but never skips the rest.
        """
        self._state.teardown()
