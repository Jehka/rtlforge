"""Parser tests built from real tool output, not invented formats.

The strings below were captured from Verilator 5.020, Icarus 12.0 and
Yosys 0.33. If you upgrade a tool, re-capture rather than assume.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rtlforge.parsers import (  # noqa: E402
    parse_icarus,
    parse_simulation,
    parse_verilator,
    parse_yosys,
    render_feedback,
)

VERILATOR_SYNTAX = """\
%Error: bad.v:9:9: syntax error, unexpected else
    9 |         else
      |         ^~~~
%Error: Exiting due to 1 error(s)
"""

VERILATOR_WARNINGS = """\
%Warning-DECLFILENAME: warn.v:1:8: Filename 'warn' does not match MODULE name: 'counter'
    1 | module counter (
      |        ^~~~~~~
                       ... For warning description see https://verilator.org/warn/DECLFILENAME?v=5.020
                       ... Use "/* verilator lint_off DECLFILENAME */" and lint_on around source to disable this message.
%Warning-WIDTHTRUNC: warn.v:9:19: Operator ASSIGNDLY expects 4 bits on the Assign RHS, but Assign RHS's CONST '8'hff' generates 8 bits.
                                : ... note: In instance 'counter'
    9 |             count <= 8'hFF;
      |                   ^~
%Error: Exiting due to 2 warning(s)
"""

ICARUS_SYNTAX = """\
bad.v:9: syntax error
bad.v:10: Syntax in assignment statement l-value.
"""


def test_verilator_syntax_error():
    diags = parse_verilator(VERILATOR_SYNTAX)
    assert len(diags) == 1, "summary line must not be counted as a diagnostic"
    assert diags[0].severity == "error"
    assert diags[0].line == 9
    assert "unexpected else" in diags[0].message


def test_verilator_warnings_carry_codes():
    diags = parse_verilator(VERILATOR_WARNINGS)
    assert len(diags) == 2
    codes = {d.code for d in diags}
    assert codes == {"DECLFILENAME", "WIDTHTRUNC"}
    assert all(d.severity == "warning" for d in diags)
    # source echo, carets and "..." continuations must be dropped
    assert not any("^~" in d.message for d in diags)


def test_verilator_clean_output():
    assert parse_verilator("") == []


def test_icarus_errors():
    diags = parse_icarus(ICARUS_SYNTAX)
    assert [d.line for d in diags] == [9, 10]
    assert all(d.severity == "error" for d in diags)


def test_simulation_pass():
    passed, diags = parse_simulation("TB_PASS\n")
    assert passed and not diags


def test_simulation_mismatches():
    out = (
        "MISMATCH: holding with en=0 -- expected count=4, got 5\n"
        "MISMATCH: holding with en=0 -- expected count=4, got 6\n"
        "TB_FAIL: 2 mismatch(es)\n"
    )
    passed, diags = parse_simulation(out)
    assert not passed
    assert len(diags) == 3
    assert any(d.code == "TB_FAIL" for d in diags)


def test_simulation_no_verdict_is_failure():
    """A testbench that printed nothing verified nothing."""
    passed, diags = parse_simulation("some unrelated output\n")
    assert not passed
    assert diags[0].code == "NO_VERDICT"


def test_simulation_caps_mismatch_flood():
    out = "\n".join(f"MISMATCH: cycle {i}" for i in range(50))
    passed, diags = parse_simulation(out)
    assert not passed
    assert len(diags) <= 10, "flooded logs must be truncated before the model"
    assert "further mismatches omitted" in diags[-1].message


def test_yosys_takes_final_cell_count():
    out = """
=== counter ===
   Number of wires:                  7
   Number of cells:                 10
     $_XOR_                          2
"""
    metrics, diags = parse_yosys(out)
    assert metrics.cells == 10
    assert metrics.wires == 7
    assert not diags


def test_yosys_error():
    metrics, diags = parse_yosys("ERROR: Module `\\foo' not found!\n")
    assert diags and diags[0].code == "YOSYS"


def test_render_feedback_deduplicates():
    _, diags = parse_simulation(
        "MISMATCH: same\nMISMATCH: same\nMISMATCH: other\nTB_FAIL: x\n"
    )
    rendered = render_feedback(diags)
    assert rendered.count("same") == 1
