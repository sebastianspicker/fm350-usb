"""Reconnect supervisor tests, with FakeAtPort and a real Bridge (fake
USB/utun, threads never started -- only Bridge.stats/failed/set_our_ip is
exercised). Time source and sleep are injected so nothing here really waits.
"""

import socket

from fakes import FakeAtPort, FakeRndisUsb, FakeUtun

from fm350mac.bridge import Bridge
from fm350mac.netconfig import NetConfig
from fm350mac.supervisor import State, Supervisor

OUR_MAC = bytes.fromhex("001122334455")
CID = 1


def _make_supervisor(fake_at, ip="10.0.0.5", **kwargs):
    fake_at.active[CID] = True
    fake_at.ip_pool[CID] = ip
    bridge = Bridge(FakeRndisUsb(), FakeUtun(), OUR_MAC, socket.inet_aton(ip), max_transfer_size=0x4000)
    net = NetConfig(dry_run=True)
    clock = {"t": 0.0}

    def time_source():
        return clock["t"]

    def sleep(seconds):
        clock["t"] += seconds

    kwargs.setdefault("sleep", sleep)
    kwargs.setdefault("time_source", time_source)
    kwargs.setdefault("tick", 1.0)
    sup = Supervisor(fake_at, bridge, net, "utun7", CID, initial_ip=ip, **kwargs)
    return sup, bridge, net


def test_steady_state_stays_connected():
    fake_at = FakeAtPort(sim_ready=True, registered=True)
    sup, bridge, net = _make_supervisor(fake_at)
    for _ in range(5):
        sup.step()
    assert sup.state == State.CONNECTED
    assert sup.stats.at_errors == 0
    assert sup.stats.reconnects == 0


def test_registration_loss_then_recovery_reconnects():
    fake_at = FakeAtPort(sim_ready=True, registered=True)
    sup, bridge, net = _make_supervisor(fake_at)

    sup.step()
    assert sup.state == State.CONNECTED

    fake_at.registered = False
    sup.step()
    assert sup.state == State.LOST

    sup.step()  # still unregistered: stays LOST, no reconnect attempted yet
    assert sup.state == State.LOST
    assert sup.stats.reconnects == 0

    fake_at.registered = True
    sup.step()  # registered again: attempts CGACT=0/1 + CGPADDR and succeeds
    assert sup.state == State.CONNECTED
    assert sup.stats.reconnects == 1
    assert "AT+CGACT=0,1" in fake_at.commands
    assert "AT+CGACT=1,1" in fake_at.commands


def test_registration_sa_case_cereg_unregistered_c5greg_registered_is_registered():
    """A 5G SA registration can show up only in C5GREG while CEREG stays
    "not registered" -- both must always be polled and combined with OR.
    """
    fake_at = FakeAtPort(sim_ready=True, registered=False, registered_nr=True)
    sup, bridge, net = _make_supervisor(fake_at)

    sup.step()

    assert sup.state == State.CONNECTED
    assert sup.stats.at_errors == 0
    assert "AT+CEREG?" in fake_at.commands
    assert "AT+C5GREG?" in fake_at.commands


def test_registration_c5greg_single_field_is_not_an_at_error_when_cereg_parses():
    """C5GREG in unsolicited-report-only mode (``+C5GREG: 0``, no ``<stat>``)
    must not count as an at_error as long as CEREG parsed fine.
    """
    fake_at = FakeAtPort(sim_ready=True, registered=True, c5greg_unsupported=True)
    sup, bridge, net = _make_supervisor(fake_at)

    sup.step()

    assert sup.state == State.CONNECTED
    assert sup.stats.at_errors == 0


def test_registration_both_cereg_and_c5greg_unregistered_is_lost():
    fake_at = FakeAtPort(sim_ready=True, registered=False, registered_nr=False)
    sup, bridge, net = _make_supervisor(fake_at)

    sup.step()

    assert sup.state == State.LOST
    assert sup.stats.at_errors == 0


def test_registration_cereg_unparseable_but_c5greg_registered_is_registered():
    fake_at = FakeAtPort(sim_ready=True, registered_nr=True, cereg_error=True)
    sup, bridge, net = _make_supervisor(fake_at)

    sup.step()

    assert sup.state == State.CONNECTED
    assert sup.stats.at_errors == 0


def test_registration_both_cereg_and_c5greg_unparseable_counts_an_at_error():
    fake_at = FakeAtPort(sim_ready=True, cereg_error=True, c5greg_unsupported=True)
    sup, bridge, net = _make_supervisor(fake_at)

    sup.step()

    assert sup.stats.at_errors == 1
    assert sup.state == State.CONNECTED  # unchanged: step() returned early


def test_ip_change_triggers_utun_reconfigure_and_updates_bridge():
    fake_at = FakeAtPort(sim_ready=True, registered=True)
    sup, bridge, net = _make_supervisor(fake_at, ip="10.0.0.5")

    sup.step()
    assert sup.state == State.CONNECTED
    assert sup.stats.ip_changes == 0

    fake_at.ip_pool[CID] = "10.0.0.9"
    sup.step()

    assert sup.stats.ip_changes == 1
    assert sup.current_ip == "10.0.0.9"
    assert net.commands == [
        ["ifconfig", "utun7", "inet", "10.0.0.5", "delete"],
        ["ifconfig", "utun7", "inet", "10.0.0.9", "10.0.0.9", "mtu", "1500", "up"],
    ]
    assert bridge.our_ip == socket.inet_aton("10.0.0.9")


def test_malicious_or_invalid_ip_is_treated_as_lost_not_reconfigured():
    """A modem response like +CGPADDR: 1,"999.1.1.1" (out-of-range octet,
    which parse_cgpaddr's regex alone would accept) or a multicast address
    must never reach NetConfig/the bridge: at.ip_address() rejects it, and
    the supervisor treats that exactly like "no PDP context up".
    """
    fake_at = FakeAtPort(sim_ready=True, registered=True)
    sup, bridge, net = _make_supervisor(fake_at, ip="10.0.0.5")

    sup.step()
    assert sup.state == State.CONNECTED

    fake_at.ip_pool[CID] = "999.1.1.1"
    sup.step()
    assert sup.state == State.LOST
    assert net.commands == []  # never reconfigured with the bad value
    assert bridge.our_ip == socket.inet_aton("10.0.0.5")  # unchanged

    fake_at.ip_pool[CID] = "224.0.0.1"  # multicast
    sup.step()  # still LOST, still registered -> attempts a reconnect
    assert sup.state in (State.LOST, State.RECONNECTING)
    assert net.commands == []


def test_repeated_activation_failures_back_off_with_the_right_delays():
    fake_at = FakeAtPort(sim_ready=True, registered=True)
    sup, _bridge, _net = _make_supervisor(fake_at)
    fake_at.inject_failure("AT+CGACT=1,1", "CME_ERROR", "no service", persistent=True)

    sup.step()
    assert sup.state == State.CONNECTED

    # Force a loss without deregistering, so every subsequent step attempts
    # (and fails) a reconnect.
    fake_at.active[CID] = False
    sup.step()
    assert sup.state == State.LOST

    delays = []
    for _ in range(6):
        sup.step()
        assert sup.state == State.RECONNECTING
        delays.append(sup.current_wait())

    assert delays == [10.0, 20.0, 40.0, 80.0, 160.0, 300.0]
    assert sup.stats.reconnects == 6


def test_bridge_failure_stops_the_supervisor_with_a_clear_reason():
    fake_at = FakeAtPort(sim_ready=True, registered=True)
    sup, bridge, _net = _make_supervisor(fake_at)
    bridge.failure_reason = "rx: bulk_read: device disconnected (no such device)"
    bridge.failed.set()

    sup.run()

    assert sup.state == State.STOPPED
    assert sup.failure_reason == "rx: bulk_read: device disconnected (no such device)"


def test_tx_stall_increase_shortens_the_wait_between_polls():
    fake_at = FakeAtPort(sim_ready=True, registered=True)
    sup, bridge, _net = _make_supervisor(fake_at, poll_interval=10.0, tick=1.0)
    sup.step()
    assert sup.state == State.CONNECTED

    calls = []

    def fake_sleep(seconds):
        calls.append(seconds)
        if len(calls) == 2:
            bridge.stats.tx_stalls += 1

    sup._sleep = fake_sleep
    sup._wait_for_next_step()

    assert len(calls) == 2  # returned early instead of waiting the full 10 ticks


def test_backoff_resets_after_a_stable_connection():
    fake_at = FakeAtPort(sim_ready=True, registered=True)
    sup, _bridge, _net = _make_supervisor(fake_at, stable_reset=600.0)
    fake_at.inject_failure("AT+CGACT=1,1", "CME_ERROR", "no service")

    sup.step()
    fake_at.active[CID] = False
    sup.step()  # LOST
    sup.step()  # RECONNECTING, fails once (backoff -> 10s)
    assert sup._backoff == 10.0

    sup.step()  # RECONNECTING, succeeds (no more injected failures)
    assert sup.state == State.CONNECTED
    assert sup._backoff == 10.0  # not reset immediately: only after a stable period

    connected_since = sup._connected_since
    sup._time = lambda: connected_since + 700.0  # pretend 700s of stable connection passed
    sup.step()
    assert sup._backoff == sup.backoff_initial
