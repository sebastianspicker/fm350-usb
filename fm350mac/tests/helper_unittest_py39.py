"""Stdlib-unittest port of the core fm350mac-helper tests (see test_helper.py
for the full pytest suite), meant to run under the *real* target interpreter:
the system ``/usr/bin/python3`` (3.9.6), isolated (``-I -S``), exactly how
the installed helper runs. No pytest, no third-party imports, no fixtures --
just unittest and the standard library, so it can run standalone.

Run via tests/run_helper_tests_py39.sh, or directly:
    /usr/bin/python3 -I -S tests/helper_unittest_py39.py
"""

import json
import os
import socket
import struct
import subprocess
import sys
import tempfile
import unittest
import unittest.mock

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "src", "fm350mac", "helper"))
import fm350mac_helper as helper  # noqa: E402 (must follow the sys.path fix-up)


class FakeRunner:
    def __init__(self, route_get_stdout="", fail_on=None):
        self.route_get_stdout = route_get_stdout
        self.fail_on = fail_on or (lambda argv, kwargs: False)
        self.argvs = []

    def __call__(self, argv, **kwargs):
        argv = list(argv)
        self.argvs.append(argv)
        if self.fail_on(argv, kwargs):
            raise subprocess.CalledProcessError(1, argv)
        stdout = self.route_get_stdout if argv[:3] == [helper.ROUTE, "-n", "get"] else ""
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")


def fake_open_utun():
    read_fd, write_fd = os.pipe()
    os.close(write_fd)
    return read_fd, "utun-fake0"


def fds_from_ancdata(ancdata):
    fds = []
    int_size = struct.calcsize("i")
    for level, cmsg_type, cmsg_data in ancdata:
        if level == socket.SOL_SOCKET and cmsg_type == socket.SCM_RIGHTS:
            n = len(cmsg_data) // int_size
            fds.extend(struct.unpack("%di" % n, cmsg_data[: n * int_size]))
    return fds


def drive(requests, open_utun_fn=None, runner=None):
    if open_utun_fn is None:
        open_utun_fn = fake_open_utun
    if runner is None:
        runner = FakeRunner()
    server_conn, client_conn = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    for req in requests:
        client_conn.sendall((json.dumps(req) + "\n").encode())
    client_conn.shutdown(socket.SHUT_WR)

    conn_handler = helper.ConnectionHandler(server_conn, open_utun_fn=open_utun_fn, runner=runner)
    conn_handler.handle()
    server_conn.close()

    chunks = []
    fds = []
    while True:
        data, ancdata, _flags, _addr = client_conn.recvmsg(65536, socket.CMSG_LEN(64))
        if not data:
            break
        chunks.append(data)
        fds.extend(fds_from_ancdata(ancdata))
    client_conn.close()
    for fd in fds:
        os.close(fd)
    lines = [line for line in b"".join(chunks).split(b"\n") if line]
    return [json.loads(line) for line in lines], conn_handler


def drive_after_open(requests, **kwargs):
    responses, conn_handler = drive([{"op": "open_utun"}] + list(requests), **kwargs)
    assert responses[0]["ok"] is True
    return responses[1:], conn_handler


class RequestValidationTests(unittest.TestCase):
    def test_hello_round_trip(self):
        responses, _h = drive([{"op": "hello", "version": 1}])
        self.assertEqual(responses, [{"ok": True, "version": 1, "pid": os.getpid()}])

    def test_unknown_op_rejected(self):
        responses, _h = drive([{"op": "nope"}])
        self.assertFalse(responses[0]["ok"])
        self.assertIn("unknown op", responses[0]["error"])

    def test_unknown_field_rejected(self):
        responses, _h = drive([{"op": "hello", "version": 1, "extra": "x"}])
        self.assertFalse(responses[0]["ok"])
        self.assertIn("unknown field", responses[0]["error"])

    def test_missing_field_rejected(self):
        responses, _h = drive([{"op": "set_address"}])
        self.assertFalse(responses[0]["ok"])
        self.assertIn("missing field", responses[0]["error"])

    def test_bad_ips_rejected(self):
        for bad in ("0.0.0.0", "999.1.1.1", "224.0.0.1", "127.0.0.1", "169.254.1.1", "not-an-ip", 12345, None):
            responses, _h = drive_after_open([{"op": "set_address", "ip": bad}])
            self.assertFalse(responses[0]["ok"], bad)
            self.assertIn("invalid ip", responses[0]["error"])

    def test_add_host_route_restricted_to_loopback_net(self):
        for bad in ("8.8.8.8", "198.51.99.5", "198.51.101.5", "not-an-ip"):
            responses, _h = drive_after_open([{"op": "add_host_route", "dest": bad}])
            self.assertFalse(responses[0]["ok"], bad)
        responses, h = drive_after_open([{"op": "add_host_route", "dest": "198.51.100.5"}])
        self.assertTrue(responses[0]["ok"])
        self.assertIn(["/sbin/route", "add", "-host", "198.51.100.5", "-interface", "utun-fake0"], h._runner.argvs)

    def test_oversize_message_rejected(self):
        server_conn, client_conn = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        junk = b"x" * (helper.MAX_MESSAGE_BYTES + 1000)
        client_conn.sendall(b'{"op": "hello", "version": 1, "junk": "' + junk + b'"}\n')
        client_conn.shutdown(socket.SHUT_WR)
        helper.handle_one_connection(server_conn, os.getuid(), get_peer_uid_fn=lambda _c: os.getuid(), runner=FakeRunner())
        server_conn.close()
        resp = json.loads(client_conn.recv(65536).strip())
        self.assertFalse(resp["ok"])
        self.assertIn("exceeds", resp["error"])
        client_conn.close()

    def test_recursion_error_while_parsing_rejected_cleanly(self):
        server_conn, client_conn = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        client_conn.sendall(b'{"op": "hello", "version": 1}\n')
        client_conn.shutdown(socket.SHUT_WR)

        def boom(*_a, **_kw):
            raise RecursionError("maximum recursion depth exceeded")

        with unittest.mock.patch.object(json, "loads", boom):
            helper.handle_one_connection(
                server_conn, os.getuid(), get_peer_uid_fn=lambda _c: os.getuid(), runner=FakeRunner()
            )
        server_conn.close()
        resp = json.loads(client_conn.recv(4096).strip())
        self.assertFalse(resp["ok"])
        client_conn.close()


class JournalAndTeardownTests(unittest.TestCase):
    def test_disconnect_undoes_everything_in_reverse_order(self):
        runner = FakeRunner(route_get_stdout="   gateway: 10.0.0.1\n interface: en0\n")
        responses, h = drive(
            [
                {"op": "open_utun"},
                {"op": "set_address", "ip": "10.0.0.2"},
                {"op": "add_host_route", "dest": "198.51.100.5"},
                {"op": "set_default_route", "enable": True},
                {"op": "set_dns", "servers": ["8.8.8.8"]},
            ],
            runner=runner,
        )
        self.assertTrue(all(r["ok"] for r in responses))
        commands = h.net.commands
        dns_idx = next(i for i, c in enumerate(commands) if c[0] == "/usr/sbin/scutil" and "remove State:/Network/Service/fm350mac/DNS" in c)
        route_delete_idxs = [i for i, c in enumerate(commands) if c == ["/sbin/route", "delete", "default"]]
        host_route_idx = next(i for i, c in enumerate(commands) if c == ["/sbin/route", "delete", "-host", "198.51.100.5"])
        ifdown_idx = next(i for i, c in enumerate(commands) if c == ["/sbin/ifconfig", "utun-fake0", "down"])
        self.assertLess(dns_idx, route_delete_idxs[-1])
        self.assertLess(route_delete_idxs[-1], host_route_idx)
        self.assertLess(host_route_idx, ifdown_idx)
        self.assertIn(["/sbin/route", "add", "default", "10.0.0.1"], commands)

    def test_default_route_idempotent(self):
        runner = FakeRunner(route_get_stdout="   gateway: 10.0.0.1\n interface: en0\n")
        responses, _h = drive_after_open(
            [{"op": "set_default_route", "enable": True}, {"op": "set_default_route", "enable": True}],
            runner=runner,
        )
        self.assertTrue(all(r["ok"] for r in responses))
        route_get_calls = [a for a in runner.argvs if a[:3] == ["/sbin/route", "-n", "get"]]
        self.assertEqual(len(route_get_calls), 1)

    def test_default_route_never_captures_its_own_interface(self):
        runner = FakeRunner(route_get_stdout="   route to: default\n interface: utun-fake0\n")
        responses, h = drive_after_open([{"op": "set_default_route", "enable": True}], runner=runner)
        self.assertTrue(responses[0]["ok"])
        default_adds = [c for c in h.net.commands if c[:3] == ["/sbin/route", "add", "default"]]
        self.assertEqual(default_adds, [["/sbin/route", "add", "default", "-interface", "utun-fake0"]])


class PeerUidTests(unittest.TestCase):
    def test_wrong_uid_rejected(self):
        server_conn, client_conn = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        client_conn.sendall(b'{"op": "hello", "version": 1}\n')
        client_conn.shutdown(socket.SHUT_WR)
        helper.handle_one_connection(server_conn, allowed_uid=1000, get_peer_uid_fn=lambda _c: 4242, runner=FakeRunner())
        server_conn.close()
        resp = json.loads(client_conn.recv(4096).strip())
        self.assertEqual(resp, {"ok": False, "error": "unauthorized"})
        client_conn.close()

    def test_matching_uid_accepted(self):
        server_conn, client_conn = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        client_conn.sendall(b'{"op": "hello", "version": 1}\n')
        client_conn.shutdown(socket.SHUT_WR)
        helper.handle_one_connection(server_conn, allowed_uid=1000, get_peer_uid_fn=lambda _c: 1000, runner=FakeRunner())
        server_conn.close()
        resp = json.loads(client_conn.recv(4096).strip())
        self.assertTrue(resp["ok"])
        client_conn.close()

    def test_real_local_peercred_matches_our_own_uid(self):
        """Exercises the real LOCAL_PEERCRED getsockopt path (not injected)."""
        a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self.assertEqual(helper.get_peer_uid(a), os.getuid())
        finally:
            a.close()
            b.close()


class FdPassingTests(unittest.TestCase):
    def test_fd_passing_over_socketpair_with_a_pipe_fd(self):
        server_conn, client_conn = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        read_fd, write_fd = os.pipe()
        conn_handler = helper.ConnectionHandler(server_conn, open_utun_fn=lambda: (read_fd, "utun-fake0"), runner=FakeRunner())
        conn_handler._op_open_utun()
        server_conn.close()

        data, ancdata, _flags, _addr = client_conn.recvmsg(4096, socket.CMSG_LEN(struct.calcsize("i")))
        resp = json.loads(data.split(b"\n", 1)[0])
        self.assertEqual(resp, {"ok": True, "ifname": "utun-fake0"})
        fds = fds_from_ancdata(ancdata)
        self.assertEqual(len(fds), 1)
        received_fd = fds[0]

        os.write(write_fd, b"hi")
        self.assertEqual(os.read(received_fd, 2), b"hi")

        os.close(write_fd)
        os.close(received_fd)
        client_conn.close()


class StandaloneModeTests(unittest.TestCase):
    def test_main_without_launchd_socket_and_without_standalone_exits_nonzero(self):
        with unittest.mock.patch.object(helper, "get_launchd_sockets", lambda: None):
            rc = helper.main(["--allowed-uid", "501"])
        self.assertEqual(rc, 1)

    def test_bind_own_socket_refuses_if_final_path_exists_and_isnt_a_socket(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as d:
            path = os.path.join(d, "h.sock")
            with open(path, "w") as f:
                f.write("not a socket")
            with self.assertRaises(helper.HelperStartError):
                helper.bind_own_socket(path, os.getuid())

    def test_bind_own_socket_binds_atomically_and_cleans_up(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as d:
            path = os.path.join(d, "h.sock")
            sock = helper.bind_own_socket(path, os.getuid())
            try:
                self.assertEqual(sorted(os.listdir(d)), ["h.sock"])
            finally:
                sock.close()


if __name__ == "__main__":
    unittest.main()
