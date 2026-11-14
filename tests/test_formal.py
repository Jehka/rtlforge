"""Tests for rtlforge.formal -- need a Yosys binary (YOSYS env or `yosys`).

Designs are VerilogEval problems and the model attempts logged on
2026-11-07/08; the verdicts were checked by hand before being pinned here.
"""

import os
import shutil

import pytest

from rtlforge.formal import YOSYS, distinguish, render_ab_question, render_question

pytestmark = pytest.mark.skipif(shutil.which(YOSYS) is None,
                                reason=f"{YOSYS} not installed")

REF117 = """module RefModule (input clk, input a, output reg [2:0] q);
  always @(posedge clk)
    if (a) q <= 4;
    else if (q == 6) q <= 0;
    else q <= q + 1'b1;
endmodule"""

WRAP7 = """module TopModule (input clk, input a, output reg [2:0] q);
always @(posedge clk) begin
    if (a) q <= 3'b100;
    else begin
        q <= q + 1;
        if (q == 3'b111) q <= 3'b000;
    end
end
endmodule"""

WRAP6_CASE = """module TopModule (input clk, input a, output reg [2:0] q);
always @(posedge clk) begin
    if (a) q <= 4;
    else case (q)
        6: q <= 0;
        default: q <= q + 3'd1;
    endcase
end
endmodule"""

REF053 = """module RefModule (input clk, input in, output logic out);
  initial out = 0;
  always@(posedge clk) out <= in ^ out;
endmodule"""

NOINIT053 = """module TopModule (input clk, input in, output out);
    reg q;
    always @(posedge clk) q <= in ^ q;
    assign out = q;
endmodule"""

REF113 = """module RefModule (input [3:0] x, output logic f);
  always_comb case (x)
    4'h0: f = 1; 4'h1: f = 1; 4'h2: f = 0; 4'h3: f = 0;
    4'h4: f = 1; 4'h5: f = 1; 4'h6: f = 1; 4'h7: f = 0;
    4'h8: f = 0; 4'h9: f = 0; 4'ha: f = 0; 4'hb: f = 0;
    4'hc: f = 1; 4'hd: f = 0; 4'he: f = 1; 4'hf: f = 1;
  endcase
endmodule"""

SOP113_WRONG = """module TopModule (input [3:0] x, output f);
assign f = (x[3] & ~x[2]) | (~x[3] & ~x[2] & ~x[1]) | (x[3] & x[2] & ~x[0]);
endmodule"""


def test_wrong_wrap_value_found_as_logic_difference():
    d = distinguish(REF117, WRAP7, top_gold="RefModule")
    assert d.verdict == "logic"
    s = d.counterexample.first_difference()
    assert (s.gold["q"], s.gate["q"]) == ("0", "7")   # 6 -> 0 vs 6 -> 7


def test_differently_written_equivalent_design_is_equivalent():
    assert distinguish(REF117, WRAP6_CASE, top_gold="RefModule").verdict == "equivalent"


def test_missing_initial_is_init_only():
    d = distinguish(REF053, NOINIT053, top_gold="RefModule")
    assert d.verdict == "init-only"
    s = d.counterexample.first_difference()
    assert s.gold["out"] == "0" and s.gate["out"] == "x"


def test_combinational_difference_names_a_wrong_input():
    d = distinguish(REF113, SOP113_WRONG, top_gold="RefModule")
    assert d.verdict == "logic"
    s = d.counterexample.first_difference()
    assert s.gold["f"] != s.gate["f"]
    assert "x" in s.inputs


def test_same_design_is_equivalent_to_itself():
    assert distinguish(WRAP7, WRAP7).verdict == "equivalent"


def test_helper_modules_with_the_same_name_do_not_clash():
    a = """module half(input a, b, output s); assign s = a ^ b; endmodule
module TopModule(input a, b, output y); half h(.a(a), .b(b), .s(y)); endmodule"""
    b = """module half(input a, b, output s); assign s = a | b; endmodule
module TopModule(input a, b, output y); half h(.a(a), .b(b), .s(y)); endmodule"""
    d = distinguish(a, b)
    assert d.verdict == "logic"          # xor vs or differ at a=b=1


def test_interface_mismatch_is_an_error_not_a_verdict():
    other = "module TopModule(input a, output z); assign z = a; endmodule"
    d = distinguish(WRAP7, other)
    assert d.verdict == "error"
    assert d.error


def test_reference_dont_care_is_not_a_difference():
    ref = """module RefModule(input [1:0] s, output reg y);
  always_comb case (s) 2'd0: y = 0; 2'd1: y = 1; default: y = 1'bx; endcase
endmodule"""
    gate = """module TopModule(input [1:0] s, output y); assign y = s[0] & ~s[1]; endmodule"""
    assert distinguish(ref, gate, top_gold="RefModule").verdict == "equivalent"


def test_question_reads_as_a_spec_query():
    q = render_question(distinguish(REF117, WRAP7, top_gold="RefModule"), "A", "B")
    assert "candidate A outputs q=0\n  candidate B outputs q=7" in q
    assert "not the edge at the end of this cycle" in q    # exact timing
    assert q.rstrip().endswith("?")
    assert "clk=" not in q                # clocks are not inputs to ask about


def test_no_question_for_equivalent_designs():
    assert render_question(distinguish(REF117, WRAP6_CASE, top_gold="RefModule")) == ""


# ------------------------------------------------------------ reset assumption

from rtlforge.formal import reset_inputs

ONEHOT_REF = """module RefModule(input clk, input reset, input go, output reg [3:0] s);
  always @(posedge clk) if (reset) s <= 4'b0001; else if (go) s <= {s[2:0], s[3]};
endmodule"""
# Identical from reset; differs only in the unreachable all-zero state.
ONEHOT_UNREACHABLE = """module TopModule(input clk, input reset, input go, output reg [3:0] s);
  always @(posedge clk) if (reset) s <= 4'b0001;
    else if (go) s <= (s == 4'b0000) ? 4'b1111 : {s[2:0], s[3]};
endmodule"""


@pytest.mark.parametrize("ports,want", [
    ("input clk, input reset, input go", {"reset": 1}),
    ("input clk, input areset, input x", {"areset": 1}),
    ("input wire clk, input wire rst_n, input d", {"rst_n": 0}),
    ("input clk, input aresetn", {"aresetn": 0}),
    ("input clk, input [3:0] data, input resetn", {"resetn": 0}),
    ("input clk, input in, input first", {}),
])
def test_reset_inputs_detected_with_polarity(ports, want):
    assert reset_inputs(f"module TopModule({ports}, output y); endmodule") == want


def test_unreachable_state_difference_is_not_reported():
    """A testbench resets first; formal must too, or it reports phantoms."""
    assert distinguish(ONEHOT_REF, ONEHOT_UNREACHABLE,
                       top_gold="RefModule").verdict == "equivalent"


def test_without_reset_assumption_the_phantom_appears():
    assert distinguish(ONEHOT_REF, ONEHOT_UNREACHABLE, top_gold="RefModule",
                       assume_reset=False).verdict == "logic"


def test_wrong_reset_value_still_caught():
    bad = ONEHOT_UNREACHABLE.replace("s <= 4'b0001;\n", "s <= 4'b0010;\n", 1)
    assert distinguish(ONEHOT_REF, bad, top_gold="RefModule").verdict == "logic"


def test_shortest_counterexample_is_short():
    """Wrap at 7 vs wrap at 6: from reset-free zero, a=1 then 3 increments."""
    d = distinguish(REF117, WRAP7, top_gold="RefModule")
    s = d.counterexample.first_difference()
    assert s.t <= 7                       # longest-witness search gave 14
    assert (s.gold["q"], s.gate["q"]) == ("0", "7")


def test_shortest_can_be_disabled():
    d = distinguish(REF117, WRAP7, top_gold="RefModule", shortest=False)
    assert d.verdict == "logic"



# ---------------------------------------------------- M1a audit fixes

PRERESET_REF = """module RefModule(input clk, input reset, input in, output out);
  parameter A=0, B=1; reg state;
  always @(posedge clk) if (reset) state <= B; else state <= in ? state : ~state;
  assign out = (state == B);
endmodule"""
# Different state encoding: identical after reset, differs only in the
# power-up state seen during the reset cycle (Prob107/141/149 in the audit).
PRERESET_GATE = """module TopModule(input clk, input reset, input in, output out);
  reg s;
  always @(posedge clk) if (reset) s <= 0; else s <= in ? s : ~s;
  assign out = (s == 0);
endmodule"""


def test_reset_cycle_is_not_compared():
    assert distinguish(PRERESET_REF, PRERESET_GATE,
                       top_gold="RefModule").verdict == "equivalent"


def test_reset_cycle_difference_visible_without_reset_assumption():
    assert distinguish(PRERESET_REF, PRERESET_GATE, top_gold="RefModule",
                       assume_reset=False).verdict != "equivalent"


def test_clockless_design_asked_as_plain_input_output():
    """Prob083 in the audit: the model treated an XNOR as registered."""
    ref = "module RefModule(input x, input y, output z); assign z = ~(x ^ y); endmodule"
    gate = "module TopModule(input x, input y, output z); assign z = x & y; endmodule"
    q, _ = render_ab_question(distinguish(ref, gate, top_gold="RefModule"), True)
    assert q.splitlines()[1].startswith("With inputs ")
    assert "cycle" not in q and "registered" not in q


def test_wide_values_shown_in_hex_with_differing_bits():
    """Prob092 in the audit: 31-digit decimals."""
    ref = "module RefModule(input [99:0] in, output [99:0] o); assign o = in; endmodule"
    # differs only when bit 99 is set, so the counterexample must be wide
    gate = "module TopModule(input [99:0] in, output [99:0] o); assign o = {in[99:1], in[0] ^ in[99]}; endmodule"
    q, _ = render_ab_question(distinguish(ref, gate, top_gold="RefModule"), True)
    assert "0x" in q and "differ in bit 0" in q


def test_reset_named_in_question():
    q, _ = render_ab_question(distinguish(REF117.replace("input a,", "input reset, input a,"),
                                         WRAP7.replace("input a,", "input reset, input a,"),
                                         top_gold="RefModule"), True)
    assert "Reset (reset=1) is asserted in cycle 1." in q


@pytest.mark.parametrize("body,want", [
    # CVDP lfsr_0001: active-low reset named plain `reset`
    ("always_ff @(posedge clk or negedge reset) if (!reset) q <= 0; else q <= d;", 0),
    ("always @(posedge clk) if (~reset) q <= 0; else q <= d;", 0),
    ("always @(posedge clk) if (reset == 1'b0) q <= 0; else q <= d;", 0),
    ("always @(posedge clk or posedge reset) if (reset) q <= 0; else q <= d;", 1),
    ("always @(posedge clk) if (reset) q <= 0; else q <= d;", 1),
    ("always @(posedge clk) q <= d;", 1),            # unused: falls back to the name
])
def test_reset_polarity_from_usage(body, want):
    rtl = f"module m(input clk, input reset, input d, output reg q); {body} endmodule"
    assert reset_inputs(rtl) == {"reset": want}


def test_reset_n_name_overridden_by_usage():
    rtl = "module m(input clk, input rst_n, input d, output reg q); always @(posedge clk) if (rst_n) q <= 0; else q <= d; endmodule"
    assert reset_inputs(rtl) == {"rst_n": 1}


def test_line_comment_does_not_swallow_rest_of_file():
    """`//.*` under re.S ate everything after the first comment."""
    rtl = ("// header comment\nmodule m(input clock, reset, input d, output reg q);\n"
           "always_ff @(posedge clock or negedge reset) // async\n"
           "  if (!reset) q <= 0; else q <= d;\nendmodule\n")
    assert reset_inputs(rtl) == {"reset": 0}


@pytest.mark.parametrize("rtl,want", [
    ("module m(input i_clk, input i_rst_n, output reg q); always @(posedge i_clk or negedge i_rst_n) if(!i_rst_n) q<=0; else q<=1; endmodule", {"i_rst_n": 0}),
    ("module m(input pclk, input presetn, output reg q); always @(posedge pclk) if(!presetn) q<=0; else q<=1; endmodule", {"presetn": 0}),
    ("module m(input axi_aclk, input axi_aresetn, output reg q); always @(posedge axi_aclk) if(axi_aresetn==1'b0) q<=0; else q<=1; endmodule", {"axi_aresetn": 0}),
    ("module m(input clk, input i_reset_bar, output reg q); always @(posedge clk) if(!i_reset_bar) q<=0; else q<=1; endmodule", {"i_reset_bar": 0}),
    ("module m(input clk_in, input rst_in, output reg q); always @(posedge clk_in or posedge rst_in) if(rst_in) q<=0; else q<=1; endmodule", {"rst_in": 1}),
    ("module m(input clk, input [7:0] preset_value, input load, output reg [7:0] q); always @(posedge clk) if(load) q<=preset_value; endmodule", {}),
])
def test_cvdp_reset_names(rtl, want):
    """CVDP naming (2026-11-14 audit): missed resets meant power-up states were compared."""
    assert reset_inputs(rtl) == want


def test_cvdp_clock_names():
    from rtlforge.formal import is_clock_name
    assert all(is_clock_name(n) for n in ("pclk", "clk_in", "axi_aclk", "hclk", "w_clk", "clk1"))
    assert not any(is_clock_name(n) for n in ("sel", "block", "clkdiv_en", "data"))


def test_clock_named_clk_in_renders_as_sequential():
    rtl_a = "module TopModule(input clk_in, input rst_in, input d, output reg q); always @(posedge clk_in) if (rst_in) q <= 0; else q <= d; endmodule"
    rtl_b = "module TopModule(input clk_in, input rst_in, input d, output reg q); always @(posedge clk_in) if (rst_in) q <= 0; else q <= ~d; endmodule"
    d = distinguish(rtl_a, rtl_b)
    assert d.resets == {"rst_in": 1} and "clk_in" in d.clocks
    q = render_question(d, "A", "B")
    assert "Reset (rst_in=1) is asserted in cycle 1." in q and "clk_in=" not in q and "cycle 2" in q
