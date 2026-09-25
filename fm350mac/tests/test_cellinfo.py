"""Pure cell/signal response parser tests (cellinfo.py). No USB, no hardware.

Fixtures are real FM350-GL responses from this modem (see docs/bench-log.md's
cable-swap entry), with TAC/cell ID replaced by placeholders (1A2B / 0012345AB).
"""

from fm350mac import cellinfo

CESQ_LTE_SAMPLE = "+CESQ: 17,99,255,255,4,29,75,52,57\r\n\r\nOK\r\n"
CESQ_NO_SIGNAL = "+CESQ: 99,99,255,255,255,255,255,255,255\r\n\r\nOK\r\n"
GTCCINFO_LTE_SAMPLE = (
    "+GTCCINFO: \r\n"
    "1,4,262,2,1A2B,0012345AB,100,42,,,-7,29,29,4\r\n"
    "\r\n"
    "2,4,,,FFFF,00FFFFFFF,9460,71,,43,43,10\r\n"
    "2,4,,,FFFF,00FFFFFFF,6300,207,,42,42,15\r\n"
    "\r\nOK\r\n"
)


# --- +CESQ -------------------------------------------------------------


def test_parse_cesq_decodes_lte_and_nr_fields():
    sq = cellinfo.parse_cesq(CESQ_LTE_SAMPLE)
    assert sq is not None
    assert sq.rsrp_dbm == -112.0  # 29 -> -141 + 29 (lower bound of the range)
    assert sq.rsrq_db == -18.0  # 4 -> -20 + 0.5*4
    assert sq.ss_rsrp_dbm == -105.0  # 52 -> -157 + 52
    assert sq.ss_rsrq_db == -6.0  # 75 -> -43.5 + 0.5*75
    assert sq.ss_sinr_db == 5.0  # 57 -> -23.5 + 0.5*57
    # rscp/ecno are UMTS-only fields; this cell is LTE, so both read "unknown".
    assert sq.rscp_dbm is None
    assert sq.ecno_db is None


def test_parse_cesq_all_unknown_when_no_signal():
    sq = cellinfo.parse_cesq(CESQ_NO_SIGNAL)
    assert sq is not None
    assert sq.rxlev is None
    assert sq.rsrp_dbm is None
    assert sq.rsrq_db is None
    assert sq.ss_rsrp_dbm is None
    assert sq.ss_rsrq_db is None
    assert sq.ss_sinr_db is None


def test_parse_cesq_missing():
    assert cellinfo.parse_cesq("ERROR\r\n") is None


# --- +GTCCINFO -----------------------------------------------------------


def test_parse_gtccinfo_serving_and_neighbour_cells():
    cells = cellinfo.parse_gtccinfo(GTCCINFO_LTE_SAMPLE)
    assert len(cells) == 3

    serving = cellinfo.serving_cell(cells)
    assert isinstance(serving, cellinfo.LteCell)
    assert serving.is_serving is True
    assert serving.mcc == 262
    assert serving.mnc == 2
    assert serving.tac == "1A2B"
    assert serving.cell_id == "0012345AB"
    assert serving.earfcn == 100
    assert serving.band == 1  # EARFCN 100 -> LTE band 1 (matches docs/bench-log.md)
    assert serving.pci == 42
    assert serving.rsrp_dbm == -112.0
    assert serving.rsrq_db == -18.0

    neighbours = [c for c in cells if not c.is_serving]
    assert len(neighbours) == 2
    assert {n.band for n in neighbours} == {28, 20}  # EARFCN 9460/6300 -> B28/B20
    assert all(n.tac == "FFFF" for n in neighbours)


def test_parse_gtccinfo_empty_response_returns_no_cells():
    assert cellinfo.parse_gtccinfo("+GTCCINFO: \r\n\r\nOK\r\n") == []


def test_no_cells_measured_true_when_nothing_heard():
    sq = cellinfo.parse_cesq(CESQ_NO_SIGNAL)
    assert cellinfo.no_cells_measured(sq, []) is True


def test_no_cells_measured_false_when_a_cell_is_present():
    cells = cellinfo.parse_gtccinfo(GTCCINFO_LTE_SAMPLE)
    sq = cellinfo.parse_cesq(CESQ_LTE_SAMPLE)
    assert cellinfo.no_cells_measured(sq, cells) is False


def test_neighbour_count_by_band():
    cells = cellinfo.parse_gtccinfo(GTCCINFO_LTE_SAMPLE)
    counts = cellinfo.neighbour_count_by_band(cells)
    assert counts == {"LTE B28": 1, "LTE B20": 1}


# --- EARFCN/NR-ARFCN -> band -----------------------------------------------


def test_lte_band_for_earfcn_known_bands():
    assert cellinfo.lte_band_for_earfcn(100) == 1
    assert cellinfo.lte_band_for_earfcn(3200) == 7
    assert cellinfo.lte_band_for_earfcn(3600) == 8
    assert cellinfo.lte_band_for_earfcn(6300) == 20
    assert cellinfo.lte_band_for_earfcn(9460) == 28


def test_lte_band_for_earfcn_unknown_returns_none():
    assert cellinfo.lte_band_for_earfcn(999999) is None
    assert cellinfo.lte_band_for_earfcn(None) is None


def test_nr_band_for_arfcn_known_bands():
    assert cellinfo.nr_band_for_arfcn(428000) == "n1"
    assert cellinfo.nr_band_for_arfcn(636667) == "n78"


def test_nr_band_for_arfcn_unknown_returns_none():
    assert cellinfo.nr_band_for_arfcn(1) is None
    assert cellinfo.nr_band_for_arfcn(None) is None


# --- +COPS ------------------------------------------------------------------


def test_parse_cops_registered_lte():
    op = cellinfo.parse_cops('+COPS: 0,2,"26202",13\r\n\r\nOK\r\n')
    assert op is not None
    assert op.mode == 0
    assert op.name == "26202"
    assert op.act == 13
    assert op.act_name == "LTE + NR dual connectivity (EN-DC, 5G NSA)"


def test_parse_cops_no_operator():
    op = cellinfo.parse_cops('+COPS:0,255,"",0\r\n\r\nOK\r\n')
    assert op is not None
    assert op.name is None


def test_parse_cops_missing():
    assert cellinfo.parse_cops("ERROR\r\n") is None


# --- +GTSENRDTEMP ------------------------------------------------------------


def test_parse_gtsenrdtemp_and_conversion():
    raw = cellinfo.parse_gtsenrdtemp("+GTSENRDTEMP: 1,32507\r\n\r\nOK\r\n")
    assert raw == 32507
    assert cellinfo.millidegrees_to_celsius(raw) == 32.5


def test_parse_gtsenrdtemp_missing():
    assert cellinfo.parse_gtsenrdtemp("ERROR\r\n") is None
    assert cellinfo.millidegrees_to_celsius(None) is None


# --- +ERAT --------------------------------------------------------------


def test_parse_erat_decodes_act_and_mode():
    rat = cellinfo.parse_erat("+ERAT: 13,0,21,0,0\r\n\r\nOK\r\n")
    assert rat is not None
    assert rat.act == 13
    assert rat.act_name == "NG-RAN"
    assert rat.rat_mode == 21
    assert rat.rat_mode_name == "UMTS+LTE+NR"


def test_parse_erat_missing():
    assert cellinfo.parse_erat("ERROR\r\n") is None


def test_lte_band_table_edges_per_36101():
    assert cellinfo.lte_band_for_earfcn(1000) == 2
    assert cellinfo.lte_band_for_earfcn(9800) == 30
    assert cellinfo.lte_band_for_earfcn(9900) is None  # band 31, not modelled
    assert cellinfo.lte_band_for_earfcn(9920) == 32
    assert cellinfo.lte_band_for_earfcn(10359) == 32
