"""CLI argument validation tests (cli.py). No USB/network access, no root:
these only exercise argparse parsing, never args.func(args).
"""

import pytest

from fm350mac.cli import build_parser


def test_connect_accepts_a_normal_apn():
    args = build_parser().parse_args(["connect", "--apn", "internet"])
    assert args.apn == "internet"
    assert args.pdp == "IP"


@pytest.mark.parametrize("bad_apn", ['x"\rAT+CFUN=0', "-flag"])
def test_connect_rejects_injection_like_apn_at_parse_time(bad_apn):
    with pytest.raises(SystemExit):
        build_parser().parse_args(["connect", "--apn", bad_apn])


@pytest.mark.parametrize("bad_pdp", ['IP"\rAT+CFUN=0', "-flag", "BOGUS"])
def test_connect_rejects_bad_pdp_type_at_parse_time(bad_pdp):
    with pytest.raises(SystemExit):
        build_parser().parse_args(["connect", "--apn", "internet", "--pdp", bad_pdp])


def test_up_accepts_a_normal_apn():
    args = build_parser().parse_args(["up", "--apn", "internet", "--dry-run"])
    assert args.apn == "internet"
    assert args.dry_run is True


@pytest.mark.parametrize("bad_apn", ['x"\rAT+CFUN=0', "-flag"])
def test_up_rejects_injection_like_apn_at_parse_time(bad_apn):
    with pytest.raises(SystemExit):
        build_parser().parse_args(["up", "--apn", bad_apn])
