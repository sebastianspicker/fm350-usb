"""Shared fakes for end-to-end tests: a scriptable AT port, and RNDIS
control-aware stand-ins for usb_transport.RndisUsb / utun.Utun.

No hardware, no real subprocess, no real sockets. Response text for
FakeAtPort matches real FM350-GL samples (command echo first, then the
reply, terminated by "\\r\\nOK\\r\\n", or an error marker with no trailing OK)
so the pure parsers in at.py exercise exactly what they'd see live.
"""

from __future__ import annotations

import re
import struct

from fm350mac import rndis
from fm350mac.netconfig import NetConfig
from fm350mac.usb_async import UsbTimeout

# --- AT ----------------------------------------------------------------

# Real +CESQ/+GTCCINFO samples from this modem (TAC/cell ID replaced with
# placeholders, see docs/bench-log.md's cable-swap entry).
_CESQ_LTE_SAMPLE = "+CESQ: 17,99,255,255,4,29,75,52,57\r\n\r\nOK\r\n"
_CESQ_NO_SIGNAL = "+CESQ: 99,99,255,255,255,255,255,255,255\r\n\r\nOK\r\n"
_GTCCINFO_LTE_SAMPLE = (
    "+GTCCINFO: \r\n"
    "1,4,262,2,1A2B,0012345AB,100,42,,,-7,29,29,4\r\n"
    "\r\n"
    "2,4,,,FFFF,00FFFFFFF,9460,71,,43,43,10\r\n"
    "2,4,,,FFFF,00FFFFFFF,6300,207,,42,42,15\r\n"
    "\r\nOK\r\n"
)
_GTCCINFO_EMPTY = "+GTCCINFO: \r\n\r\nOK\r\n"


class FakeAtPort:
    """Scriptable stand-in for at.AtPort: same .command()/.close() interface.

    Models SIM state, registration, one CGDCONT-defined PDP context per cid,
    CGACT activation toggling with an IP assigned only while active, and
    GTDNS. Every command sent is recorded on ``commands`` in order, so tests
    can assert what was (or wasn't) sent.
    """

    def __init__(
        self,
        sim_ready: bool = True,
        registered: bool = True,
        dns_servers: tuple[str, str] = ("8.8.8.8", "8.8.4.4"),
        imei: str = "490154203237518",
        registered_nr: bool | None = None,
        c5greg_unsupported: bool = False,
        cereg_error: bool = False,
    ) -> None:
        self.sim_ready = sim_ready
        self.registered = registered
        self.dns_servers = dns_servers
        self.imei = imei
        # registered_nr=None mirrors `registered` on C5GREG too (the old,
        # single-RAT behaviour most tests want); set it explicitly to model
        # CEREG/C5GREG disagreeing (e.g. a 5G SA registration). See also
        # c5greg_unsupported (unsolicited-mode-only "+C5GREG: 0" response)
        # and cereg_error (CEREG answers ERROR, e.g. a modem that doesn't
        # support it at all).
        self.registered_nr = registered_nr
        self.c5greg_unsupported = c5greg_unsupported
        self.cereg_error = cereg_error
        self.ip_pool: dict[int, str] = {}
        self.pdp_contexts: dict[int, tuple[str, str]] = {}  # cid -> (pdp_type, apn)
        self.active: dict[int, bool] = {}
        self.commands: list[str] = []
        self.closed = False
        self._failures: dict[str, list[tuple[str, str]]] = {}
        self._persistent_failures: dict[str, tuple[str, str]] = {}

        # +CESQ/+GTCCINFO: None means "derive from `registered`" (a
        # plausible real LTE reading vs. no signal at all); set explicitly
        # to model something else (e.g. registered but temporarily no cell
        # measured, which does happen).
        self.cesq_values: tuple[int, ...] | None = None
        self.gtccinfo_lines: list[str] | None = None

        # `doctor`-only fields, matching this real modem's readings
        # (docs/dell-dw5931e-usb.md, docs/bench-log.md); override per-test.
        self.pkgver = "81600.0000.00.29.20.22_5025.0000.040.000.038_C69"
        self.dipcmode = (3, 1, 1, 1, 3, 15)
        self.fcceffstatus = (0, 1)
        self.fmode = (1, 0)
        self.usbmode = 41
        self.erat = (13, 0, 21, 0, 0)
        self.anttuningen = 1
        self.cfun = 1

    def inject_failure(self, prefix: str, kind: str, message: str = "", persistent: bool = False) -> None:
        """Make the next command starting with ``prefix`` fail.

        ``kind`` is one of "ERROR", "CME_ERROR" or "TIMEOUT". One-shot unless
        ``persistent=True``, in which case every matching command fails.
        """
        if persistent:
            self._persistent_failures[prefix] = (kind, message)
        else:
            self._failures.setdefault(prefix, []).append((kind, message))

    def _pop_failure(self, cmd: str) -> tuple[str, str] | None:
        for prefix, queue in self._failures.items():
            if cmd.startswith(prefix) and queue:
                return queue.pop(0)
        for prefix, failure in self._persistent_failures.items():
            if cmd.startswith(prefix):
                return failure
        return None

    def command(self, cmd: str, timeout: float = 240.0) -> str:
        self.commands.append(cmd)
        echo = cmd + "\r\n"
        failure = self._pop_failure(cmd)
        if failure is not None:
            kind, message = failure
            if kind == "TIMEOUT":
                return echo.strip()  # no final result code ever arrived
            if kind == "CME_ERROR":
                return (echo + f"+CME ERROR: {message}\r\n").strip()
            return (echo + "ERROR\r\n").strip()
        return (echo + self._handle(cmd)).strip()

    def _handle(self, cmd: str) -> str:
        if cmd == "AT+CGSN":
            return f"{self.imei}\r\n\r\nOK\r\n"
        if cmd == "AT+CPIN?":
            if not self.sim_ready:
                return "+CME ERROR: SIM not inserted\r\n"
            return "+CPIN: READY\r\n\r\nOK\r\n"
        if cmd == "AT+COPS?":
            if self.registered:
                return '+COPS: 0,0,"FakeNet",7\r\n\r\nOK\r\n'
            return '+COPS:0,255,"",0\r\n\r\nOK\r\n'
        if cmd == "AT+CEREG?":
            if self.cereg_error:
                return "ERROR\r\n"
            return f"+CEREG: 0,{1 if self.registered else 0}\r\n\r\nOK\r\n"
        if cmd == "AT+C5GREG?":
            if self.c5greg_unsupported:
                return "+C5GREG: 0\r\n\r\nOK\r\n"
            nr_registered = self.registered if self.registered_nr is None else self.registered_nr
            return f"+C5GREG: 0,{1 if nr_registered else 0}\r\n\r\nOK\r\n"
        if cmd == "AT+CESQ":
            if self.cesq_values is not None:
                return f"+CESQ: {','.join(str(v) for v in self.cesq_values)}\r\n\r\nOK\r\n"
            return _CESQ_LTE_SAMPLE if self.registered else _CESQ_NO_SIGNAL
        if cmd == "AT+GTCCINFO?":
            if self.gtccinfo_lines is not None:
                body = "\r\n".join(self.gtccinfo_lines)
                return f"+GTCCINFO: \r\n{body}\r\n\r\nOK\r\n" if body else "+GTCCINFO: \r\n\r\nOK\r\n"
            return _GTCCINFO_LTE_SAMPLE if self.registered else _GTCCINFO_EMPTY
        if cmd == "AT+GTSENRDTEMP=0":
            return "+GTSENRDTEMP: 1,27615\r\n\r\nOK\r\n"
        if cmd == "AT+GTPKGVER?":
            return f'+GTPKGVER: "{self.pkgver}"\r\n\r\nOK\r\n'
        if cmd == "AT+GTDIPCMODE?":
            return f"+GTDIPCMODE: {','.join(str(v) for v in self.dipcmode)}\r\n\r\nOK\r\n"
        if cmd == "AT+GTFCCEFFSTATUS?":
            return f"+GTFCCEFFSTATUS: {','.join(str(v) for v in self.fcceffstatus)}\r\n\r\nOK\r\n"
        if cmd == "AT+GTFMODE?":
            return f"+GTFMODE: {','.join(str(v) for v in self.fmode)}\r\n\r\nOK\r\n"
        if cmd == "AT+GTUSBMODE?":
            return f"+GTUSBMODE: {self.usbmode}\r\n\r\nOK\r\n"
        if cmd == "AT+ERAT?":
            return f"+ERAT: {','.join(str(v) for v in self.erat)}\r\n\r\nOK\r\n"
        if cmd == "AT+GTANTTUNINGEN?":
            return f"+GTANTTUNINGEN: {self.anttuningen}\r\n\r\nOK\r\n"
        if cmd == "AT+CFUN?":
            return f"+CFUN: {self.cfun}\r\n\r\nOK\r\n"

        m = re.match(r'AT\+CGDCONT=(\d+),"([^"]*)","([^"]*)"', cmd)
        if m:
            cid, pdp_type, apn = int(m.group(1)), m.group(2), m.group(3)
            self.pdp_contexts[cid] = (pdp_type, apn)
            return "OK\r\n"

        m = re.match(r"AT\+CGACT=([01]),(\d+)", cmd)
        if m:
            state, cid = int(m.group(1)), int(m.group(2))
            if state == 1:
                if not self.sim_ready:
                    return "+CME ERROR: SIM not inserted\r\n"
                if not self.registered:
                    return "+CME ERROR: no network service\r\n"
                self.active[cid] = True
                self.ip_pool.setdefault(cid, "10.20.30.40")
            else:
                self.active[cid] = False
            return "OK\r\n"

        m = re.match(r"AT\+CGPADDR=(\d+)", cmd)
        if m:
            cid = int(m.group(1))
            if self.active.get(cid):
                return f'+CGPADDR: {cid},"{self.ip_pool.get(cid, "10.20.30.40")}"\r\n\r\nOK\r\n'
            return f'+CGPADDR: {cid},"0.0.0.0"\r\n\r\nOK\r\n'

        m = re.match(r"AT\+GTDNS=(\d+)", cmd)
        if m:
            cid = int(m.group(1))
            if self.active.get(cid):
                return f'+GTDNS: {cid},"{self.dns_servers[0]}","{self.dns_servers[1]}"\r\n\r\nOK\r\n'
            return f'+GTDNS: {cid},"0.0.0.0","0.0.0.0"\r\n\r\nOK\r\n'

        return "OK\r\n"

    def close(self) -> None:
        self.closed = True


# --- RNDIS control-message encoding (device/"modem" side; rndis.py only
# encodes the host->device direction, so the completions a fake device sends
# back are built here) --------------------------------------------------


def _control_header(msg_type: int, body: bytes, request_id: int) -> bytes:
    return struct.pack("<III", msg_type, 12 + len(body), request_id) + body


def _pack_init_cmplt(request_id: int, max_transfer_size: int) -> bytes:
    body = struct.pack("<IIIIIIII", 0, 1, 0, 0, 0, 1, max_transfer_size, 0)
    return _control_header(rndis.INIT_CMPLT, body, request_id)


def _pack_query_cmplt(request_id: int, value: bytes) -> bytes:
    buf_offset = 16  # status,buf_len,buf_offset (3*4=12 bytes) => value at byte 8+16=24
    body = struct.pack("<III", 0, len(value), buf_offset) + value
    return _control_header(rndis.QUERY_CMPLT, body, request_id)


def _pack_set_cmplt(request_id: int) -> bytes:
    return _control_header(rndis.SET_CMPLT, struct.pack("<I", 0), request_id)


# --- RNDIS/USB ------------------------------------------------------------


class FakeRndisUsb:
    """Fake standing in for usb_transport.RndisUsb's bulk/control API.

    Answers RNDIS control messages (INIT/QUERY/SET/HALT) like a real device
    would, so RndisDevice.initialize()/mac()/set_packet_filter()/halt() work
    against it without any wall-clock waiting. Bulk IN/OUT is a simple queue,
    as in the original bridge.py tests.
    """

    def __init__(
        self,
        mac: bytes = bytes.fromhex("000011121314"),
        max_transfer_size: int = 0x4000,
        bulk_read_queue=None,
        raise_on_bulk_read=None,
    ):
        self.mac = mac
        self.max_transfer_size = max_transfer_size
        self._bulk_read_queue = list(bulk_read_queue or [])
        self._raise_on_bulk_read = raise_on_bulk_read
        self.bulk_writes: list[bytes] = []
        self.sent: list[bytes] = []
        self._pending_responses: list[bytes] = []
        self.closed = False
        self.halted = False

    def bulk_read(self, size, timeout=1000):
        if self._bulk_read_queue:
            return self._bulk_read_queue.pop(0)
        if self._raise_on_bulk_read is not None:
            raise self._raise_on_bulk_read
        raise UsbTimeout(-7, "LIBUSB_ERROR_TIMEOUT", "no data")

    def bulk_write(self, data, timeout=1000):
        self.bulk_writes.append(bytes(data))
        return len(data)

    def send_encapsulated(self, msg):
        msg = bytes(msg)
        self.sent.append(msg)
        msg_type = rndis.message_type(msg)
        request_id = rndis.peek_request_id(msg)
        if msg_type == rndis.INIT:
            self._pending_responses.append(_pack_init_cmplt(request_id, self.max_transfer_size))
        elif msg_type == rndis.QUERY:
            oid = struct.unpack_from("<I", msg, 12)[0]
            value = self.mac if oid == rndis.OID_802_3_CURRENT_ADDRESS else b"\x00\x00\x00\x00"
            self._pending_responses.append(_pack_query_cmplt(request_id, value))
        elif msg_type == rndis.SET:
            self._pending_responses.append(_pack_set_cmplt(request_id))
        elif msg_type == rndis.HALT:
            self.halted = True
        elif msg_type == rndis.KEEPALIVE:
            self._pending_responses.append(_control_header(rndis.KEEPALIVE_CMPLT, struct.pack("<I", 0), request_id))

    def wait_notify(self, timeout=2000):
        if self._pending_responses:
            return b"\x01" + b"\x00" * 7
        return None  # no control-channel traffic pending

    def get_encapsulated(self, size=4096):
        if self._pending_responses:
            return self._pending_responses.pop(0)
        return b""

    def close(self) -> None:
        self.closed = True


class FakeUtun:
    """Fake standing in for utun.Utun."""

    def __init__(self, read_queue=None, name: str = "utun-fake"):
        self._read_queue = list(read_queue or [])
        self.writes: list[bytes] = []
        self.name = name
        self.closed = False

    def settimeout(self, timeout):
        pass

    def read(self, size=4096):
        if self._read_queue:
            return self._read_queue.pop(0)
        raise TimeoutError("no data")

    def write(self, packet):
        self.writes.append(bytes(packet))
        return len(packet)

    def close(self) -> None:
        self.closed = True


# --- NetConfig with an injectable failure point --------------------------


class FailingNetConfig(NetConfig):
    """NetConfig that raises at a chosen step, to exercise up()'s cleanup
    paths without touching the real network.

    ``fail_at`` is one of "configure_interface", "add_default_route",
    "set_dns". For "add_default_route" the route is actually recorded (so
    teardown has something to restore) before the injected failure fires.
    """

    def __init__(self, *args, fail_at: str | None = None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.fail_at = fail_at

    def configure_interface(self, *args, **kwargs) -> None:
        if self.fail_at == "configure_interface":
            raise RuntimeError("injected failure: configure_interface")
        super().configure_interface(*args, **kwargs)

    def add_default_route(self, *args, **kwargs) -> None:
        super().add_default_route(*args, **kwargs)
        if self.fail_at == "add_default_route":
            raise RuntimeError("injected failure: add_default_route")

    def set_dns(self, *args, **kwargs) -> None:
        if self.fail_at == "set_dns":
            raise RuntimeError("injected failure: set_dns")
        super().set_dns(*args, **kwargs)
