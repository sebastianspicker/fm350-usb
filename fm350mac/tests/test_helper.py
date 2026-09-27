"""Tests for the root helper (src/fm350mac/helper/fm350mac_helper.py) and its
main-process client (helper_client.py). No real subprocess, no root, no real
utun -- a fake command runner stands in for /sbin/ifconfig etc., and a fake
"utun opener" hands out one end of a local socketpair/pipe instead of a real
kernel utun.

Most tests drive a ``ConnectionHandler`` synchronously, with no threads: the
client's requests are written to one end of a ``socket.socketpair()`` up
front, the write side is shut down (so the server's read loop sees a clean
EOF once it's drained them), then ``handle_one_connection()`` is called
directly and processes everything before returning. Only the "end to end"
test at the bottom needs a real background thread (the server has to be
running concurrently with the real ``HelperClient``/``cli.cmd_up`` blocking
call it drives).
"""

from __future__ import annotations

import json
import json.decoder
import json.scanner
import os
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

from fm350mac import cli
from fm350mac import helper_client as client_mod
from fm350mac.helper import fm350mac_helper as helper
from fm350mac.helper_client import HelperClient

# --- fakes -----------------------------------------------------------------


class _FakeRunner:
    """Fake subprocess.run: records every call, can be made to fail for a
    specific one, and answers `route -n get default` with canned output.
    Mirrors tests/test_netconfig.py's _FakeRunner.
    """

    def __init__(self, route_get_stdout: str = "", fail_on=None):
        self.route_get_stdout = route_get_stdout
        self.original_route = route_get_stdout
        self.fail_on = fail_on or (lambda argv, kwargs: False)
        self.argvs: list[list[str]] = []

    def __call__(self, argv, **kwargs):
        argv = list(argv)
        self.argvs.append(argv)
        if self.fail_on(argv, kwargs):
            raise subprocess.CalledProcessError(1, argv)
        if argv[:3] == [helper.ROUTE, "-n", "get"] and not self.route_get_stdout:
            raise subprocess.CalledProcessError(1, argv)
        stdout = self.route_get_stdout if argv[:3] == [helper.ROUTE, "-n", "get"] else ""
        if argv[:3] == [helper.ROUTE, "delete", "default"]:
            self.route_get_stdout = ""
        elif argv[:3] == [helper.ROUTE, "add", "default"]:
            self.route_get_stdout = (
                f"interface: {argv[-1]}\n" if "-interface" in argv else self.original_route
            )
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")


def _fake_open_utun():
    """Stand-in for helper.open_utun(): a pipe, not a real utun."""
    read_fd, write_fd = os.pipe()
    os.close(write_fd)
    return read_fd, "utun-fake0"


def _fds_from_ancdata(ancdata):
    fds = []
    int_size = struct.calcsize("i")
    for level, cmsg_type, cmsg_data in ancdata:
        if level == socket.SOL_SOCKET and cmsg_type == socket.SCM_RIGHTS:
            n = len(cmsg_data) // int_size
            fds.extend(struct.unpack(f"{n}i", cmsg_data[: n * int_size]))
    return fds


def _drive(requests, *, open_utun_fn=None, runner=None):
    """Feed `requests` (a list of dicts) to a fresh ConnectionHandler (no
    peer-uid check: that's exercised separately via handle_one_connection),
    run it synchronously to completion (EOF once every request is queued),
    and return ``(responses, fd_count, handler)``.
    """
    if open_utun_fn is None:
        open_utun_fn = _fake_open_utun
    if runner is None:
        runner = _FakeRunner()

    server_conn, client_conn = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    for req in requests:
        client_conn.sendall((json.dumps(req) + "\n").encode())
    client_conn.shutdown(socket.SHUT_WR)

    handler = helper.ConnectionHandler(server_conn, open_utun_fn=open_utun_fn, runner=runner)
    handler.handle()
    server_conn.close()

    chunks = []
    fds = []
    while True:
        data, ancdata, _flags, _addr = client_conn.recvmsg(65536, socket.CMSG_LEN(64))
        if not data:
            break
        chunks.append(data)
        fds.extend(_fds_from_ancdata(ancdata))
    client_conn.close()
    for fd in fds:
        os.close(fd)  # only the mechanics are under test; nothing reads/writes through them here
    lines = [line for line in b"".join(chunks).split(b"\n") if line]
    return [json.loads(line) for line in lines], len(fds), handler


def _drive_after_open(requests, **kwargs):
    """Like _drive(), but with an open_utun request prepended; returns the
    responses to *just* the caller's own requests (the open_utun response
    is checked once here and then dropped) plus the handler (``handler.net``
    exposes the full ordered command log, ``handler._runner`` the raw fake
    runner).
    """
    responses, _fds, handler = _drive([{"op": "open_utun"}, *requests], **kwargs)
    assert responses[0]["ok"] is True
    return responses[1:], handler


# --- hello / protocol basics -------------------------------------------


def test_hello_round_trip():
    responses, _fds, _runner = _drive([{"op": "hello", "version": 1}])
    assert responses == [{"ok": True, "version": 1, "pid": os.getpid()}]


def test_hello_rejects_wrong_version():
    responses, _fds, _runner = _drive([{"op": "hello", "version": 2}])
    assert responses[0]["ok"] is False
    assert "protocol version" in responses[0]["error"]


# --- schema validation: unknown ops/fields, missing fields -----------------


def test_unknown_op_rejected():
    responses, _fds, _runner = _drive([{"op": "reboot_the_mac"}])
    assert responses[0]["ok"] is False
    assert "unknown op" in responses[0]["error"]


def test_unknown_field_rejected():
    responses, _fds, _runner = _drive([{"op": "hello", "version": 1, "extra": "nope"}])
    assert responses[0]["ok"] is False
    assert "unknown field" in responses[0]["error"]


def test_missing_field_rejected():
    responses, _fds, _runner = _drive([{"op": "set_address"}])
    assert responses[0]["ok"] is False
    assert "missing field" in responses[0]["error"]


@pytest.mark.parametrize(
    "op,fields",
    [
        ("hello", {"version": 1}),
        ("open_utun", {}),
        ("set_address", {"ip": "10.0.0.2"}),
        ("reconfigure_address", {"old_ip": "10.0.0.2", "new_ip": "10.0.0.3"}),
        ("add_host_route", {"dest": "198.51.100.5"}),
        ("set_default_route", {"enable": True}),
        ("set_dns", {"servers": ["8.8.8.8"]}),
        ("clear_dns", {}),
        ("teardown", {}),
    ],
)
def test_every_op_rejects_an_unknown_field(op, fields):
    req = {"op": op, **fields, "bogus": "field"}
    responses, _fds, _runner = _drive([{"op": "open_utun"}, req])
    assert responses[1]["ok"] is False
    assert "unknown field" in responses[1]["error"]


def test_request_must_be_a_json_object():
    server_conn, client_conn = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    client_conn.sendall(b'["not", "an", "object"]\n')
    client_conn.shutdown(socket.SHUT_WR)
    helper.handle_one_connection(server_conn, os.getuid(), get_peer_uid_fn=lambda _c: os.getuid(), runner=_FakeRunner())
    server_conn.close()
    resp = json.loads(client_conn.recv(4096).strip())
    assert resp["ok"] is False
    assert "JSON object" in resp["error"]
    client_conn.close()


def test_oversize_message_is_rejected_and_ends_the_connection():
    """A message that never reaches a newline within MAX_MESSAGE_BYTES gets
    one error response, then the connection ends (see _LineReader): the
    framing can no longer be trusted, so there's no attempt to resynchronise.
    """
    server_conn, client_conn = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    junk = b"x" * (helper.MAX_MESSAGE_BYTES + 1000)
    client_conn.sendall(b'{"op": "hello", "version": 1, "junk": "' + junk + b'"}\n')
    client_conn.shutdown(socket.SHUT_WR)
    helper.handle_one_connection(server_conn, os.getuid(), get_peer_uid_fn=lambda _c: os.getuid(), runner=_FakeRunner())
    server_conn.close()
    resp = json.loads(client_conn.recv(65536).strip())
    assert resp["ok"] is False
    assert "exceeds" in resp["error"]
    client_conn.close()


def test_recursion_error_while_parsing_is_rejected_cleanly(monkeypatch):
    """A json.loads() call that raises RecursionError must produce a normal
    error response, not an unhandled exception that kills the connection
    handler (or the whole process). json.loads is un-patched again before
    decoding the response (the test itself needs a working json.loads).
    """
    server_conn, client_conn = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    client_conn.sendall(b'{"op": "hello", "version": 1}\n')
    client_conn.shutdown(socket.SHUT_WR)

    def boom(*_a, **_kw):
        raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr(json, "loads", boom)
    helper.handle_one_connection(server_conn, os.getuid(), get_peer_uid_fn=lambda _c: os.getuid(), runner=_FakeRunner())
    monkeypatch.undo()
    server_conn.close()
    resp = json.loads(client_conn.recv(4096).strip())
    assert resp["ok"] is False
    client_conn.close()


def test_value_error_while_parsing_is_rejected_cleanly(monkeypatch):
    """Same as above, for a bare ValueError raised anywhere in the
    parse/validate step (json.JSONDecodeError is already a ValueError
    subclass and covered separately; this is the broader net)."""

    def boom(_req):
        raise ValueError("something else went wrong")

    monkeypatch.setattr(helper, "_validate_request", boom)
    responses, _fds, _handler = _drive([{"op": "hello", "version": 1}])
    assert responses[0]["ok"] is False


def test_deeply_nested_json_under_a_lowered_recursion_limit_is_rejected_cleanly(monkeypatch):
    """A *real* RecursionError from json.loads() itself (not injected):
    forces the pure-Python json scanner (the C-accelerated one doesn't
    respect sys.setrecursionlimit for array/object nesting -- it takes far
    deeper nesting than fits under MAX_MESSAGE_BYTES to fail, if it ever
    does) and deliberately lowers the recursion limit, so a nesting depth
    well under MAX_MESSAGE_BYTES reliably triggers it.
    """
    monkeypatch.setattr(json.decoder.scanner, "make_scanner", json.scanner.py_make_scanner)
    monkeypatch.setattr(json, "_default_decoder", json.decoder.JSONDecoder())

    old_limit = sys.getrecursionlimit()
    sys.setrecursionlimit(100)
    try:
        nested = ("[" * 600 + "]" * 600).encode()
        assert len(nested) < helper.MAX_MESSAGE_BYTES
        server_conn, client_conn = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        client_conn.sendall(nested + b"\n")
        client_conn.shutdown(socket.SHUT_WR)
        helper.handle_one_connection(
            server_conn, os.getuid(), get_peer_uid_fn=lambda _c: os.getuid(), runner=_FakeRunner()
        )
    finally:
        sys.setrecursionlimit(old_limit)
    server_conn.close()
    resp = json.loads(client_conn.recv(65536).strip())
    assert resp["ok"] is False
    client_conn.close()


# --- malicious/invalid IPv4 inputs ------------------------------------------


@pytest.mark.parametrize(
    "bad_ip", ["0.0.0.0", "999.1.1.1", "224.0.0.1", "127.0.0.1", "169.254.1.1", "not-an-ip", "10.0.0.2/24", 12345, None]
)
def test_set_address_rejects_bad_ips(bad_ip):
    responses, _handler = _drive_after_open([{"op": "set_address", "ip": bad_ip}])
    assert responses[0]["ok"] is False
    assert "invalid ip" in responses[0]["error"]


def test_set_address_accepts_a_normal_ip_and_runs_ifconfig():
    responses, handler = _drive_after_open([{"op": "set_address", "ip": "10.20.30.40"}])
    assert responses[0]["ok"] is True
    assert ["/sbin/ifconfig", "utun-fake0", "inet", "10.20.30.40", "10.20.30.40", "mtu", "1500", "up"] in handler._runner.argvs


def test_reconfigure_address_rejects_bad_new_ip():
    responses, _handler = _drive_after_open(
        [{"op": "set_address", "ip": "10.0.0.2"}, {"op": "reconfigure_address", "old_ip": "10.0.0.2", "new_ip": "0.0.0.0"}]
    )
    assert responses[1]["ok"] is False
    assert "invalid new_ip" in responses[1]["error"]


def test_reconfigure_address_requires_matching_old_ip():
    responses, _handler = _drive_after_open(
        [{"op": "set_address", "ip": "10.0.0.2"}, {"op": "reconfigure_address", "old_ip": "10.0.0.9", "new_ip": "10.0.0.3"}]
    )
    assert responses[1]["ok"] is False
    assert "old_ip" in responses[1]["error"]


def test_ops_other_than_hello_and_open_utun_require_a_utun_first():
    responses, _fds, _runner = _drive([{"op": "set_address", "ip": "10.0.0.2"}])
    assert responses[0]["ok"] is False
    assert "open_utun" in responses[0]["error"]


def test_open_utun_rejects_a_second_call_on_the_same_connection():
    responses, _fds, _runner = _drive([{"op": "open_utun"}, {"op": "open_utun"}])
    assert responses[0]["ok"] is True
    assert responses[1]["ok"] is False
    assert "already called" in responses[1]["error"]


# --- add_host_route: restricted to the loopback /24 -------------------------


@pytest.mark.parametrize("bad_dest", ["8.8.8.8", "198.51.99.5", "198.51.101.5", "not-an-ip", "0.0.0.0"])
def test_add_host_route_rejects_anything_outside_the_loopback_net(bad_dest):
    responses, _handler = _drive_after_open([{"op": "add_host_route", "dest": bad_dest}])
    assert responses[0]["ok"] is False
    assert "198.51.100.0/24" in responses[0]["error"] or "invalid dest" in responses[0]["error"]


def test_add_host_route_accepts_a_loopback_net_address():
    responses, handler = _drive_after_open([{"op": "add_host_route", "dest": "198.51.100.5"}])
    assert responses[0]["ok"] is True
    assert ["/sbin/route", "add", "-host", "198.51.100.5", "-interface", "utun-fake0"] in handler._runner.argvs


def test_add_host_route_rejects_a_second_call():
    responses, _handler = _drive_after_open(
        [{"op": "add_host_route", "dest": "198.51.100.5"}, {"op": "add_host_route", "dest": "198.51.100.6"}]
    )
    assert responses[0]["ok"] is True
    assert responses[1]["ok"] is False
    assert "already installed" in responses[1]["error"]


# --- set_dns / clear_dns -----------------------------------------------


@pytest.mark.parametrize("bad_servers", [[], ["8.8.8.8", "8.8.4.4", "1.1.1.1", "1.0.0.1"], ["0.0.0.0"], ["not-an-ip"], "8.8.8.8"])
def test_set_dns_rejects_bad_server_lists(bad_servers):
    responses, _handler = _drive_after_open([{"op": "set_dns", "servers": bad_servers}])
    assert responses[0]["ok"] is False


def test_set_dns_and_clear_dns_run_scutil():
    responses, handler = _drive_after_open(
        [{"op": "set_dns", "servers": ["8.8.8.8", "8.8.4.4"]}, {"op": "clear_dns"}]
    )
    assert responses[0]["ok"] is True
    assert responses[1]["ok"] is True
    commands = handler.net.commands
    assert any(c[0] == "/usr/sbin/scutil" and "d.add ServerAddresses * 8.8.8.8 8.8.4.4" in c for c in commands)
    assert any(c[0] == "/usr/sbin/scutil" and "remove State:/Network/Service/fm350mac/DNS" in c for c in commands)


# --- set_default_route: same capture/restore semantics as NetConfig --------


def test_default_route_idempotent_and_restores_previous_gateway():
    runner = _FakeRunner(route_get_stdout="   gateway: 10.0.0.1\n interface: en0\n")
    responses, handler = _drive_after_open(
        [{"op": "set_default_route", "enable": True}, {"op": "set_default_route", "enable": True}, {"op": "set_default_route", "enable": False}],
        runner=runner,
    )
    assert [r["ok"] for r in responses] == [True, True, True]
    route_get_calls = [a for a in runner.argvs if a[:3] == ["/sbin/route", "-n", "get"]]
    assert len(route_get_calls) == 2  # capture once, then check ownership before disabling
    assert ["/sbin/route", "add", "default", "10.0.0.1"] in handler.net.commands


def test_default_route_never_captures_its_own_interface():
    runner = _FakeRunner(route_get_stdout="   route to: default\n interface: utun-fake0\n")
    responses, handler = _drive_after_open(
        [{"op": "set_default_route", "enable": True}, {"op": "set_default_route", "enable": False}], runner=runner
    )
    assert [r["ok"] for r in responses] == [True, True]
    commands = handler.net.commands
    assert commands.count(["/sbin/route", "add", "default", "-interface", "utun-fake0"]) == 1
    # Nothing ever "restores" a route back to our own utun: the only
    # "route add default" seen is the one for our own interface.
    default_adds = [c for c in commands if c[:3] == ["/sbin/route", "add", "default"]]
    assert default_adds == [["/sbin/route", "add", "default", "-interface", "utun-fake0"]]


def test_disable_default_route_without_enable_is_a_no_op():
    responses, handler = _drive_after_open([{"op": "set_default_route", "enable": False}])
    assert responses[0]["ok"] is True
    assert handler.net.commands == []


def test_failed_default_add_restores_previous_route_and_reports_original_error():
    original = "gateway: 10.0.0.1\ninterface: en0\n"
    runner = _FakeRunner(
        original,
        fail_on=lambda argv, _: argv == [helper.ROUTE, "add", "default", "-interface", "utun-fake0"],
    )
    responses, _handler = _drive_after_open([{"op": "set_default_route", "enable": True}], runner=runner)
    assert responses[0]["ok"] is False
    assert "-interface" in responses[0]["error"]
    assert runner.argvs[-1] == [helper.ROUTE, "add", "default", "10.0.0.1"]
    assert runner.route_get_stdout == original


def test_failed_default_restore_is_retried_on_disconnect():
    restore_attempts = 0

    def fail_on(argv, _):
        nonlocal restore_attempts
        if argv == [helper.ROUTE, "add", "default", "-interface", "utun-fake0"]:
            return True
        if argv == [helper.ROUTE, "add", "default", "10.0.0.1"]:
            restore_attempts += 1
            return restore_attempts == 1
        return False

    runner = _FakeRunner("gateway: 10.0.0.1\ninterface: en0\n", fail_on=fail_on)
    responses, _handler = _drive_after_open([{"op": "set_default_route", "enable": True}], runner=runner)
    assert responses[0]["ok"] is False
    assert "-interface" in responses[0]["error"]
    assert restore_attempts == 2
    assert runner.route_get_stdout == runner.original_route


def test_successful_enable_retry_preserves_pending_original_route():
    own_add_attempts = 0
    restore_attempts = 0

    def fail_on(argv, _):
        nonlocal own_add_attempts, restore_attempts
        if argv == [helper.ROUTE, "add", "default", "-interface", "utun-fake0"]:
            own_add_attempts += 1
            return own_add_attempts == 1
        if argv == [helper.ROUTE, "add", "default", "10.0.0.1"]:
            restore_attempts += 1
            return restore_attempts == 1
        return False

    runner = _FakeRunner("gateway: 10.0.0.1\ninterface: en0\n", fail_on=fail_on)
    net = helper.NetState("utun-fake0", runner=runner)
    with pytest.raises(subprocess.CalledProcessError):
        net.enable_default_route()

    net.enable_default_route()
    net.disable_default_route()

    assert own_add_attempts == 2
    assert restore_attempts == 2
    assert runner.route_get_stdout == runner.original_route
    route_gets = [argv for argv in runner.argvs if argv[:3] == [helper.ROUTE, "-n", "get"]]
    assert len(route_gets) == 2  # initial capture and disable; the enable retry must not recapture


def test_disable_leaves_new_default_route_untouched():
    runner = _FakeRunner("gateway: 10.0.0.1\ninterface: en0\n")
    net = helper.NetState("utun-fake0", runner=runner)
    net.enable_default_route()
    runner.route_get_stdout = "gateway: 192.0.2.1\ninterface: en1\n"
    before = len(runner.argvs)
    net.disable_default_route()
    assert runner.argvs[before:] == [[helper.ROUTE, "-n", "get", "default"]]
    assert runner.route_get_stdout.endswith("interface: en1\n")


def test_disable_restores_previous_route_when_ours_is_already_gone():
    runner = _FakeRunner("gateway: 10.0.0.1\ninterface: en0\n")
    net = helper.NetState("utun-fake0", runner=runner)
    net.enable_default_route()
    runner.fail_on = lambda argv, _: argv == [helper.ROUTE, "-n", "get", "default"]
    net.disable_default_route()
    assert runner.argvs[-1] == [helper.ROUTE, "add", "default", "10.0.0.1"]


# --- journal undo order: dns, default route, host route, then the iface ---


def test_disconnect_undoes_everything_in_reverse_order():
    runner = _FakeRunner(route_get_stdout="   gateway: 10.0.0.1\n interface: en0\n")
    responses, _fds, handler = _drive(
        [
            {"op": "open_utun"},
            {"op": "set_address", "ip": "10.0.0.2"},
            {"op": "add_host_route", "dest": "198.51.100.5"},
            {"op": "set_default_route", "enable": True},
            {"op": "set_dns", "servers": ["8.8.8.8"]},
        ],
        runner=runner,
    )
    assert all(r["ok"] for r in responses)

    commands = handler.net.commands
    dns_remove_idx = next(i for i, c in enumerate(commands) if c[0] == "/usr/sbin/scutil" and "remove State:/Network/Service/fm350mac/DNS" in c)
    route_delete_idxs = [i for i, c in enumerate(commands) if c == ["/sbin/route", "delete", "default"]]
    host_route_delete_idx = next(i for i, c in enumerate(commands) if c == ["/sbin/route", "delete", "-host", "198.51.100.5"])
    ifdown_idx = next(i for i, c in enumerate(commands) if c == ["/sbin/ifconfig", "utun-fake0", "down"])

    assert dns_remove_idx < route_delete_idxs[-1] < host_route_delete_idx < ifdown_idx
    assert ["/sbin/route", "add", "default", "10.0.0.1"] in commands  # the previous gateway was restored


def test_explicit_teardown_then_disconnect_is_idempotent():
    runner = _FakeRunner(route_get_stdout="   gateway: 10.0.0.1\n interface: en0\n")
    responses, _fds, handler = _drive(
        [
            {"op": "open_utun"},
            {"op": "set_address", "ip": "10.0.0.2"},
            {"op": "set_default_route", "enable": True},
            {"op": "teardown"},
        ],
        runner=runner,
    )
    assert all(r["ok"] for r in responses)
    before = len(handler.net.commands)
    # handle_one_connection's own finally-teardown (on disconnect) runs after
    # the explicit "teardown" op above; nothing further should happen.
    assert len(handler.net.commands) == before


# --- peer uid rejection (injected credential lookup) -----------------------


def test_wrong_peer_uid_is_rejected():
    server_conn, client_conn = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    client_conn.sendall(b'{"op": "hello", "version": 1}\n')
    client_conn.shutdown(socket.SHUT_WR)
    helper.handle_one_connection(server_conn, allowed_uid=1000, get_peer_uid_fn=lambda _conn: 4242, runner=_FakeRunner())
    server_conn.close()
    resp = json.loads(client_conn.recv(4096).strip())
    assert resp == {"ok": False, "error": "unauthorized"}
    client_conn.close()


def test_matching_peer_uid_is_accepted():
    server_conn, client_conn = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    client_conn.sendall(b'{"op": "hello", "version": 1}\n')
    client_conn.shutdown(socket.SHUT_WR)
    helper.handle_one_connection(server_conn, allowed_uid=1000, get_peer_uid_fn=lambda _conn: 1000, runner=_FakeRunner())
    server_conn.close()
    resp = json.loads(client_conn.recv(4096).strip())
    assert resp["ok"] is True
    client_conn.close()


# --- item 4: fallback self-bind is opt-in (--standalone) only --------------


def test_main_without_launchd_socket_and_without_standalone_exits_nonzero(monkeypatch):
    monkeypatch.setattr(helper, "get_launchd_sockets", lambda: None)
    rc = helper.main(["--allowed-uid", "501"])
    assert rc == 1


def test_main_with_standalone_binds_its_own_socket(monkeypatch):
    import signal

    monkeypatch.setattr(helper, "get_launchd_sockets", lambda: None)
    served = {}

    def fake_serve_forever(listen_sock, allowed_uid, **kwargs):
        served["allowed_uid"] = allowed_uid
        listen_sock.close()

    monkeypatch.setattr(helper, "serve_forever", fake_serve_forever)
    # A short /tmp-based path: AF_UNIX paths are capped at ~104 bytes, and
    # pytest's own tmp_path fixture lives too deep for that once
    # bind_own_socket's private staging subdirectory is added on top.
    # chown()ing the socket to `allowed_uid` needs to actually succeed
    # without being root, so use our own uid here (not a fixed one).
    allowed_uid = os.getuid()
    with tempfile.TemporaryDirectory(dir="/tmp") as d:
        sock_path = os.path.join(d, "h.sock")
        old_sigterm = signal.getsignal(signal.SIGTERM)
        try:
            rc = helper.main(["--allowed-uid", str(allowed_uid), "--standalone", "--socket-path", sock_path])
        finally:
            signal.signal(signal.SIGTERM, old_sigterm)  # main() installs its own handler; don't leak it
    assert rc == 0
    assert served["allowed_uid"] == allowed_uid


def test_main_refuses_allowed_uid_zero(monkeypatch):
    monkeypatch.setattr(helper, "get_launchd_sockets", lambda: None)
    rc = helper.main(["--allowed-uid", "0", "--standalone"])
    assert rc == 1


def test_bind_own_socket_refuses_if_final_path_exists_and_isnt_a_socket():
    with tempfile.TemporaryDirectory(dir="/tmp") as d:
        path = Path(d) / "h.sock"
        path.write_text("not a socket")
        try:
            helper.bind_own_socket(str(path), os.getuid())
            assert False, "expected HelperStartError"
        except helper.HelperStartError as exc:
            assert "isn't a socket" in str(exc)
        assert path.read_text() == "not a socket"  # untouched


def test_bind_own_socket_replaces_a_stale_socket_atomically():
    with tempfile.TemporaryDirectory(dir="/tmp") as d:
        path = Path(d) / "h.sock"
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale.bind(str(path))
        stale.close()  # the socket file is left behind, unconnectable ("stale")

        sock = helper.bind_own_socket(str(path), os.getuid())
        try:
            st = os.stat(path)
            assert stat.S_ISSOCK(st.st_mode)
            assert stat.S_IMODE(st.st_mode) == 0o600
        finally:
            sock.close()


def test_bind_own_socket_leaves_no_temp_directory_behind():
    with tempfile.TemporaryDirectory(dir="/tmp") as d:
        path = Path(d) / "h.sock"
        sock = helper.bind_own_socket(str(path), os.getuid())
        try:
            assert sorted(p.name for p in Path(d).iterdir()) == ["h.sock"]
        finally:
            sock.close()


# --- fd passing: SCM_RIGHTS over a socketpair, a pipe fd standing in -------


def test_fd_passing_over_socketpair_with_a_pipe_fd():
    server_conn, client_conn = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    read_fd, write_fd = os.pipe()
    handler = helper.ConnectionHandler(server_conn, open_utun_fn=lambda: (read_fd, "utun-fake0"), runner=_FakeRunner())

    handler._op_open_utun()
    server_conn.close()

    data, ancdata, _flags, _addr = client_conn.recvmsg(4096, socket.CMSG_LEN(struct.calcsize("i")))
    resp = json.loads(data.split(b"\n", 1)[0])
    assert resp == {"ok": True, "ifname": "utun-fake0"}
    fds = _fds_from_ancdata(ancdata)
    assert len(fds) == 1
    received_fd = fds[0]

    # The received fd is a real dup of the original pipe's read end: bytes
    # written to the write end show up when reading from it.
    os.write(write_fd, b"hi")
    assert os.read(received_fd, 2) == b"hi"

    os.close(write_fd)
    os.close(received_fd)
    client_conn.close()


def test_helper_client_open_utun_receives_fd_and_wraps_it_in_a_utun():
    """Same mechanics as above, but through the real client-side
    HelperClient.open_utun() (which sends the request itself, then does its
    own recvmsg), with a SOCK_DGRAM socketpair standing in for the utun
    (Utun.read/write are single-datagram-per-call, matching the real utun's
    PF_SYSTEM socket). HelperClient.open_utun() blocks on recvmsg, so the
    server side is driven from a background thread.
    """
    server_conn, client_sock = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    utun_a, utun_b = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
    fake_fd = utun_a.detach()

    handler = helper.ConnectionHandler(server_conn, open_utun_fn=lambda: (fake_fd, "utun-fake0"), runner=_FakeRunner())

    def _serve_one_request():
        line = handler._reader.read_line()
        req = json.loads(line.decode())
        handler._dispatch(helper._validate_request(req), req)

    server_thread = threading.Thread(target=_serve_one_request)
    server_thread.start()

    client = HelperClient(client_sock)
    utun = client.open_utun()
    server_thread.join(timeout=5)
    server_conn.close()
    assert utun.name == "utun-fake0"

    utun_b.send(b"\x00\x00\x00\x02hello")  # AF_INET header + payload
    assert utun.read() == b"hello"

    utun.close()
    utun_b.close()


# --- end to end: HelperClient <-> helper, driving cli.cmd_up(--loopback) ---


def test_end_to_end_up_loopback_through_the_helper():
    """Runs a real helper server (fake runner, fake utun opener) on a
    background thread over a temporary socket path, then drives
    ``cli.cmd_up(["up", "--loopback"])`` for real (no --dry-run, no
    --no-helper) against it, exactly the DI wiring production code uses.
    Shutdown is a real SIGINT, exactly like Ctrl-C, so cmd_up's own signal
    handling is exercised too.
    """
    import signal

    runner = _FakeRunner()
    other_ends = []

    def fake_open_utun():
        a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
        other_ends.append(b)
        return a.detach(), "utun-fake-e2e"

    with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
        sock_path = os.path.join(tmpdir, "h.sock")
        listen_sock = helper.bind_own_socket(sock_path, os.getuid())

        server_thread = threading.Thread(
            target=helper.serve_forever,
            args=(listen_sock, os.getuid()),
            kwargs={"open_utun_fn": fake_open_utun, "runner": runner, "get_peer_uid_fn": lambda _c: os.getuid()},
            daemon=True,
        )
        server_thread.start()
        try:
            args = cli.build_parser().parse_args(["up", "--apn", "internet", "--loopback"])

            def _send_sigint_soon():
                time.sleep(1.0)
                os.kill(os.getpid(), signal.SIGINT)

            signaler = threading.Thread(target=_send_sigint_soon, daemon=True)
            signaler.start()

            rc = cli.cmd_up(
                args,
                helper_probe_factory=lambda: client_mod.probe(path=sock_path),
            )
            signaler.join(timeout=5)
        finally:
            listen_sock.close()
            server_thread.join(timeout=5)

    assert rc == 0
    assert ["/sbin/ifconfig", "utun-fake-e2e", "inet", "192.0.2.2", "192.0.2.2", "mtu", "1500", "up"] in runner.argvs
    assert ["/sbin/route", "add", "-host", "198.51.100.1", "-interface", "utun-fake-e2e"] in runner.argvs
    # Never a default route: --loopback ignores --default-route entirely.
    assert not any(c[:3] == ["/sbin/route", "add", "default"] for c in runner.argvs)
