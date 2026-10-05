"""Reconnect supervisor: polls registration and PDP state with read-only AT
commands and drives CGACT/CGPADDR re-activation with exponential backoff
when the link is lost.

Ownership: the supervisor owns ``at_port`` exclusively for as long as it
runs. ``up`` sends the initial CPIN/CGDCONT/CGACT/CGPADDR/GTDNS bring-up
sequence on the same AT port from the main thread *before* constructing and
running the supervisor -- once ``Supervisor.run()`` starts, no other thread
may call anything on that AtPort. The RNDIS bulk data path and the control-
channel keepalive in bridge.py use a separate USB interface, so they're
unaffected either way.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from enum import Enum, auto

from . import at as at_mod
from .bridge import Bridge
from .netconfig import NetConfig

_log = logging.getLogger(__name__)

POLL_INTERVAL_S = 10.0
BACKOFF_INITIAL_S = 5.0
BACKOFF_MAX_S = 300.0
STABLE_RESET_S = 600.0
TICK_S = 1.0
MIN_EARLY_POLL_SPACING_S = 5.0  # min gap between a poll and a tx-stall-triggered early poll
AT_TIMEOUT_S = 10.0  # per-command AT timeout (AtPort's default is 240 s)
# Data-path stall detection while CONNECTED: the modem keeps refusing OUT
# transfers (at least STALL_MIN_TX_TIMEOUTS new tx_timeouts) while rx stays
# flat for this long. Sent-but-unanswered traffic alone (a host that drops
# ICMP, SYN retries, one-way UDP) is not a stall: the modem took it.
STALL_WINDOW_S = 60.0
STALL_MIN_TX_TIMEOUTS = 20


class State(Enum):
    """Supervisor connection state."""

    CONNECTED = auto()
    LOST = auto()
    RECONNECTING = auto()
    STOPPED = auto()


@dataclass
class SupervisorStats:
    """Running counters for the reconnect supervisor."""

    poll_count: int = 0
    at_errors: int = 0
    reconnects: int = 0
    ip_changes: int = 0
    stalls: int = 0  # data-path stalls detected while CONNECTED (see STALL_WINDOW_S)


class Supervisor:
    """Polls the modem's registration/PDP state and reconnects on loss.

    ``sleep``/``time_source`` are injectable so tests run instantly without
    real waiting; production code uses the real ``time.sleep``/``time.monotonic``.
    """

    def __init__(
        self,
        at_port: at_mod.AtPort,
        bridge: Bridge,
        net: NetConfig,
        ifname: str,
        cid: int,
        *,
        initial_ip: str,
        poll_interval: float = POLL_INTERVAL_S,
        backoff_initial: float = BACKOFF_INITIAL_S,
        backoff_max: float = BACKOFF_MAX_S,
        stable_reset: float = STABLE_RESET_S,
        tick: float = TICK_S,
        min_early_poll_spacing: float = MIN_EARLY_POLL_SPACING_S,
        at_timeout: float = AT_TIMEOUT_S,
        stall_window: float = STALL_WINDOW_S,
        stall_min_tx_timeouts: int = STALL_MIN_TX_TIMEOUTS,
        refresh_dns=None,
        time_source=time.monotonic,
        sleep=time.sleep,
    ) -> None:
        self.at_port = at_port
        self.bridge = bridge
        self.net = net
        self.ifname = ifname
        self.cid = cid
        self.poll_interval = poll_interval
        self.backoff_initial = backoff_initial
        self.backoff_max = backoff_max
        self.stable_reset = stable_reset
        self.tick = tick
        self.min_early_poll_spacing = min_early_poll_spacing
        self.at_timeout = at_timeout
        self.stall_window = stall_window
        self.stall_min_tx_timeouts = stall_min_tx_timeouts
        # Optional ``refresh_dns(new_ip)`` callback, called after an IP change
        # so the caller can re-query/re-apply DNS (the supervisor itself only
        # reconfigures the interface address).
        self._refresh_dns = refresh_dns
        self._time = time_source
        self._sleep = sleep

        self.state = State.CONNECTED
        self.stats = SupervisorStats()
        self.current_ip: str | None = initial_ip
        self.failure_reason: str | None = None
        # True if this supervisor itself failed the bridge over a data-path
        # stall (see _check_stall): ``up`` rebuilds without counting that
        # toward its cap on RNDIS-level rebuilds.
        self.stall_failure = False
        self._backoff = backoff_initial
        self._connected_since: float | None = self._time()
        self._last_tx_timeouts = bridge.stats.tx_timeouts
        self._last_poll_time = self._time()
        self._stop = threading.Event()
        # Stall window baseline: (since, rx_packets, tx_timeouts) at its
        # start, or None while not CONNECTED. _stall_cycled: the PDP context
        # was already cycled once for the current stall.
        self._stall_baseline: tuple[float, int, int] | None = None
        self._stall_cycled = False

    def stop(self) -> None:
        """Ask ``run()`` to exit at the next opportunity."""
        self._stop.set()

    def current_wait(self) -> float:
        """The delay ``run()`` will sleep before the next poll/reconnect attempt."""
        return self._backoff if self.state in (State.LOST, State.RECONNECTING) else self.poll_interval

    def run(self) -> None:
        """Poll until ``stop()`` is called or the bridge fails.

        On a bridge failure, ``failure_reason`` is set from
        ``bridge.failure_reason`` before returning -- the caller (``up``)
        decides what a fatal bridge failure means (e.g. USB device gone).
        """
        while not self._stop.is_set():
            if self.bridge.failed.is_set():
                self.failure_reason = self.bridge.failure_reason or "bridge failed"
                _log.error("supervisor stopping: %s", self.failure_reason)
                self.state = State.STOPPED
                return
            self.step()
            self._wait_for_next_step()

    def _wait_for_next_step(self) -> None:
        remaining = self.current_wait()
        while remaining > 0 and not self._stop.is_set():
            if self.bridge.failed.is_set():
                return  # run() reports the failure
            if self.state == State.CONNECTED and self._tx_stall_increased():
                _log.info("tx timeouts increased while connected; polling registration/PDP early")
                return
            tick = min(self.tick, remaining)
            self._sleep(tick)
            remaining -= tick

    def _tx_stall_increased(self) -> bool:
        """True if genuine tx timeouts (not pool-full drops) grew since the
        last early poll, and at least ``min_early_poll_spacing`` has passed
        since the last poll of any kind -- so a stalled link can't turn the
        supervisor into an AT-command hammer.
        """
        current = self.bridge.stats.tx_timeouts
        if current > self._last_tx_timeouts and self._time() - self._last_poll_time >= self.min_early_poll_spacing:
            self._last_tx_timeouts = current
            return True
        return False

    # --- one poll cycle --------------------------------------------------

    def step(self) -> None:
        """Run one poll (and, if needed, one reconnect attempt). Never raises."""
        self.stats.poll_count += 1
        self._last_poll_time = self._time()
        registered = self._poll_registration()
        if registered is None:
            return  # AT error/timeout already counted; try again next tick

        if self.state == State.CONNECTED:
            self._step_connected(registered)
        else:
            self._step_disconnected(registered)
        if self.state != State.CONNECTED:
            self._stall_baseline = None  # an outage is not a stall; start afresh once connected

    def _step_connected(self, registered: bool) -> None:
        if not registered:
            _log.warning("registration lost")
            self.state = State.LOST
            self._connected_since = None
            return
        answered, ip = self._poll_ip()
        if not answered:
            return  # AT timeout already counted; says nothing about the PDP context
        if ip is None:
            _log.warning("PDP context appears down (no valid IP)")
            self.state = State.LOST
            self._connected_since = None
            return
        self._note_ip(ip)
        self._maybe_reset_backoff()
        self._check_stall()

    def _check_stall(self) -> None:
        """Catch a session that reports CONNECTED but no longer passes
        traffic: the modem refuses OUT transfers (``tx_timeouts`` grows by
        at least ``stall_min_tx_timeouts``) while rx stays flat for
        ``stall_window``. The first time, cycle the PDP context; if it's
        still stalled after that, fail the bridge so ``up`` rebuilds the
        whole session. Packets the modem accepted but nobody answered
        (``tx_packets`` growth alone) are not a stall, and neither is an
        idle session. A window that passes without a stall, or rx flowing
        again, ends the current stall.
        """
        stats = self.bridge.stats
        rx = stats.rx_packets
        tx_timeouts = stats.tx_timeouts
        now = self._time()
        if self._stall_baseline is None:
            self._stall_baseline = (now, rx, tx_timeouts)
            return
        since, rx0, tx_timeouts0 = self._stall_baseline
        if rx != rx0:
            self._stall_baseline = (now, rx, tx_timeouts)
            self._stall_cycled = False  # traffic flows again
            return
        if now - since < self.stall_window:
            return
        self._stall_baseline = (now, rx, tx_timeouts)
        refused = tx_timeouts - tx_timeouts0
        if refused < self.stall_min_tx_timeouts:
            self._stall_cycled = False  # idle or merely unanswered: no stall in this window
            return
        self.stats.stalls += 1
        detail = f"{refused} tx timeouts while rx stayed at {rx} for {now - since:.0f}s"
        if self._stall_cycled:
            reason = f"data path stalled ({detail}) even after cycling the PDP context"
            _log.error("%s; failing the bridge to force a rebuild", reason)
            self.stall_failure = True
            self._fail_bridge(reason)
            return
        _log.warning("data path stalled (%s); cycling the PDP context once", detail)
        self._stall_cycled = True
        if not self._deactivate_activate():
            self.state = State.LOST
            self._connected_since = None
            return
        _answered, ip = self._poll_ip()
        if ip is None:
            self.state = State.LOST
            self._connected_since = None
            return
        self._note_ip(ip)

    def _fail_bridge(self, reason: str) -> None:
        # AsyncBridge.fail() is public; the frozen sync Bridge only has
        # _mark_failed(), which does the same.
        fail = getattr(self.bridge, "fail", None) or getattr(self.bridge, "_mark_failed")
        fail(reason)

    def _step_disconnected(self, registered: bool) -> None:
        if not registered:
            self.state = State.LOST
            return
        # Registration is back; the PDP context may have survived (or the
        # modem re-activated it itself). Only cycle it if there's no valid IP
        # -- not merely because CGPADDR timed out.
        answered, ip = self._poll_ip()
        if not answered:
            return
        if ip is not None:
            self._note_ip(ip)
            self.state = State.CONNECTED
            self._connected_since = self._time()
            _log.info("registration restored with PDP still up: ip=%s", ip)
            return
        self.state = State.RECONNECTING
        self._attempt_reconnect()

    def _attempt_reconnect(self) -> None:
        self.stats.reconnects += 1
        if not self._deactivate_activate():
            self._backoff = min(self._backoff * 2, self.backoff_max)
            _log.warning("reconnect attempt failed; next retry in %.0fs", self._backoff)
            return
        _answered, ip = self._poll_ip()
        if ip is None:
            self._backoff = min(self._backoff * 2, self.backoff_max)
            _log.warning("reconnect: no IP after CGACT=1; next retry in %.0fs", self._backoff)
            return
        self._note_ip(ip)
        self.state = State.CONNECTED
        self._connected_since = self._time()
        # Backoff is not reset here on purpose: it only resets after the
        # connection has been stable for ``stable_reset`` seconds (see
        # _maybe_reset_backoff), so a flapping link doesn't get to retry at
        # the fast initial rate indefinitely.
        _log.info("reconnected: ip=%s", ip)

    # --- AT helpers (all defensive: never raise, just count errors) ------

    def _command(self, cmd: str, timeout: float) -> str:
        # stop() aborts an in-flight command (e.g. a 60 s CGACT mid-reconnect)
        # within one AT read instead of after its full timeout.
        return self.at_port.command(cmd, timeout=timeout, stop_event=self._stop)

    def _poll_registration(self) -> bool | None:
        """Poll both CEREG (LTE) and C5GREG (NR) and report whether either
        one says registered.

        Both are always queried -- a 5G SA registration can show up only in
        C5GREG while CEREG stays 0 (not registered), so querying just one
        would miss it. C5GREG's ``<stat>`` is None both when the modem
        doesn't support it and when it's in unsolicited-report-only mode
        (``+C5GREG: <n>`` with no ``<stat>`` field at all, see
        ``at.parse_registration``); either way that's not an error as long
        as CEREG parsed. Only counts as an ``at_errors`` if *neither*
        response parses.
        """
        try:
            cereg_stat = at_mod.parse_registration(self._command("AT+CEREG?", self.at_timeout))
            c5greg_stat = at_mod.parse_registration(self._command("AT+C5GREG?", self.at_timeout))
        except TimeoutError:
            self.stats.at_errors += 1
            _log.warning("registration poll timed out")
            return None
        except Exception:
            self.stats.at_errors += 1
            _log.exception("registration poll failed")
            return None
        if cereg_stat is None and c5greg_stat is None:
            self.stats.at_errors += 1
            return None
        return at_mod.is_registered(cereg_stat) or at_mod.is_registered(c5greg_stat)

    def _poll_ip(self) -> tuple[bool, str | None]:
        """Return ``(answered, ip)``. ``answered`` is False if CGPADDR timed
        out: the modem is busy or wedged, which says nothing about the PDP
        context, so callers must not treat that as "no IP" and cycle it.
        ``ip`` is None if there's no valid assigned IPv4 address.
        """
        try:
            # at_mod.ip_address() takes no timeout, so do what it does here.
            resp = self._command(f"AT+CGPADDR={self.cid}", self.at_timeout)
            return True, at_mod.valid_assigned_ipv4(at_mod.parse_cgpaddr(resp))
        except TimeoutError:
            self.stats.at_errors += 1
            _log.warning("CGPADDR poll timed out")
            return False, None
        except Exception:
            self.stats.at_errors += 1
            _log.exception("CGPADDR poll failed")
            return True, None

    def _deactivate_activate(self) -> bool:
        try:
            self._command(f"AT+CGACT=0,{self.cid}", at_mod.ACTIVATE_TIMEOUT_S)
            resp = self._command(f"AT+CGACT=1,{self.cid}", at_mod.ACTIVATE_TIMEOUT_S)
        except TimeoutError:
            self.stats.at_errors += 1
            _log.warning("CGACT during reconnect timed out")
            return False
        except Exception:
            self.stats.at_errors += 1
            _log.exception("CGACT during reconnect failed")
            return False
        # The final non-empty line must be the result code: a bare "OK"
        # substring would also match e.g. "+CME ERROR: BOOK" or echoed text.
        lines = [line.strip() for line in resp.splitlines() if line.strip()]
        if not lines or lines[-1] != "OK":
            self.stats.at_errors += 1
            return False
        return True

    # --- IP change handling ------------------------------------------------

    def _note_ip(self, ip: str) -> None:
        if self.current_ip is not None and ip != self.current_ip:
            _log.warning("IP changed: %s -> %s", self.current_ip, ip)
            self.stats.ip_changes += 1
            try:
                self.net.reconfigure_address(self.ifname, self.current_ip, ip)
            except Exception:
                _log.exception("failed to reconfigure utun address")
            self.bridge.set_our_ip(ip)
            if self._refresh_dns is not None:
                try:
                    self._refresh_dns(ip)
                except Exception:
                    _log.exception("failed to refresh DNS after IP change")
        self.current_ip = ip

    def _maybe_reset_backoff(self) -> None:
        if self._connected_since is None:
            self._connected_since = self._time()
            return
        if self._backoff != self.backoff_initial and self._time() - self._connected_since >= self.stable_reset:
            _log.info("connection stable for %.0fs; resetting backoff", self.stable_reset)
            self._backoff = self.backoff_initial
