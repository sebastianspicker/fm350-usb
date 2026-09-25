"""Pure AT response parser tests (at.py). No USB, no hardware."""

import pytest

from fm350mac import at
from fm350mac.usb_async import UsbTimeout


def test_parse_cgpaddr_quoted():
    resp = 'AT+CGPADDR=1\r\n+CGPADDR: 1,"10.20.30.40"\r\n\r\nOK\r\n'
    assert at.parse_cgpaddr(resp) == "10.20.30.40"


def test_parse_cgpaddr_unquoted():
    resp = "+CGPADDR: 1,10.20.30.40\r\n\r\nOK\r\n"
    assert at.parse_cgpaddr(resp) == "10.20.30.40"


def test_parse_cgpaddr_missing():
    assert at.parse_cgpaddr("ERROR\r\n") is None


# --- valid_assigned_ipv4()/ip_address(): the modem's response is untrusted
# text that ends up driving ifconfig/route/ARP replies, so anything that
# isn't a plausible unicast IPv4 address must be rejected. ------------------


def test_valid_assigned_ipv4_accepts_a_normal_address():
    assert at.valid_assigned_ipv4("10.20.30.40") == "10.20.30.40"


@pytest.mark.parametrize(
    "bad",
    [
        "999.1.1.1",  # out-of-range octet: parse_cgpaddr's regex lets this through
        "0.0.0.0",  # unspecified
        "224.0.0.1",  # multicast
        "127.0.0.1",  # loopback
        "169.254.1.1",  # link-local
        "not-an-ip",
        None,
    ],
)
def test_valid_assigned_ipv4_rejects_bad_addresses(bad):
    assert at.valid_assigned_ipv4(bad) is None


class _FakeAtPortForIp:
    """A minimal fake AtPort returning a fixed +CGPADDR response, to test
    at.ip_address()'s validation without needing the FakeAtPort in
    tests/fakes.py (which models a lot more than needed here).
    """

    def __init__(self, cgpaddr_response: str) -> None:
        self._response = cgpaddr_response

    def command(self, cmd: str, timeout: float = 240.0) -> str:
        return self._response


def test_ip_address_rejects_a_malicious_out_of_range_octet():
    port = _FakeAtPortForIp('+CGPADDR: 1,"999.1.1.1"\r\n\r\nOK\r\n')
    assert at.ip_address(port, 1) is None


def test_ip_address_rejects_multicast():
    port = _FakeAtPortForIp('+CGPADDR: 1,"224.0.0.1"\r\n\r\nOK\r\n')
    assert at.ip_address(port, 1) is None


def test_ip_address_accepts_a_normal_address():
    port = _FakeAtPortForIp('+CGPADDR: 1,"10.20.30.40"\r\n\r\nOK\r\n')
    assert at.ip_address(port, 1) == "10.20.30.40"


# --- parse_cgsn()/imei() ----------------------------------------------------


def test_parse_cgsn_extracts_the_imei():
    assert at.parse_cgsn("490154203237518\r\n\r\nOK\r\n") == "490154203237518"


def test_parse_cgsn_missing():
    assert at.parse_cgsn("ERROR\r\n") is None


def test_imei_sends_cgsn_and_returns_the_parsed_value():
    port = _FakeAtPortForIp("490154203237518\r\n\r\nOK\r\n")
    assert at.imei(port) == "490154203237518"


def test_parse_gtdns_quoted_both():
    resp = '+GTDNS: 1,"8.8.8.8","8.8.4.4"\r\n\r\nOK\r\n'
    assert at.parse_gtdns(resp) == ["8.8.8.8", "8.8.4.4"]


def test_parse_gtdns_unquoted_with_spaces():
    resp = "+GTDNS: 1, 8.8.8.8 , 8.8.4.4\r\n\r\nOK\r\n"
    assert at.parse_gtdns(resp) == ["8.8.8.8", "8.8.4.4"]


def test_parse_gtdns_single_server():
    resp = '+GTDNS: 1,"8.8.8.8"\r\n\r\nOK\r\n'
    assert at.parse_gtdns(resp) == ["8.8.8.8"]


def test_parse_gtdns_skips_unspecified():
    resp = '+GTDNS: 1,"8.8.8.8","0.0.0.0"\r\n\r\nOK\r\n'
    assert at.parse_gtdns(resp) == ["8.8.8.8"]


def test_parse_gtdns_missing():
    assert at.parse_gtdns("ERROR\r\n") == []


def test_parse_cpin_ready():
    assert at.parse_cpin("+CPIN: READY\r\n\r\nOK\r\n") is True


def test_parse_cpin_not_ready():
    assert at.parse_cpin("+CPIN: SIM PIN\r\n\r\nOK\r\n") is False


def test_parse_registration_cereg():
    assert at.parse_registration("+CEREG: 0,1\r\n\r\nOK\r\n") == 1


def test_parse_registration_c5greg():
    assert at.parse_registration("+C5GREG: 0,5\r\n\r\nOK\r\n") == 5


def test_parse_registration_missing():
    assert at.parse_registration("ERROR\r\n") is None


def test_parse_registration_c5greg_unsolicited_mode_only_returns_none():
    """``+C5GREG: <n>`` with no ``<stat>`` field at all (unsolicited-report-
    only mode, seen live on this modem) must return None, not raise or
    misparse ``<n>`` as if it were ``<stat>``.
    """
    assert at.parse_registration("+C5GREG: 0\r\n\r\nOK\r\n") is None


@pytest.mark.parametrize("stat", [1, 5])
def test_is_registered_true_for_home_and_roaming(stat):
    assert at.is_registered(stat) is True


@pytest.mark.parametrize("stat", [0, 2, 3, 4, None])
def test_is_registered_false_otherwise(stat):
    assert at.is_registered(stat) is False


# --- AtPort.command(): returns as soon as a final result code is seen,
# without needing __init__ (no real USB device). ----------------------------


class _FakeUsbDevice:
    """Minimal stand-in for usb_async.UsbDevice's bulk I/O: queued IN reads, no-op OUT writes."""

    def __init__(self, read_chunks=()):
        self._chunks = list(read_chunks)

    def bulk_out(self, endpoint, data, timeout_ms=200):
        return len(data)

    def bulk_in(self, endpoint, size, timeout_ms=200):
        if self._chunks:
            return self._chunks.pop(0)
        raise UsbTimeout(-7, "LIBUSB_ERROR_TIMEOUT", "no data")


def test_command_returns_as_soon_as_final_result_code_seen():
    port = at.AtPort.__new__(at.AtPort)
    port.usb_device = _FakeUsbDevice([b"AT\r\n", b"\r\nOK\r\n"])
    port.ep_out = 0x06
    port.ep_in = 0x87
    port.ep_in_max_packet = 64
    result = port.command("AT", timeout=5)
    assert result == "AT\r\n\r\nOK\r\n".strip()


# --- Input validation: APN/PDP-type values end up inside a quoted AT
# command string, so anything that could break out or inject a second
# `\r`-terminated command must be rejected before it gets there. ------------


def test_validate_apn_accepts_normal_values():
    assert at.validate_apn("internet") == "internet"
    assert at.validate_apn("iot.1nce.net") == "iot.1nce.net"
    assert at.validate_apn("some_apn-1") == "some_apn-1"


@pytest.mark.parametrize("apn", ['x"\rAT+CFUN=0', "-flag", "", '"', "a" * 101])
def test_validate_apn_rejects_injection_and_malformed_values(apn):
    with pytest.raises(ValueError):
        at.validate_apn(apn)


def test_validate_pdp_type_accepts_known_values():
    for pdp in ("IP", "IPV6", "IPV4V6"):
        assert at.validate_pdp_type(pdp) == pdp


@pytest.mark.parametrize("pdp", ['IP"\rAT+CFUN=0', "-flag", "ip", "BOGUS"])
def test_validate_pdp_type_rejects_injection_and_unknown_values(pdp):
    with pytest.raises(ValueError):
        at.validate_pdp_type(pdp)


def test_define_pdp_rejects_bad_apn_before_sending_anything():
    class _ExplodingPort:
        def command(self, cmd, timeout=240.0):
            raise AssertionError("must not send a command for invalid input")

    with pytest.raises(ValueError):
        at.define_pdp(_ExplodingPort(), 1, "IP", 'x"\rAT+CFUN=0')


def test_define_pdp_rejects_bad_pdp_type_before_sending_anything():
    class _ExplodingPort:
        def command(self, cmd, timeout=240.0):
            raise AssertionError("must not send a command for invalid input")

    with pytest.raises(ValueError):
        at.define_pdp(_ExplodingPort(), 1, "-flag", "internet")
