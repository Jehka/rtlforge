"""Tests for the M1a question builder and answer parser (no LLM needed)."""

import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from spec_query_eval import build_prompt, parse_answer, wilson   # noqa: E402

from rtlforge.formal import YOSYS, distinguish, render_ab_question


@pytest.mark.parametrize("text,want", [
    ("A\nBecause the counter wraps at 6.", "A"),
    ("B", "B"),
    ("  b) the spec says 7", "B"),
    ("**A**\nreason", "A"),
    ("NEITHER\nthe spec requires 3", "NEITHER"),
    ("neither -- both wrong", "NEITHER"),
    ("\n\nA\n", "A"),
    ("The answer is A", "UNPARSED"),       # must lead with the choice
    ("Both are fine", "UNPARSED"),
    ("", "UNPARSED"),
    (None, "UNPARSED"),
])
def test_parse_answer(text, want):
    assert parse_answer(text) == want


def test_prompt_contains_spec_and_question_and_no_reference_hint():
    msgs = build_prompt("Build a counter.", "Two implementations ...")
    user = msgs[-1]["content"]
    assert "Build a counter." in user and "Two implementations" in user
    assert "reference" not in user.lower()


def test_wilson_interval():
    lo, hi = wilson(40, 50)
    assert 0.66 < lo < 0.70 and 0.88 < hi < 0.90
    assert wilson(0, 0) == (0.0, 0.0)


needs_yosys = pytest.mark.skipif(shutil.which(YOSYS) is None, reason="no yosys")

REF = """module RefModule (input clk, input a, output reg [2:0] q);
  always @(posedge clk)
    if (a) q <= 4; else if (q == 6) q <= 0; else q <= q + 1'b1;
endmodule"""
BAD = """module TopModule (input clk, input a, output reg [2:0] q);
always @(posedge clk) begin
    if (a) q <= 3'b100;
    else begin q <= q + 1; if (q == 3'b111) q <= 3'b000; end
end
endmodule"""


@needs_yosys
@pytest.mark.parametrize("gold_is_a", [True, False])
def test_ab_question_puts_reference_on_the_requested_side(gold_is_a):
    d = distinguish(REF, BAD, top_gold="RefModule")
    q, gold = render_ab_question(d, gold_is_a)
    assert gold == ("A" if gold_is_a else "B")
    ref_val, dut_val = "q=0", "q=7"
    a_line = next(l for l in q.splitlines() if "candidate A outputs" in l)
    assert (ref_val if gold_is_a else dut_val) in a_line
    # A is always mentioned first, whichever side the reference is on
    assert q.index("candidate A") < q.index("candidate B")
    assert "reference" not in q.lower()


@needs_yosys
def test_ab_question_empty_for_equivalent():
    assert render_ab_question(distinguish(REF, REF.replace("RefModule", "TopModule"),
                                          top_gold="RefModule"), True) == ("", "")


from spec_query_eval import classify   # noqa: E402


@pytest.mark.parametrize("text,finish,want", [
    ("", "length", "TRUNCATED"),            # qwen3.8: thinking ate the budget
    ("", "stop", "EMPTY"),
    ("   \n", None, "EMPTY"),
    ("ERROR failed after 4 attempts: Connection refused", None, "ERROR"),
    ("B\nbecause", "stop", "B"),
    ("maybe", "stop", "UNPARSED"),
])
def test_classify(text, finish, want):
    assert classify(text, finish) == want


def test_server_check_reports_down_for_closed_port():
    from spec_query_eval import server_up
    assert server_up("http://127.0.0.1:9/v1") is False
