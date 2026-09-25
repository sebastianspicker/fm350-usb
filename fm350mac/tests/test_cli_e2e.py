"""End-to-end cli.py tests with fakes: no USB, no root, no real subprocess,
no SIM. Exercises status/connect/up through the injected factories rather
than argparse dispatch (build_parser() is only used to build realistic
argparse.Namespace objects).
"""

import json

import pytest
from fakes import FailingNetConfig, FakeAtPort, FakeRndisUsb, FakeUtun

from fm350mac.cli import build_parser, cmd_connect, cmd_doctor, cmd_status, cmd_up
from fm350mac.netconfig import NetConfig


def _up_args(extra=()):
    return build_parser().parse_args(["up", "--apn", "internet", *extra])


def _status_args(extra=()):
    return build_parser().parse_args(["status", *extra])


def _incrementing_clock(start: float = 0.0, step: float = 1.0):
    state = {"t": start}

    def _clock():
        state["t"] += step
        return state["t"]

    return _clock


# --- status ------------------------------------------------------------


def test_status_prints_sim_ready(capsys):
    fake = FakeAtPort(sim_ready=True, registered=True)
    rc = cmd_status(_status_args(), at_port_factory=lambda: fake)
    assert rc == 0
    out = capsys.readouterr().out
    assert "SIM: ready" in out
    assert "Registration: LTE yes" in out
    assert fake.closed


def test_status_prints_sim_missing(capsys):
    fake = FakeAtPort(sim_ready=False)
    rc = cmd_status(_status_args(), at_port_factory=lambda: fake)
    assert rc == 0
    out = capsys.readouterr().out
    assert "SIM: not ready" in out


def test_status_raw_prints_the_underlying_at_responses(capsys):
    fake = FakeAtPort(sim_ready=True, registered=True)
    rc = cmd_status(_status_args(["--raw"]), at_port_factory=lambda: fake)
    assert rc == 0
    out = capsys.readouterr().out
    assert "+CPIN: READY" in out
    assert "SIM: ready" in out  # the summary still follows the raw dump


def test_status_json_is_machine_readable(capsys):
    fake = FakeAtPort(sim_ready=True, registered=True)
    rc = cmd_status(_status_args(["--json"]), at_port_factory=lambda: fake)
    assert rc == 0
    report = json.loads(capsys.readouterr().out)
    assert report["sim_ready"] is True
    assert report["lte_registered"] is True
    assert report["serving_cell"]["band"] == 1
    assert report["serving_cell"]["cell_id"] == "0012345AB"


def test_status_redact_masks_cell_id_and_tac(capsys):
    fake = FakeAtPort(sim_ready=True, registered=True)
    rc = cmd_status(_status_args(["--redact"]), at_port_factory=lambda: fake)
    assert rc == 0
    out = capsys.readouterr().out
    assert "0012345AB" not in out
    assert "1A2B" not in out
    assert "cell_id=REDACTED" in out


def test_status_redact_raw_masks_gtccinfo_tac_and_cell_id(capsys):
    fake = FakeAtPort(sim_ready=True, registered=True)
    rc = cmd_status(_status_args(["--raw", "--redact"]), at_port_factory=lambda: fake)
    assert rc == 0
    out = capsys.readouterr().out
    assert "0012345AB" not in out
    assert "1A2B" not in out


def test_status_hints_at_antennas_when_no_cells_measured(capsys):
    fake = FakeAtPort(sim_ready=True, registered=False)  # -> no-signal CESQ/empty GTCCINFO
    rc = cmd_status(_status_args(), at_port_factory=lambda: fake)
    assert rc == 0
    out = capsys.readouterr().out
    assert "check the antenna pigtails" in out


def test_status_watch_refreshes_until_ctrl_c(capsys):
    fake = FakeAtPort(sim_ready=True, registered=True)
    calls = []

    def fake_sleep(seconds):
        calls.append(seconds)
        if len(calls) >= 2:
            raise KeyboardInterrupt

    rc = cmd_status(_status_args(["--watch", "1"]), at_port_factory=lambda: fake, sleep=fake_sleep)
    assert rc == 0
    assert calls == [1.0, 1.0]
    out = capsys.readouterr().out
    assert out.count("SIM: ready") == 2
    assert "RSRP [" in out


# --- doctor --------------------------------------------------------------


def test_doctor_all_ok_on_a_healthy_modem_exits_zero(capsys):
    fake = FakeAtPort(sim_ready=True, registered=True)
    rc = cmd_doctor(None, at_port_factory=lambda: fake)
    assert rc == 0
    out = capsys.readouterr().out
    assert "[OK]" in out
    assert "[WARN]" not in out
    assert "Dell DW5931e" in out


def test_doctor_warns_on_no_cells_and_exits_nonzero(capsys):
    fake = FakeAtPort(sim_ready=True, registered=False)
    rc = cmd_doctor(None, at_port_factory=lambda: fake)
    assert rc == 1
    out = capsys.readouterr().out
    assert "[WARN] no cells measured" in out


def test_doctor_treats_cme_error_as_a_warning_not_data(capsys):
    fake = FakeAtPort(sim_ready=True, registered=True)
    fake.inject_failure("AT+GTDIPCMODE?", "CME_ERROR", "3", persistent=True)
    rc = cmd_doctor(None, at_port_factory=lambda: fake)
    assert rc == 1
    out = capsys.readouterr().out
    assert "[OK] DIPC mode" not in out
    assert "[WARN]" in out


def test_doctor_warns_when_antenna_tuner_disabled():
    fake = FakeAtPort(sim_ready=True, registered=True)
    fake.anttuningen = 0
    rc = cmd_doctor(None, at_port_factory=lambda: fake)
    assert rc == 1


def test_doctor_never_sends_a_write_command():
    """Every command doctor sends must be a query (ends with "?") or appear
    in the explicit allowlist for read-type commands that happen to use "="
    (see cli._DOCTOR_ALLOWED_EQUALS_COMMANDS).
    """
    from fm350mac.cli import _DOCTOR_ALLOWED_EQUALS_COMMANDS, _DOCTOR_COMMANDS

    fake = FakeAtPort(sim_ready=True, registered=True)
    cmd_doctor(None, at_port_factory=lambda: fake)

    for _label, cmd in _DOCTOR_COMMANDS:
        assert "=" not in cmd or cmd in _DOCTOR_ALLOWED_EQUALS_COMMANDS
    for cmd in fake.commands:
        assert "=" not in cmd or cmd in _DOCTOR_ALLOWED_EQUALS_COMMANDS


# --- connect -------------------------------------------------------------


def test_connect_happy_path_prints_ip_and_dns(capsys):
    fake = FakeAtPort(sim_ready=True, registered=True)
    args = build_parser().parse_args(["connect", "--apn", "internet"])
    rc = cmd_connect(args, at_port_factory=lambda: fake)
    assert rc == 0
    out = capsys.readouterr().out
    assert "IP: 10.20.30.40" in out
    assert "DNS: ['8.8.8.8', '8.8.4.4']" in out


def test_connect_with_sim_missing_fails_cleanly_and_sends_no_cgact(capsys):
    fake = FakeAtPort(sim_ready=False)
    args = build_parser().parse_args(["connect", "--apn", "internet"])
    rc = cmd_connect(args, at_port_factory=lambda: fake)
    assert rc == 1
    err = capsys.readouterr().err
    assert "SIM not ready" in err
    assert not any(c.startswith("AT+CGACT") for c in fake.commands)


# --- up --dry-run: full flow -------------------------------------------


def test_up_dry_run_full_flow_records_commands_and_tears_down_in_order():
    at_fake = FakeAtPort(sim_ready=True, registered=True)
    net_holder = {}

    def net_factory(dry_run):
        # add_default_route() always reads the *current* default route (even
        # in dry-run) to know what to restore; a fake runner keeps that from
        # hitting the real machine's routing table.
        net = net_holder["net"] = NetConfig(dry_run=dry_run, runner=_no_op_runner)
        return net

    args = _up_args(["--dry-run", "--default-route", "--dns"])
    rc = cmd_up(
        args,
        at_port_factory=lambda: at_fake,
        find_device_factory=lambda: object(),
        rndis_usb_factory=lambda dev: FakeRndisUsb(),
        net_config_factory=net_factory,
    )
    assert rc == 0

    net = net_holder["net"]
    assert net.commands[0] == ["ifconfig", "utun-dry-run", "inet", "10.20.30.40", "10.20.30.40", "mtu", "1500", "up"]
    assert ["route", "add", "default", "-interface", "utun-dry-run"] in net.commands
    assert any(c[0] == "scutil" and "d.add ServerAddresses * 8.8.8.8 8.8.4.4" in c for c in net.commands)

    dns_remove_idx = next(i for i, c in enumerate(net.commands) if c[0] == "scutil" and "remove" in c[1])
    route_delete_idx = next(i for i, c in enumerate(net.commands) if c == ["route", "delete", "default"])
    assert dns_remove_idx < route_delete_idx

    # AT bring-up (including teardown deactivate) ran even though this is dry-run.
    assert "AT+CGACT=1,1" in at_fake.commands
    assert "AT+CGACT=0,1" in at_fake.commands
    assert at_fake.closed


# --- up: exception injected at each stage leaves everything cleaned up ---


def test_up_cleans_up_after_failure_following_pdp_activate():
    at_fake = FakeAtPort(sim_ready=True, registered=True)

    def boom():
        raise RuntimeError("usb gone")

    args = _up_args(["--no-helper"])
    with pytest.raises(RuntimeError, match="usb gone"):
        cmd_up(args, at_port_factory=lambda: at_fake, find_device_factory=boom, geteuid=lambda: 0)

    assert "AT+CGACT=0,1" in at_fake.commands  # PDP deactivated
    assert at_fake.closed


def test_up_cleans_up_after_failure_following_utun_open():
    at_fake = FakeAtPort(sim_ready=True, registered=True)
    usb_fake = FakeRndisUsb()
    utun_fake = FakeUtun()
    net = FailingNetConfig(dry_run=False, runner=_no_op_runner, fail_at="configure_interface")

    args = _up_args(["--no-helper"])
    with pytest.raises(RuntimeError, match="configure_interface"):
        cmd_up(
            args,
            at_port_factory=lambda: at_fake,
            find_device_factory=lambda: object(),
            rndis_usb_factory=lambda dev: usb_fake,
            utun_factory=lambda: utun_fake,
            net_config_factory=lambda dry_run: net,
            geteuid=lambda: 0,
        )

    assert utun_fake.closed
    assert usb_fake.halted
    assert usb_fake.closed
    assert "AT+CGACT=0,1" in at_fake.commands


def test_up_cleans_up_after_failure_following_default_route():
    at_fake = FakeAtPort(sim_ready=True, registered=True)
    usb_fake = FakeRndisUsb()
    utun_fake = FakeUtun()
    net = FailingNetConfig(dry_run=False, runner=_no_op_runner, fail_at="add_default_route")

    args = _up_args(["--no-helper", "--default-route"])
    with pytest.raises(RuntimeError, match="add_default_route"):
        cmd_up(
            args,
            at_port_factory=lambda: at_fake,
            find_device_factory=lambda: object(),
            rndis_usb_factory=lambda dev: usb_fake,
            utun_factory=lambda: utun_fake,
            net_config_factory=lambda dry_run: net,
            geteuid=lambda: 0,
        )

    assert net._default_route_added is False  # torn down
    assert ["route", "delete", "default"] in net.commands
    assert utun_fake.closed
    assert usb_fake.halted
    assert usb_fake.closed
    assert "AT+CGACT=0,1" in at_fake.commands


def _no_op_runner(argv, **kwargs):
    import subprocess

    return subprocess.CompletedProcess(list(argv), 0, stdout="", stderr="")


# --- up --supervise: device loss -> re-enumeration -> rebuilt session ----


class _SupervisorSequence:
    """Stands in for Supervisor: records what it was built with, and reports
    a scripted outcome on each successive instantiation -- either None (a
    clean stop) or a ``(failure_reason, device_lost)`` pair. ``device_lost``
    is applied to the real Bridge's ``device_lost`` attribute, the same way
    the bridge itself would set it on a real UsbNoDevice/ENODEV failure (see
    bridge.py) -- cli.py now keys off that instead of the failure text.
    """

    outcomes: list[tuple[str, bool] | None] = []
    _n = -1

    def __init__(self, at_port, bridge, net, ifname, cid, initial_ip=None, **kw):
        type(self)._n += 1
        self.bridge = bridge
        self.failure_reason = None
        self._scripted = type(self).outcomes[type(self)._n]

    def stop(self):
        pass

    def run(self):
        if self._scripted is None:
            self.failure_reason = None
            return
        reason, device_lost = self._scripted
        self.failure_reason = reason
        self.bridge.device_lost = device_lost
        self.bridge.failed.set()


def _sequence_class(outcomes):
    return type("_Seq", (_SupervisorSequence,), {"outcomes": outcomes, "_n": -1})


def test_up_supervise_rebuilds_session_after_device_loss_then_stops_cleanly():
    events = []
    at_fake = FakeAtPort(sim_ready=True, registered=True)

    def at_port_factory():
        events.append("at_open")
        return at_fake

    def find_device_factory():
        events.append("find_device")
        return object()

    def rndis_usb_factory(dev):
        events.append("rndis_init")
        return FakeRndisUsb()

    utun_fake = FakeUtun()
    net = NetConfig(dry_run=True)
    sup_cls = _sequence_class([("rx: bulk_read: device disconnected (no such device)", True), None])

    # FakeRndisUsb only implements Bridge's sync interface, not AsyncEndpoint's;
    # --io async's real device wiring is exercised separately in
    # test_async_bridge.py with a fake Libusb.
    args = _up_args(["--no-helper", "--io", "sync"])
    rc = cmd_up(
        args,
        at_port_factory=at_port_factory,
        find_device_factory=find_device_factory,
        rndis_usb_factory=rndis_usb_factory,
        utun_factory=lambda: utun_fake,
        net_config_factory=lambda dry_run: net,
        geteuid=lambda: 0,
        supervisor_factory=sup_cls,
        reenum_sleep=lambda s: None,
        reenum_time_source=_incrementing_clock(),
    )

    assert rc == 0
    assert events.count("at_open") == 2
    assert events.count("rndis_init") == 2
    assert events.index("at_open", events.index("rndis_init") + 1) > events.index("rndis_init")
    # The restart came back with the same IP (FakeAtPort's ip_pool didn't
    # change), so the interface is configured once, not stacked/re-run on
    # the restart (see NetConfig.reconfigure_address / the item-12 fix).
    ifconfig_calls = [c for c in net.commands if c[:3] == ["ifconfig", "utun-fake", "inet"]]
    assert len(ifconfig_calls) == 1


def test_up_supervise_exits_3_when_modem_never_reenumerates():
    at_fake = FakeAtPort(sim_ready=True, registered=True)
    call_n = {"n": 0}

    def find_device_factory():
        call_n["n"] += 1
        if call_n["n"] == 1:
            return object()  # the initial session builds fine
        raise RuntimeError("gone")  # every re-enumeration poll fails

    net = NetConfig(dry_run=True)
    sup_cls = _sequence_class([("tx: bulk_write: device disconnected (no such device)", True)])

    args = _up_args(["--no-helper", "--reenum-timeout", "5", "--io", "sync"])
    rc = cmd_up(
        args,
        at_port_factory=lambda: at_fake,
        find_device_factory=find_device_factory,
        rndis_usb_factory=lambda dev: FakeRndisUsb(),
        utun_factory=lambda: FakeUtun(),
        net_config_factory=lambda dry_run: net,
        geteuid=lambda: 0,
        supervisor_factory=sup_cls,
        reenum_sleep=lambda s: None,
        reenum_time_source=_incrementing_clock(step=1.0),
    )

    assert rc == 3


# --- item 11: pin the modem's identity (IMEI) across re-enumeration --------


def test_up_supervise_exits_4_on_imei_mismatch_after_reenumeration():
    """If AT+CGSN reports a different IMEI after a re-enumeration, that's a
    different physical modem, not "the same one, just back" -- refuse to
    rebuild the session instead of quietly bridging to the wrong device.
    """
    at_fake = FakeAtPort(sim_ready=True, registered=True, imei="111111111111111")

    def find_device_factory():
        return object()

    net = NetConfig(dry_run=True, runner=_no_op_runner)
    sup_cls = _sequence_class([("rx: bulk_read: device disconnected (no such device)", True)])

    call_n = {"n": 0}

    def at_port_factory():
        call_n["n"] += 1
        if call_n["n"] == 2:
            at_fake.imei = "222222222222222"  # a different modem enumerated at the same VID/PID
        return at_fake

    args = _up_args(["--no-helper", "--io", "sync"])
    rc = cmd_up(
        args,
        at_port_factory=at_port_factory,
        find_device_factory=find_device_factory,
        rndis_usb_factory=lambda dev: FakeRndisUsb(),
        utun_factory=lambda: FakeUtun(),
        net_config_factory=lambda dry_run: net,
        geteuid=lambda: 0,
        supervisor_factory=sup_cls,
        reenum_sleep=lambda s: None,
        reenum_time_source=_incrementing_clock(),
    )

    assert rc == 4
    # Refused before touching any network config on the mismatched session.
    assert not any(c[0] == "ifconfig" and "up" in c for c in net.commands[len(net.commands):])


# --- item 12: re-enumeration must never stack/corrupt the default route ----


def test_up_supervise_reenum_with_default_route_reconfigures_and_restores_original_gateway():
    cid = 1  # matches DEFAULT_CID
    at_fake = FakeAtPort(sim_ready=True, registered=True)
    at_fake.ip_pool[cid] = "10.20.30.40"

    call_n = {"n": 0}

    def at_port_factory():
        call_n["n"] += 1
        if call_n["n"] == 2:
            at_fake.ip_pool[cid] = "10.20.30.99"  # the modem got a new IP after re-enumerating
        return at_fake

    route_get_calls: list[list[str]] = []

    def fake_runner(argv, **kwargs):
        import subprocess

        argv = list(argv)
        if argv[:3] == ["route", "-n", "get"]:
            route_get_calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, stdout="   gateway: 192.168.1.1\n interface: en0\n", stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    net = NetConfig(dry_run=False, runner=fake_runner)
    sup_cls = _sequence_class([("rx: bulk_read: device disconnected (no such device)", True), None])

    args = _up_args(["--no-helper", "--io", "sync", "--default-route"])
    rc = cmd_up(
        args,
        at_port_factory=at_port_factory,
        find_device_factory=lambda: object(),
        rndis_usb_factory=lambda dev: FakeRndisUsb(),
        utun_factory=lambda: FakeUtun(),
        net_config_factory=lambda dry_run: net,
        geteuid=lambda: 0,
        supervisor_factory=sup_cls,
        reenum_sleep=lambda s: None,
        reenum_time_source=_incrementing_clock(),
    )

    assert rc == 0
    # The real previous default was captured exactly once -- never
    # re-captured on the restart (it would otherwise see our own route).
    assert len(route_get_calls) == 1

    # The address changed across the restart: reconfigured in place (delete
    # old alias, add new one), not stacked as a second alias alongside the
    # original bring-up's "up" command.
    assert ["ifconfig", "utun-fake", "inet", "10.20.30.40", "10.20.30.40", "mtu", "1500", "up"] in net.commands
    assert ["ifconfig", "utun-fake", "inet", "10.20.30.40", "delete"] in net.commands
    assert ["ifconfig", "utun-fake", "inet", "10.20.30.99", "10.20.30.99", "mtu", "1500", "up"] in net.commands

    # Our own route was only ever added once (add_default_route() is a
    # no-op on the restart, since it's already installed for this interface).
    assert net.commands.count(["route", "add", "default", "-interface", "utun-fake"]) == 1

    # Final teardown restores the ORIGINAL gateway, not our own utun route.
    assert ["route", "add", "default", "192.168.1.1"] in net.commands
