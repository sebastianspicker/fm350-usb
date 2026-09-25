"""netconfig dry-run tests: command lists, previous-default-route restore
logic, and best-effort/idempotent teardown (netconfig.py).

No real subprocess is ever run here: dry_run=True only records argv lists,
and a fake ``runner`` stands in for subprocess.run wherever NetConfig reads
or (in the "teardown raises" test) actually invokes a command.
"""

import subprocess

from fm350mac.netconfig import NetConfig


class _FakeRunner:
    """A fake subprocess.run replacement: records every call, can be made to
    raise CalledProcessError for a specific one, and answers `route -n get
    default` with canned output.
    """

    def __init__(self, route_get_stdout: str = "", fail_on=None):
        self.route_get_stdout = route_get_stdout
        self.fail_on = fail_on or (lambda argv, kwargs: False)
        self.argvs: list[list[str]] = []

    def __call__(self, argv, **kwargs):
        argv = list(argv)
        self.argvs.append(argv)
        if self.fail_on(argv, kwargs):
            raise subprocess.CalledProcessError(1, argv)
        stdout = self.route_get_stdout if argv[:3] == ["route", "-n", "get"] else ""
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")


def test_configure_interface_dry_run_records_command():
    net = NetConfig(dry_run=True)
    net.configure_interface("utun7", "10.0.0.2", mtu=1500)
    assert net.commands == [["ifconfig", "utun7", "inet", "10.0.0.2", "10.0.0.2", "mtu", "1500", "up"]]


def test_add_default_route_dry_run_restores_previous_gateway_on_teardown():
    runner = _FakeRunner(route_get_stdout="   route to: default\n    gateway: 10.0.0.1\n interface: en0\n")
    net = NetConfig(dry_run=True, runner=runner)
    net.add_default_route("utun7")
    assert net.commands == [
        ["route", "delete", "default"],
        ["route", "add", "default", "-interface", "utun7"],
    ]
    net.teardown()
    assert ["route", "add", "default", "10.0.0.1"] in net.commands


def test_add_default_route_dry_run_restores_previous_interface_only_on_teardown():
    runner = _FakeRunner(route_get_stdout="   route to: default\n interface: en0\n")
    net = NetConfig(dry_run=True, runner=runner)
    net.add_default_route("utun7")
    net.teardown()
    assert ["route", "add", "default", "-interface", "en0"] in net.commands


def test_add_default_route_dry_run_no_previous_default_skips_delete():
    runner = _FakeRunner(route_get_stdout="   route to: default\n")  # no gateway/interface line
    net = NetConfig(dry_run=True, runner=runner)
    net.add_default_route("utun7")
    # No previous default route: setup must not delete a route that doesn't exist.
    assert net.commands == [["route", "add", "default", "-interface", "utun7"]]
    net.teardown()
    # Teardown just removes ours; nothing is restored.
    assert net.commands[-1] == ["route", "delete", "default"]
    route_add_commands = [c for c in net.commands if c[:2] == ["route", "add"]]
    assert route_add_commands == [["route", "add", "default", "-interface", "utun7"]]


def test_set_dns_dry_run_records_scutil_script():
    net = NetConfig(dry_run=True)
    net.set_dns(["8.8.8.8", "8.8.4.4"])
    assert len(net.commands) == 1
    argv = net.commands[0]
    assert argv[0] == "scutil"
    assert "d.add ServerAddresses * 8.8.8.8 8.8.4.4" in argv
    assert "set State:/Network/Service/fm350mac/DNS" in argv


def test_set_dns_no_servers_is_a_no_op():
    net = NetConfig(dry_run=True)
    net.set_dns([])
    assert net.commands == []


def test_teardown_order_and_idempotent():
    runner = _FakeRunner(route_get_stdout="   gateway: 10.0.0.1\n interface: en0\n")
    net = NetConfig(dry_run=True, runner=runner)
    net.configure_interface("utun7", "10.0.0.2")
    net.add_default_route("utun7")
    net.set_dns(["8.8.8.8"])
    net.teardown()

    commands = net.commands
    dns_remove_idx = next(i for i, c in enumerate(commands) if c[0] == "scutil" and "remove" in c[1])
    route_delete_idxs = [i for i, c in enumerate(commands) if c == ["route", "delete", "default"]]
    ifdown_idx = next(i for i, c in enumerate(commands) if c == ["ifconfig", "utun7", "down"])

    # Teardown undoes DNS, then the route (restoring the previous gateway), then brings the interface down.
    assert dns_remove_idx < route_delete_idxs[-1] < ifdown_idx
    assert ["route", "add", "default", "10.0.0.1"] in commands

    # Idempotent: a second teardown does nothing further.
    before = len(net.commands)
    net.teardown()
    assert len(net.commands) == before


def test_reconfigure_address_deletes_old_then_adds_new():
    net = NetConfig(dry_run=True)
    net.reconfigure_address("utun7", "10.0.0.2", "10.0.0.9")
    assert net.commands == [
        ["ifconfig", "utun7", "inet", "10.0.0.2", "delete"],
        ["ifconfig", "utun7", "inet", "10.0.0.9", "10.0.0.9", "mtu", "1500", "up"],
    ]


def test_add_host_route_and_teardown_removes_it_never_touching_default():
    net = NetConfig(dry_run=True)
    net.configure_interface("utun7", "192.0.2.2")
    net.add_host_route("utun7", "198.51.100.1")
    assert ["route", "add", "-host", "198.51.100.1", "-interface", "utun7"] in net.commands
    net.teardown()
    assert ["route", "delete", "-host", "198.51.100.1"] in net.commands
    assert not any(c[:2] == ["route", "add"] and "default" in c for c in net.commands)
    assert not any(c == ["route", "delete", "default"] for c in net.commands)


def test_add_default_route_is_idempotent_for_the_same_interface():
    """Calling add_default_route() again for the same interface (e.g. a
    session rebuilt after a USB re-enumeration, without a teardown() in
    between) must be a no-op: it must not re-read/re-capture "the previous
    default" (which by then would just be our own route).
    """
    runner = _FakeRunner(route_get_stdout="   gateway: 10.0.0.1\n interface: en0\n")
    net = NetConfig(dry_run=True, runner=runner)
    net.add_default_route("utun7")
    route_get_calls_before = sum(1 for a in runner.argvs if a[:3] == ["route", "-n", "get"])
    commands_before = list(net.commands)

    net.add_default_route("utun7")  # idempotent no-op

    assert net.commands == commands_before
    route_get_calls_after = sum(1 for a in runner.argvs if a[:3] == ["route", "-n", "get"])
    assert route_get_calls_after == route_get_calls_before  # never re-captured

    net.teardown()
    assert ["route", "add", "default", "10.0.0.1"] in net.commands  # the ORIGINAL gateway, restored once


def test_add_default_route_never_captures_our_own_interface_as_the_previous_default():
    """If "the current default" already points at the interface we're about
    to install a route for (our own route survived without a teardown()),
    there is no real previous default to restore.
    """
    runner = _FakeRunner(route_get_stdout="   route to: default\n interface: utun7\n")
    net = NetConfig(dry_run=True, runner=runner)
    net.add_default_route("utun7")

    assert net.commands == [["route", "add", "default", "-interface", "utun7"]]  # no delete: nothing to replace
    net.teardown()
    # Nothing "restores" a route back to our own utun: teardown only removes ours.
    assert net.commands[-1] == ["route", "delete", "default"]
    assert net.commands.count(["route", "add", "default", "-interface", "utun7"]) == 1


def test_add_default_route_repoints_to_a_new_interface_without_recapturing():
    runner = _FakeRunner(route_get_stdout="   gateway: 10.0.0.1\n interface: en0\n")
    net = NetConfig(dry_run=True, runner=runner)
    net.add_default_route("utun7")
    net.add_default_route("utun8")  # e.g. a rebuilt utun after a restart

    assert net.commands == [
        ["route", "delete", "default"],
        ["route", "add", "default", "-interface", "utun7"],
        ["route", "delete", "default"],
        ["route", "add", "default", "-interface", "utun8"],
    ]
    net.teardown()
    assert ["route", "add", "default", "10.0.0.1"] in net.commands  # still the ORIGINAL gateway


def test_teardown_with_nothing_configured_is_a_no_op():
    net = NetConfig(dry_run=True)
    net.teardown()
    assert net.commands == []


def test_teardown_continues_after_a_step_raises_and_stays_idempotent():
    """A failing scutil "remove" during teardown must not stop the rest of
    teardown from running, and must not make teardown() raise or retry.
    """

    def fail_on(argv, kwargs):
        return argv == ["scutil"] and "remove" in kwargs.get("input", "")

    runner = _FakeRunner(route_get_stdout="   gateway: 10.0.0.1\n interface: en0\n", fail_on=fail_on)
    net = NetConfig(dry_run=False, runner=runner)
    net.configure_interface("utun7", "10.0.0.2")
    net.add_default_route("utun7")
    net.set_dns(["8.8.8.8"])

    net.teardown()  # must not raise, despite the scutil "remove" call failing

    assert ["route", "delete", "default"] in runner.argvs
    assert ["route", "add", "default", "10.0.0.1"] in runner.argvs
    assert ["ifconfig", "utun7", "down"] in runner.argvs

    before = len(runner.argvs)
    net.teardown()  # idempotent: nothing new is attempted
    assert len(runner.argvs) == before
