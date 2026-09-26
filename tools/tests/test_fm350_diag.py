"""Tests for tools/fm350_diag.py. No hardware, no real adb/USB -- every test
uses a fake transport/Adb (see conftest.py for the sys.path setup that makes
`import fm350_diag` work).
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import fm350_diag
import pytest

# --- Fakes ---------------------------------------------------------------

_CESQ_LTE = "+CESQ: 17,99,255,255,4,55,75,52,57\r\n\r\nOK\r\n"
_CESQ_NO_SIGNAL = "+CESQ: 99,99,255,255,255,255,255,255,255\r\n\r\nOK\r\n"
# Serving cell rsrp_raw=55 -> -141+55 = -86 dBm (a healthy reading; the
# weak-signal WARN path is exercised separately in test_check_registration_*).
_GTCCINFO_LTE = (
    "+GTCCINFO: \r\n"
    "1,4,262,2,1A2B,0012345AB,100,42,,,-7,29,55,4\r\n"
    "\r\n"
    "2,4,,,FFFF,00FFFFFFF,9460,71,,43,43,10\r\n"
    "\r\nOK\r\n"
)
_GTCCINFO_EMPTY = "+GTCCINFO: \r\n\r\nOK\r\n"


def _healthy_responses() -> dict[str, str]:
    """One canned response per level-0 snapshot command, plus the 5 sampling
    commands -- a modem that's registered, unlocked, with a healthy SIM.
    """
    return {
        "AT": "OK\r\n",
        "ATI": "Fibocom FM350-GL\r\n\r\nOK\r\n",
        "AT+CGMR": "29.20.22\r\n\r\nOK\r\n",
        "AT+GTPKGVER?": '+GTPKGVER: "81600.0000.00.29.20.22_5025.0000.040.000.038_C69"\r\n\r\nOK\r\n',
        "AT+CGSN": "490154203237518\r\n\r\nOK\r\n",
        "AT+GTUSBMODE?": "+GTUSBMODE: 41\r\n\r\nOK\r\n",
        "AT+GTDIPCMODE?": "+GTDIPCMODE: 3,1,1,1,3,15\r\n\r\nOK\r\n",
        "AT+GTCURCAR?": "+GTCURCAR: 0\r\n\r\nOK\r\n",
        "AT+GTLOCKCAR?": "+GTLOCKCAR: 0\r\n\r\nOK\r\n",
        "AT+GTFCCEFFSTATUS?": "+GTFCCEFFSTATUS: 0,1\r\n\r\nOK\r\n",
        "AT+GTFCCLOCKMODE?": "+GTFCCLOCKMODE: 0\r\n\r\nOK\r\n",
        "AT+GTFMODE?": "+GTFMODE: 1,0\r\n\r\nOK\r\n",
        "AT+CFUN?": "+CFUN: 1\r\n\r\nOK\r\n",
        "AT+GTANTTUNINGEN?": "+GTANTTUNINGEN: 1\r\n\r\nOK\r\n",
        "AT+BODYSAREN?": "+BODYSAREN: 0\r\n\r\nOK\r\n",
        "AT+GTRXPATHEN?": "+GTRXPATHEN: 1\r\n\r\nOK\r\n",
        "AT+ECAL?": "+ECAL: 1\r\n\r\nOK\r\n",
        "AT+CPIN?": "+CPIN: READY\r\n\r\nOK\r\n",
        "AT+SIMTYPE?": "+SIMTYPE: 0\r\n\r\nOK\r\n",
        "AT+GTDUALSIM?": "+GTDUALSIM: 0\r\n\r\nOK\r\n",
        "AT+MSMPD?": "+MSMPD: 1\r\n\r\nOK\r\n",
        "AT+CIMI": "262021234567890\r\n\r\nOK\r\n",
        "AT+ICCID": "+ICCID: 8949010012345678901\r\n\r\nOK\r\n",
        "AT+GTACT?": "+GTACT: 1,2,3\r\n\r\nOK\r\n",
        "AT+ERAT?": "+ERAT: 13,0,21,0,0\r\n\r\nOK\r\n",
        "AT+E5GOPT?": "+E5GOPT: 1\r\n\r\nOK\r\n",
        "AT+COPS?": '+COPS: 0,0,"FakeNet",7\r\n\r\nOK\r\n',
        "AT+CEREG?": "+CEREG: 0,1\r\n\r\nOK\r\n",
        "AT+C5GREG?": "+C5GREG: 0,1\r\n\r\nOK\r\n",
        "AT+CESQ": _CESQ_LTE,
        "AT+GTCCINFO?": _GTCCINFO_LTE,
        "AT+CEER": "+CEER: normal\r\n\r\nOK\r\n",
        "AT+CGDCONT?": '+CGDCONT: 1,"IP","internet","0.0.0.0",0,0\r\n\r\nOK\r\n',
        "AT+GTSENRDTEMP=0": "+GTSENRDTEMP: 1,27615\r\n\r\nOK\r\n",
    }


class FakeTransport:
    """Scriptable stand-in for Transport: a dict of canned responses, keyed
    by exact command string, with optional per-command failures. Records
    every command sent, in order.
    """

    def __init__(self, responses: dict[str, str] | None = None, fail_on: dict[str, Exception] | None = None):
        self.responses = dict(responses or {})
        self.fail_on = dict(fail_on or {})
        self.commands: list[str] = []
        self.closed = False
        self.reset_called = False

    def command(self, cmd: str, timeout: float = 240.0) -> str:
        self.commands.append(cmd)
        if cmd in self.fail_on:
            raise self.fail_on[cmd]
        return self.responses.get(cmd, "OK\r\n")

    def close(self) -> None:
        self.closed = True

    def usb_reset(self) -> bool:
        self.reset_called = True
        return True


def _session(transport, level=0, out_dir=None, redact=True):
    return fm350_diag.Session(transport, level, out_dir, redact=redact)


def _read_args(duration=0.0, interval=0.0, adb=False):
    return argparse.Namespace(duration=duration, interval=interval, adb=adb)


# --- read: only level-0 commands, report files, no journal ----------------


def test_read_sends_only_level0_commands_and_writes_report(tmp_path):
    transport = FakeTransport(_healthy_responses())
    session = _session(transport, level=0, out_dir=tmp_path)
    rc = fm350_diag.cmd_read(
        _read_args(),
        session_factory=lambda: session,
        usb_present_fn=lambda: True,
        sleep=lambda s: None,
        monotonic=lambda: 0.0,
        out_dir=tmp_path,
    )
    assert rc == 0
    assert transport.commands  # something was sent
    for cmd in transport.commands:
        assert fm350_diag.command_level(cmd) == 0, f"{cmd!r} is not level 0"
    assert (tmp_path / "report.md").is_file()
    assert (tmp_path / "report.json").is_file()
    assert (tmp_path / "transcript.txt").is_file()
    assert not (tmp_path / "journal.jsonl").exists()


def test_read_snapshot_matches_level0_at_commands_exactly():
    level0_from_table = {cmd for cmd, level in fm350_diag.AT_COMMANDS.items() if level == 0}
    level0_from_snapshot = {cmd for _label, cmd in fm350_diag._LEVEL0_SNAPSHOT_COMMANDS}
    assert level0_from_snapshot == level0_from_table


def test_read_reports_usb_not_found(tmp_path):
    rc = fm350_diag.cmd_read(
        _read_args(), session_factory=lambda: pytest.fail("should not open a session"), usb_present_fn=lambda: False,
        out_dir=tmp_path,
    )
    assert rc == 2


# --- guard: unknown/over-level commands refused, nothing sent -------------


@pytest.mark.parametrize("cmd", ["AT+CFUN=15", "AT+CMEE=2", "AT+GTDIPCMODE=3", "AT+NOPE"])
def test_level0_session_refuses_everything_above_level(tmp_path, cmd):
    transport = FakeTransport(_healthy_responses())
    session = _session(transport, level=0, out_dir=tmp_path)
    with pytest.raises(fm350_diag.SafetyError):
        session.send(cmd)
    assert transport.commands == []


@pytest.mark.parametrize(
    "cmd",
    [
        "AT+GTFMODE=0,0;AT+CFUN=15",
        "AT+GTFMODE=a,b",
        "AT+ERAT=abc",
        "AT+GTANTTUNINGEN=2",
        "AT+GTFCCLOCKVER=-1",
    ],
)
def test_command_level_rejects_injection_and_non_integers(cmd):
    assert fm350_diag.command_level(cmd) is None


def test_command_level_accepts_valid_regex_args():
    assert fm350_diag.command_level("AT+GTFMODE=0,0") == 2
    assert fm350_diag.command_level("AT+CEREG=3") == 1
    assert fm350_diag.command_level("AT+ERAT=21") == 2


def test_adb_run_refuses_unlisted_and_wrong_level():
    adb = fm350_diag.Adb(binary="/bin/echo")
    with pytest.raises(fm350_diag.SafetyError):
        adb.run("rm -rf /", level=0)
    with pytest.raises(fm350_diag.SafetyError):
        adb.run("cat /etc/vendor_info", level=3)  # level-0 command, wrong level


# --- redaction --------------------------------------------------------------


def test_redact_bare_imei_imsi():
    assert fm350_diag.redact_text("490154203237518") == "<redacted>"
    assert fm350_diag.redact_text("262021234567890") == "<redacted>"


def test_redact_iccid():
    text = "+ICCID: 8949010012345678901\r\n\r\nOK\r\n"
    assert "8949010012345678901" not in fm350_diag.redact_text(text)


def test_redact_gtccinfo_tac_and_cell_id():
    redacted = fm350_diag.redact_text(_GTCCINFO_LTE)
    assert "1A2B" not in redacted
    assert "0012345AB" not in redacted
    assert "<redacted>" in redacted
    assert "100,42" in redacted  # earfcn/pci untouched


def test_redact_cereg_tac_ci():
    text = '+CEREG: 3,5,"1A2B","0012345AB",7\r\n\r\nOK\r\n'
    redacted = fm350_diag.redact_text(text)
    assert '"1A2B"' not in redacted
    assert '"0012345AB"' not in redacted
    assert redacted.count("<redacted>") == 2


def test_no_redact_flag_keeps_imei_in_transcript(tmp_path):
    transport = FakeTransport({"AT+CGSN": "490154203237518\r\n\r\nOK\r\n"})
    session = _session(transport, level=0, out_dir=tmp_path, redact=False)
    session.send("AT+CGSN")
    assert "490154203237518" in session.transcript[-1][1]


def test_redact_flag_masks_imei_in_transcript(tmp_path):
    transport = FakeTransport({"AT+CGSN": "490154203237518\r\n\r\nOK\r\n"})
    session = _session(transport, level=0, out_dir=tmp_path, redact=True)
    session.send("AT+CGSN")
    assert "490154203237518" not in session.transcript[-1][1]


# --- diagnosis rules (pure functions) --------------------------------------


def test_check_cells_all_samples_no_cells_is_pigtail_fail():
    samples = [
        fm350_diag.Sample(cereg_stat=2, c5greg_stat=None, cesq=None, cells=[], no_cells=True) for _ in range(3)
    ]
    check = fm350_diag.check_cells(samples)
    assert check.level == "FAIL"
    assert "pigtail" in check.message.lower() or "antenna" in check.message.lower()


def test_check_fcc_lock_locked_suggests_experiment_for_dell():
    check = fm350_diag.check_fcc_lock("+GTFCCEFFSTATUS: 0,0\r\n\r\nOK\r\n", oem_image_is_dell=True)
    assert check.level == "FAIL"
    assert "fcc-unlock-dell" in check.message


def test_check_fcc_lock_locked_suggests_script_for_non_dell():
    check = fm350_diag.check_fcc_lock("+GTFCCEFFSTATUS: 0,0\r\n\r\nOK\r\n", oem_image_is_dell=False)
    assert check.level == "FAIL"
    assert "fm350_fcc_unlock.sh" in check.message


def test_check_sim_not_inserted_is_fail():
    checks = fm350_diag.check_sim("+CME ERROR: SIM not inserted\r\n")
    assert checks[0].level == "FAIL"


def test_check_sim_ready_is_ok():
    checks = fm350_diag.check_sim("+CPIN: READY\r\n\r\nOK\r\n")
    assert checks[0].level == "OK"


def test_check_anttuner_disabled_is_warn():
    check = fm350_diag.check_anttuner("+GTANTTUNINGEN: 0\r\n\r\nOK\r\n")
    assert check.level == "WARN"


def test_check_registration_ok_but_weak_rsrp_warns():
    from fm350mac import cellinfo

    weak_cell = cellinfo.LteCell(
        is_serving=True, mcc=262, mnc=2, tac="AAAA", cell_id="BBBBBBBBB", earfcn=100, band=1, pci=42,
        bandwidth=None, rssnr_db=4.0, rxlev_raw=29, rsrp_dbm=-115.0, rsrq_db=-14.5,
    )
    sample = fm350_diag.Sample(cereg_stat=1, c5greg_stat=None, cesq=None, cells=[weak_cell], no_cells=False)
    check = fm350_diag.check_registration([sample])
    assert check.level == "WARN"
    assert "weak signal" in check.message


def test_check_registration_strong_signal_is_ok():
    from fm350mac import cellinfo

    strong_cell = cellinfo.LteCell(
        is_serving=True, mcc=262, mnc=2, tac="AAAA", cell_id="BBBBBBBBB", earfcn=100, band=1, pci=42,
        bandwidth=None, rssnr_db=4.0, rxlev_raw=29, rsrp_dbm=-80.0, rsrq_db=-14.5,
    )
    sample = fm350_diag.Sample(cereg_stat=1, c5greg_stat=None, cesq=None, cells=[strong_cell], no_cells=False)
    check = fm350_diag.check_registration([sample])
    assert check.level == "OK"


# --- experiment template: restore-on-failure, restore.txt ordering, journal


def _fake_clock():
    state = {"t": 0.0}

    def clock():
        state["t"] += 1.0
        return state["t"]

    return clock


def test_restore_txt_written_before_apply(tmp_path):
    transport = FakeTransport({"AT+GTFMODE?": "+GTFMODE: 1,0\r\n\r\nOK\r\n"})
    session = _session(transport, level=2, out_dir=tmp_path, redact=True)
    session._clock = _fake_clock()
    seen = {}

    def apply_cmds(orig):
        seen["restore_txt_exists"] = (tmp_path / "restore.txt").exists()
        return ["AT+GTFMODE=0,0"]

    fm350_diag.run_experiment(
        session,
        tmp_path,
        name="fmode",
        read_cmd="AT+GTFMODE?",
        parse_orig=lambda r: (1, 0),
        apply_cmds=apply_cmds,
        restore_cmds=lambda o: [f"AT+GTFMODE={o[0]},{o[1]}"],
        reenumerate_after_apply=False,
        reenumerate_after_restore=False,
        measure=lambda: [],
        reenumerate=lambda: None,
        verify_cmd="AT+GTFMODE?",
        parse_verify=lambda r: (1, 0),
    )
    assert seen["restore_txt_exists"] is True


def test_run_experiment_restores_when_apply_raises(tmp_path):
    transport = FakeTransport(
        {"AT+GTFMODE?": "+GTFMODE: 1,0\r\n\r\nOK\r\n"},
        fail_on={"AT+GTFMODE=0,0": RuntimeError("boom")},
    )
    session = _session(transport, level=2, out_dir=tmp_path)
    with pytest.raises(RuntimeError):
        fm350_diag.run_experiment(
            session,
            tmp_path,
            name="fmode",
            read_cmd="AT+GTFMODE?",
            parse_orig=lambda r: (1, 0),
            apply_cmds=lambda o: ["AT+GTFMODE=0,0"],
            restore_cmds=lambda o: [f"AT+GTFMODE={o[0]},{o[1]}"],
            reenumerate_after_apply=False,
            reenumerate_after_restore=False,
            measure=lambda: [],
            reenumerate=lambda: None,
            verify_cmd="AT+GTFMODE?",
            parse_verify=lambda r: (1, 0),
        )
    assert "AT+GTFMODE=1,0" in transport.commands  # restore ran despite the apply failure


def test_run_experiment_restores_on_keyboard_interrupt_during_measure(tmp_path):
    transport = FakeTransport({"AT+GTANTTUNINGEN?": "+GTANTTUNINGEN: 1\r\n\r\nOK\r\n"})
    session = _session(transport, level=2, out_dir=tmp_path)
    calls = {"n": 0}

    def measure():
        calls["n"] += 1
        if calls["n"] == 2:  # the "after apply" measurement
            raise KeyboardInterrupt
        return []

    with pytest.raises(KeyboardInterrupt):
        fm350_diag.run_experiment(
            session,
            tmp_path,
            name="anttuner",
            read_cmd="AT+GTANTTUNINGEN?",
            parse_orig=lambda r: 1,
            apply_cmds=lambda o: ["AT+GTANTTUNINGEN=0"],
            restore_cmds=lambda o: [f"AT+GTANTTUNINGEN={o}"],
            reenumerate_after_apply=False,
            reenumerate_after_restore=False,
            measure=measure,
            reenumerate=lambda: None,
            verify_cmd="AT+GTANTTUNINGEN?",
            parse_verify=lambda r: 1,
        )
    assert "AT+GTANTTUNINGEN=0" in transport.commands
    assert "AT+GTANTTUNINGEN=1" in transport.commands  # restore ran


def test_run_experiment_journal_has_intent_and_result(tmp_path):
    transport = FakeTransport({"AT+GTANTTUNINGEN?": "+GTANTTUNINGEN: 1\r\n\r\nOK\r\n"})
    session = _session(transport, level=2, out_dir=tmp_path)
    fm350_diag.run_experiment(
        session,
        tmp_path,
        name="anttuner",
        read_cmd="AT+GTANTTUNINGEN?",
        parse_orig=lambda r: 1,
        apply_cmds=lambda o: ["AT+GTANTTUNINGEN=0"],
        restore_cmds=lambda o: [f"AT+GTANTTUNINGEN={o}"],
        reenumerate_after_apply=False,
        reenumerate_after_restore=False,
        measure=lambda: [],
        reenumerate=lambda: None,
        verify_cmd="AT+GTANTTUNINGEN?",
        parse_verify=lambda r: 1,
    )
    records = [json.loads(line) for line in (tmp_path / "journal.jsonl").read_text().splitlines()]
    apply_records = [r for r in records if r["cmd"] == "AT+GTANTTUNINGEN=0"]
    assert {r["phase"] for r in apply_records} == {"intent", "result"}


class _StuckAntTunerTransport:
    """AT+GTANTTUNINGEN=1 (the restore write) is silently dropped, so the
    experiment template's post-restore verify mismatches.
    """

    def __init__(self):
        self.value = 1
        self.commands: list[str] = []
        self.closed = False

    def command(self, cmd: str, timeout: float = 240.0) -> str:
        self.commands.append(cmd)
        if cmd == "AT+GTANTTUNINGEN?":
            return f"+GTANTTUNINGEN: {self.value}\r\n\r\nOK\r\n"
        if cmd == "AT+GTANTTUNINGEN=0":
            self.value = 0
            return "OK\r\n"
        if cmd == "AT+GTANTTUNINGEN=1":
            return "OK\r\n"  # dropped: self.value stays 0
        return "OK\r\n"

    def close(self) -> None:
        self.closed = True

    def usb_reset(self) -> bool:
        return True


def test_experiment_cli_exits_3_and_prints_restore_failed_on_mismatch(tmp_path, capsys):
    transport = _StuckAntTunerTransport()
    session = _session(transport, level=2, out_dir=tmp_path)
    args = argparse.Namespace(name="anttuner", measure=0.0, accept_risk=True, force_oem=False, even_if_unlocked=False)
    rc = fm350_diag.cmd_experiment(
        args,
        session_factory=lambda: session,
        transport_factory=lambda: transport,
        sleep=lambda s: None,
        monotonic=lambda: 0.0,
        out_dir=tmp_path,
        confirm=lambda prompt: "anttuner",
    )
    assert rc == 3
    assert "RESTORE FAILED" in capsys.readouterr().out


def test_experiment_requires_typed_name_unless_accept_risk(tmp_path):
    transport = FakeTransport({"AT+GTANTTUNINGEN?": "+GTANTTUNINGEN: 1\r\n\r\nOK\r\n"})
    session = _session(transport, level=2, out_dir=tmp_path)
    args = argparse.Namespace(name="anttuner", measure=0.0, accept_risk=False, force_oem=False, even_if_unlocked=False)
    rc = fm350_diag.cmd_experiment(
        args, session_factory=lambda: session, transport_factory=lambda: transport,
        sleep=lambda s: None, monotonic=lambda: 0.0, out_dir=tmp_path, confirm=lambda prompt: "nope",
    )
    assert rc == 1
    assert transport.commands == []  # aborted before sending anything


# --- fcc-unlock-dell ---------------------------------------------------------


def test_fcc_challenge_zero_gives_known_vector():
    assert fm350_diag.fcc_unlock_response(0) == 2065811812


def test_parse_fcc_challenge_hex():
    assert fm350_diag.parse_fcc_challenge("+GTFCCLOCKGEN: 0x00000000\r\n\r\nOK\r\n") == 0


def test_parse_fcc_challenge_decimal():
    assert fm350_diag.parse_fcc_challenge("+GTFCCLOCKGEN: 42\r\n\r\nOK\r\n") == 42


def test_fcc_unlock_refuses_non_dell_without_force(tmp_path):
    transport = FakeTransport({"AT+GTPKGVER?": '+GTPKGVER: "generic_1234.0000_C1"\r\n\r\nOK\r\n'})
    session = _session(transport, level=2, out_dir=tmp_path)
    checks = fm350_diag._experiment_fcc_unlock_dell(session, tmp_path, force_oem=False, even_if_unlocked=False)
    assert any(c.level == "FAIL" for c in checks)
    assert "AT+GTFCCLOCKGEN" not in transport.commands


def test_fcc_unlock_skips_when_already_unlocked(tmp_path):
    transport = FakeTransport(
        {
            "AT+GTPKGVER?": '+GTPKGVER: "81600.0000.00.29.20.22_5025.0000.040.000.038_C69"\r\n\r\nOK\r\n',
            "AT+GTFCCEFFSTATUS?": "+GTFCCEFFSTATUS: 0,1\r\n\r\nOK\r\n",
        }
    )
    session = _session(transport, level=2, out_dir=tmp_path)
    checks = fm350_diag._experiment_fcc_unlock_dell(session, tmp_path, force_oem=False, even_if_unlocked=False)
    assert any(c.level == "OK" and "already unlocked" in c.message for c in checks)
    assert "AT+GTFCCLOCKGEN" not in transport.commands


# --- dipc: parser/validator --------------------------------------------------


def test_dual_content_is_125_bytes_and_stock_dual():
    assert len(fm350_diag.DUAL_CONTENT.encode()) == 125
    assert fm350_diag.parse_dipc_config(fm350_diag.DUAL_CONTENT) == {
        "dual_ipc_mode": 3,
        "ap_logging_interface": 1,
        "md_logging_interface": 1,
        "md_at_interface": 1,
        "ap_pcie_port_config": 3,
        "md_pcie_port_config": 15,
    }


def test_parse_dipc_config_rejects_mode2_via_validator():
    text = "dual_ipc_mode:2\nap_logging_interface:2\nmd_logging_interface:2\nmd_at_interface:2\nap_pcie_port_config:5\nmd_pcie_port_config:13\n"
    config = fm350_diag.parse_dipc_config(text)
    assert config is not None
    assert fm350_diag.dipc_mode_ok(config) is False


def test_parse_dipc_config_rejects_extra_keys():
    assert fm350_diag.parse_dipc_config(fm350_diag.DUAL_CONTENT + "extra:1\n") is None


def test_parse_dipc_config_rejects_non_int():
    text = "dual_ipc_mode:x\nap_logging_interface:1\nmd_logging_interface:1\nmd_at_interface:1\nap_pcie_port_config:3\nmd_pcie_port_config:15\n"
    assert fm350_diag.parse_dipc_config(text) is None


# --- dipc set-dual: preconditions, restore-on-mismatch, never overwrite .orig


class FakeAdb:
    """Models the on-module md_cmn directory and executes the allowlisted
    shell strings cmd_dipc_set_dual/_revert send -- including the guard
    tests, so a missing source must never truncate dipc_config. Every
    command is also checked against the real Adb allowlist.
    """

    _DIR = "/mnt/vendor/nvdata/md_cmn"

    def __init__(self, dipc_config: str, ls_files: list[str] | None = None, corrupt_write: bool = False):
        self.files: dict[str, str] = {"dipc_config": dipc_config}
        for name in ls_files or []:
            self.files.setdefault(name, dipc_config)
        self.corrupt_write = corrupt_write
        self.commands: list[tuple[str, int]] = []

    # Convenience views used by the tests.
    @property
    def dipc_config(self) -> str:
        return self.files["dipc_config"]

    @property
    def dipc_config_orig(self) -> str | None:
        return self.files.get("dipc_config.orig")

    @dipc_config_orig.setter
    def dipc_config_orig(self, value: str) -> None:
        self.files["dipc_config.orig"] = value

    def available(self) -> bool:
        return True

    def devices(self) -> str:
        return "X\tdevice\n"

    def run(self, cmd: str, level: int = 0, binary_output: bool = False, timeout: float = 15.0):
        real = fm350_diag
        allowed = (
            cmd in real._ADB_LEVEL0_EXACT
            if level == 0
            else level == 3 and cmd in real._ADB_LEVEL3_EXACT
        )
        assert allowed, f"not allowlisted at level {level}: {cmd!r}"
        self.commands.append((cmd, level))
        if cmd.startswith(f"cat {self._DIR}/"):
            return self.files.get(cmd.rsplit("/", 1)[1], "")
        if cmd == f"ls -l {self._DIR}/":
            return "\n".join(f"-rw-r--r-- 1 root root 125 Jan  1 00:00 {n}" for n in sorted(self.files))
        if cmd == real._DIPC_CP_ORIG:
            if "dipc_config.orig" not in self.files:
                self.files["dipc_config.orig"] = self.files["dipc_config"]
            return ""
        if cmd == real._DIPC_INSTALL_NEW:
            if self.files.get("dipc_config.new"):
                self.files["dipc_config"] = "CORRUPT" if self.corrupt_write else self.files["dipc_config.new"]
            return ""
        if cmd == real._DIPC_RM_NEW:
            self.files.pop("dipc_config.new", None)
            return ""
        for name in real._DIPC_ORIG_NAMES:
            if cmd == real._dipc_restore_cmd(name):
                if self.files.get(name):
                    self.files["dipc_config"] = self.files[name]
                return ""
        raise AssertionError(f"unexpected adb command: {cmd!r}")

    def push(self, local: Path, remote: str) -> None:
        assert remote == f"{self._DIR}/dipc_config.new"
        self.files["dipc_config.new"] = Path(local).read_text()


_HP_DIPC = "dual_ipc_mode:1\nap_logging_interface:2\nmd_logging_interface:2\nmd_at_interface:2\nap_pcie_port_config:5\nmd_pcie_port_config:13\n"
_DELL_DIPC = "dual_ipc_mode:1\nap_logging_interface:2\nmd_logging_interface:2\nmd_at_interface:2\nap_pcie_port_config:7\nmd_pcie_port_config:13\n"


def _make_backup_dir(tmp_path: Path, corrupt_sha: bool = False) -> Path:
    backup = tmp_path / "backup"
    backup.mkdir()
    (backup / "nvdata.tar").write_bytes(b"fake-tar-data")
    digest = hashlib.sha256(b"fake-tar-data").hexdigest() if not corrupt_sha else "0" * 64
    (backup / "SHA256SUMS").write_text(f"{digest}  nvdata.tar\n")
    return backup


class _FakeStdin:
    def __init__(self, is_tty: bool):
        self._is_tty = is_tty

    def isatty(self) -> bool:
        return self._is_tty


def test_set_dual_refuses_without_backup(tmp_path):
    args = argparse.Namespace(backup=None, dry_run=False)
    rc = fm350_diag.cmd_dipc_set_dual(args, adb_factory=None, out_dir=tmp_path)
    assert rc == 2


def test_set_dual_refuses_bad_sha256sums(tmp_path):
    backup = _make_backup_dir(tmp_path, corrupt_sha=True)
    args = argparse.Namespace(backup=str(backup), dry_run=False)
    rc = fm350_diag.cmd_dipc_set_dual(args, adb_factory=None, out_dir=tmp_path)
    assert rc == 2


def test_set_dual_refuses_non_tty_stdin(tmp_path):
    backup = _make_backup_dir(tmp_path)
    adb = FakeAdb(dipc_config=_DELL_DIPC)
    args = argparse.Namespace(backup=str(backup), dry_run=False)
    rc = fm350_diag.cmd_dipc_set_dual(
        args, adb_factory=lambda: adb, out_dir=tmp_path, stdin=_FakeStdin(is_tty=False)
    )
    assert rc == 2
    assert all(level != 3 for _cmd, level in adb.commands)


def test_set_dual_refuses_wrong_phrase(tmp_path):
    backup = _make_backup_dir(tmp_path)
    adb = FakeAdb(dipc_config=_DELL_DIPC)
    args = argparse.Namespace(backup=str(backup), dry_run=False)
    rc = fm350_diag.cmd_dipc_set_dual(
        args, adb_factory=lambda: adb, out_dir=tmp_path, stdin=_FakeStdin(is_tty=True), confirm_input="nope"
    )
    assert rc == 1
    assert all(level != 3 for _cmd, level in adb.commands)


def test_set_dual_dry_run_sends_no_writes(tmp_path):
    backup = _make_backup_dir(tmp_path)
    adb = FakeAdb(dipc_config=_DELL_DIPC)
    args = argparse.Namespace(backup=str(backup), dry_run=True)
    rc = fm350_diag.cmd_dipc_set_dual(args, adb_factory=lambda: adb, out_dir=tmp_path)
    assert rc == 0
    assert all(level != 3 for _cmd, level in adb.commands)


def test_set_dual_never_overwrites_existing_orig(tmp_path):
    backup = _make_backup_dir(tmp_path)
    adb = FakeAdb(dipc_config=_DELL_DIPC, ls_files=["dipc_config.orig"])
    adb.dipc_config_orig = _HP_DIPC
    args = argparse.Namespace(backup=str(backup), dry_run=False)
    rc = fm350_diag.cmd_dipc_set_dual(
        args, adb_factory=lambda: adb, out_dir=tmp_path, stdin=_FakeStdin(is_tty=True), confirm_input="CHANGE DIPC"
    )
    assert rc == 0
    assert fm350_diag._DIPC_CP_ORIG not in [c for c, _ in adb.commands]
    assert adb.dipc_config_orig == _HP_DIPC


def test_set_dual_refuses_invalid_existing_orig(tmp_path):
    backup = _make_backup_dir(tmp_path)
    adb = FakeAdb(dipc_config=_DELL_DIPC, ls_files=["dipc_config.orig"])
    adb.dipc_config_orig = "dual_ipc_mode:2\n"
    args = argparse.Namespace(backup=str(backup), dry_run=False)
    rc = fm350_diag.cmd_dipc_set_dual(
        args, adb_factory=lambda: adb, out_dir=tmp_path, stdin=_FakeStdin(is_tty=True), confirm_input="CHANGE DIPC"
    )
    assert rc == 2
    assert adb.dipc_config == _DELL_DIPC
    assert all(level != 3 for _cmd, level in adb.commands)


def test_set_dual_with_only_orig_dell_restores_from_orig_dell(tmp_path):
    # Regression: "dipc_config.orig" is a substring of "dipc_config.orig-dell";
    # picking the nonexistent .orig would have made the restore truncate dipc_config.
    backup = _make_backup_dir(tmp_path)
    adb = FakeAdb(dipc_config=_DELL_DIPC, ls_files=["dipc_config.orig-dell"], corrupt_write=True)
    args = argparse.Namespace(backup=str(backup), dry_run=False)
    rc = fm350_diag.cmd_dipc_set_dual(
        args, adb_factory=lambda: adb, out_dir=tmp_path, stdin=_FakeStdin(is_tty=True), confirm_input="CHANGE DIPC"
    )
    assert rc == 3
    assert adb.dipc_config == _DELL_DIPC
    assert fm350_diag._dipc_restore_cmd("dipc_config.orig-dell") in [c for c, _ in adb.commands]


def test_dipc_write_commands_are_guarded_against_missing_source():
    # `cat missing > dipc_config` truncates before cat fails; every write must short-circuit first.
    for cmd in fm350_diag._ADB_LEVEL3_EXACT:
        if "> dipc_config" in cmd:
            assert "[ -s " in cmd.split("> dipc_config")[0], cmd
    assert "[ ! -e dipc_config.orig ]" in fm350_diag._DIPC_CP_ORIG


def test_revert_restores_orig(tmp_path):
    adb = FakeAdb(dipc_config=fm350_diag.DUAL_CONTENT, ls_files=["dipc_config.orig-dell"])
    adb.files["dipc_config.orig-dell"] = _DELL_DIPC
    args = argparse.Namespace(dry_run=False)
    rc = fm350_diag.cmd_dipc_revert(
        args, adb_factory=lambda: adb, out_dir=tmp_path, stdin=_FakeStdin(is_tty=True), confirm_input="CHANGE DIPC"
    )
    assert rc == 0
    assert adb.dipc_config == _DELL_DIPC


def test_set_dual_readback_mismatch_restores_from_orig(tmp_path):
    backup = _make_backup_dir(tmp_path)
    adb = FakeAdb(dipc_config=_DELL_DIPC, corrupt_write=True)
    args = argparse.Namespace(backup=str(backup), dry_run=False)
    rc = fm350_diag.cmd_dipc_set_dual(
        args, adb_factory=lambda: adb, out_dir=tmp_path, stdin=_FakeStdin(is_tty=True), confirm_input="CHANGE DIPC"
    )
    assert rc == 3
    assert adb.dipc_config == _DELL_DIPC  # restored from the .orig it saved before writing


def test_set_dual_succeeds(tmp_path):
    backup = _make_backup_dir(tmp_path)
    adb = FakeAdb(dipc_config=_DELL_DIPC)
    args = argparse.Namespace(backup=str(backup), dry_run=False)
    rc = fm350_diag.cmd_dipc_set_dual(
        args, adb_factory=lambda: adb, out_dir=tmp_path, stdin=_FakeStdin(is_tty=True), confirm_input="CHANGE DIPC"
    )
    assert rc == 0
    assert adb.dipc_config == fm350_diag.DUAL_CONTENT


# --- plan / dry-run send nothing --------------------------------------------


def test_plan_touches_nothing_and_lists_level3(capsys):
    rc = fm350_diag.cmd_plan(argparse.Namespace(level=3))
    assert rc == 0
    out = capsys.readouterr().out
    assert "AT+GTFCCLOCKGEN" in out
    assert "dipc_config.new" in out


def test_plan_level0_omits_higher_levels(capsys):
    rc = fm350_diag.cmd_plan(argparse.Namespace(level=0))
    assert rc == 0
    out = capsys.readouterr().out
    assert "AT+CFUN=15" not in out
    assert "AT+CPIN?" in out


def test_experiment_aborts_without_change_when_original_unparseable(tmp_path):
    # Restoring a guessed default could itself be the damage, so nothing may be applied.
    transport = FakeTransport({"AT+GTFMODE?": "+CME ERROR: unknown\r\n"})
    session = _session(transport, level=2, out_dir=tmp_path)
    with pytest.raises(fm350_diag.DiagError):
        fm350_diag._run_fmode(session, tmp_path, measure=lambda: [], reenumerate=lambda: None)
    assert transport.commands == ["AT+GTFMODE?"]
    assert not (tmp_path / "restore.txt").exists()


def test_restore_interrupted_by_ctrl_c_reports_restore_failed(tmp_path):
    # A second Ctrl-C while the restore command is in flight must not be reported as "restored".
    transport = FakeTransport(
        {"AT+GTANTTUNINGEN?": "+GTANTTUNINGEN: 1\r\n\r\nOK\r\n"},
        fail_on={"AT+GTANTTUNINGEN=1": KeyboardInterrupt()},
    )
    session = _session(transport, level=2, out_dir=tmp_path)
    with pytest.raises(fm350_diag.RestoreFailed):
        fm350_diag._run_anttuner(session, tmp_path, measure=lambda: [], reenumerate=lambda: None)


def test_adb_read_checks_redact_log_text(tmp_path):
    class _LogAdb:
        def available(self):
            return True

        def devices(self):
            return "X\tdevice\n"

        def run(self, cmd, level=0, binary_output=False, timeout=15.0):
            if cmd.startswith("logread"):
                return "[FIBO IMEI CHECK]: 490154203237518 OK\n"
            return ""

    checks = fm350_diag._run_adb_read_checks(_LogAdb(), tmp_path, redact=True)
    assert "490154203237518" not in " ".join(c.message for c in checks)
