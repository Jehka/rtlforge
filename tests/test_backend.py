"""Backend stage tests.

The STA fixtures are real OpenSTA `report_checks` output. If you upgrade
OpenSTA, re-capture rather than assume.
"""

import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from rtlforge.backend import (  # noqa: E402
    parse_sta,
    parse_techmap,
    techmap,
    write_sdc,
)

# Real OpenSTA output, met timing.
STA_MET = """\
Startpoint: r2 (rising edge-triggered flip-flop clocked by clk)
Endpoint: r3 (rising edge-triggered flip-flop clocked by clk)
Path Group: clk
Path Type: max

  Delay    Time   Description
---------------------------------------------------------
   0.00    0.00   clock clk (rise edge)
   0.00    0.00   clock network delay (ideal)
   0.00    0.00 ^ r2/CK (DFF_X1)
   0.23    0.23 v r2/Q (DFF_X1)
   0.08    0.31 v u1/Z (BUF_X1)
   0.10    0.41 v u2/ZN (AND2_X1)
   0.00    0.41 v r3/D (DFF_X1)
           0.41   data arrival time

  10.00   10.00   clock clk (rise edge)
   0.00   10.00   clock network delay (ideal)
   0.00   10.00   clock reconvergence pessimism
          10.00 ^ r3/CK (DFF_X1)
  -0.16    9.84   library setup time
           9.84   data required time
---------------------------------------------------------
           9.84   data required time
          -0.41   data arrival time
---------------------------------------------------------
           9.43   slack (MET)
"""

STA_VIOLATED = STA_MET.replace("9.43   slack (MET)", "-0.34   slack (VIOLATED)")


def test_sta_met_is_not_a_violation():
    t, diags = parse_sta(STA_MET, clock_period_ns=10.0)
    assert t.wns == 9.43
    assert not t.violated
    assert not diags


def test_sta_violation_reports_the_critical_path():
    t, diags = parse_sta(STA_VIOLATED, clock_period_ns=10.0)
    assert t.violated
    assert t.wns == -0.34
    assert t.startpoint.startswith("r2")
    assert t.endpoint.startswith("r3")
    # The path must name cells and their delays -- "too slow" is not actionable,
    # "the AND2_X1 after r2/Q costs 0.10ns" is.
    assert any("BUF_X1" in step for step in t.critical_path)
    assert any("+0.23ns" in step for step in t.critical_path)
    assert diags and diags[0].code == "TIMING"
    assert "-0.340" in diags[0].message or "-0.34" in diags[0].message


def test_fmax_needs_the_constraint_to_mean_anything():
    t, _ = parse_sta(STA_MET, clock_period_ns=10.0)
    # 10ns period, 9.43ns slack -> 0.57ns achieved -> ~1754 MHz
    assert t.fmax_mhz is not None and 1700 < t.fmax_mhz < 1800

    t_noclk, _ = parse_sta(STA_MET)
    assert t_noclk.fmax_mhz is None, (
        "fmax without a stated constraint is meaningless and must not be "
        "reported"
    )


def test_sdc_constrains_ports_not_just_the_clock():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "c.sdc"
        write_sdc(p, "clk", 2.0)
        text = p.read_text()
        assert "create_clock" in text and "-period 2.0" in text
        # Without I/O delays the tool gives port paths a full period and
        # reports timing no real chip would meet.
        assert "set_input_delay" in text and "set_output_delay" in text


def test_techmap_parses_area_and_flops():
    out = """
   Number of cells:                 11
     DFFR_X1                         4
     MUX2_X1                         1
     NAND2_X1                        1
     XNOR2_X1                        3

   Chip area for module '\\\\counter_en': 31.122000
"""
    m, diags = parse_techmap(out)
    assert m.area_um2 == pytest.approx(31.122)
    assert m.cells == 11
    assert m.flops == 4, "sequential count is the key sanity check on a map"
    assert m.cell_mix["XNOR2_X1"] == 3
    assert not diags


def test_techmap_reports_yosys_errors():
    m, diags = parse_techmap("ERROR: Can't open liberty file\n")
    assert diags and diags[0].code == "YOSYS"


LIBERTY = Path("/tmp/nangate45.lib")


@pytest.mark.skipif(
    not LIBERTY.exists() or shutil.which("yosys") is None,
    reason="needs yosys and a liberty file",
)
def test_techmap_end_to_end(tmp_path):
    shutil.copy(ROOT / "examples" / "counter_good.v", tmp_path / "design.v")
    sr = techmap(tmp_path / "design.v", tmp_path, "counter_en", LIBERTY)
    assert sr.passed
    assert (tmp_path / "mapped.v").exists(), "STA needs the gate netlist"
    assert sr.map_metrics.flops == 4
    assert sr.map_metrics.area_um2 > 0


# --- infrastructure failures must never read as clean passes ---

def test_infra_failure_detects_missing_tool():
    from rtlforge.runners import infra_failure
    assert infra_failure(127, "NOTFOUND: docker is not installed")
    assert infra_failure(0, "bash: docker: command not found")
    assert infra_failure(0, "Error response from daemon: no such container")
    assert infra_failure(0, 'service "eda" is not running')
    assert infra_failure(124, "TIMEOUT: command exceeded 60s")


def test_infra_failure_ignores_ordinary_tool_output():
    from rtlforge.runners import infra_failure
    assert infra_failure(1, "%Error: bad.v:9:9: syntax error") is None
    assert infra_failure(0, "") is None
    assert infra_failure(0, "Mismatches: 0 in 110 samples") is None


def test_unrunnable_lint_fails_rather_than_passing(tmp_path, monkeypatch):
    """The bug this guards: a command that cannot execute produces no error
    lines, the parser finds nothing wrong, and the stage reports PASS."""
    import rtlforge.runners as R

    monkeypatch.setattr(R, "EDA_BACKEND", "docker:does-not-exist")
    monkeypatch.setattr(
        R, "_run",
        lambda *a, **k: (127, "NOTFOUND: docker is not installed"),
    )
    design = tmp_path / "design.v"
    design.write_text("module m; endmodule\n")
    sr = R.lint(design, tmp_path)
    assert not sr.passed, "a tool that never ran must not report success"
    assert "did not run" in sr.note
