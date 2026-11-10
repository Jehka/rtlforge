"""Tests for rtlforge.evidence, on traces copied verbatim from
diag_20261107-165951.md (qwen2.5-coder:32b, discriminating set)."""

from dataclasses import dataclass

from rtlforge.evidence import (
    UNCHANGED_NOTE,
    counterexamples,
    first_divergence,
    parse_trace,
    prioritise_diagnostics,
    repair_evidence,
    trace_summary,
    x_state_notes,
)

# Prob117_circuit9: reference wraps 6 -> 0, the design 6 -> 7.
P117 = """\
- ERROR [HINT] at unknown line: Output 'q' has 192 mismatches. First mismatch occurred at time 80.

Signal trace around the first mismatch (_ref is correct, _dut is yours):
```
time     | clk  | tb_mismatch | a    | q_ref | q_dut
----------------------------------------------------
60       | 0    | 0           | 0    | 5     | 5
65       | 1    | 0           | 0    | 6     | 6
70       | 0    | 0           | 0    | 6     | 6
75       | 1    | 1           | 0    | 0     | 7
80       | 0    | 1           | 0    | 0     | 7     <-- first mismatch
85       | 1    | 1           | 0    | 1     | 0
```
"""

# Prob053_m2014_q4d: reference has `initial out = 0`, the design doesn't.
P053 = """\
Signal trace around the first mismatch (_ref is correct, _dut is yours):
```
time     | clk  | tb_mismatch | in   | out_ref | out_dut
--------------------------------------------------------
0        | 0    | 1           | x    | 0       | x
5        | 1    | 0           | 0    | x       | x       <-- first mismatch
10       | 0    | 0           | 1    | x       | x
```
"""

# Prob113_2012_q1g: a 4-input truth table; the answer is in the window.
P113 = """\
Signal trace around the first mismatch (_ref is correct, _dut is yours):
```
time     | clk  | tb_mismatch | x    | f_ref | f_dut
----------------------------------------------------
0        | 0    | 0           | x    | x     | x
5        | 1    | 1           | 4    | 1     | 0
10       | 0    | 0           | 1    | 1     | 1     <-- first mismatch
15       | 1    | 1           | 9    | 0     | 1
20       | 0    | 1           | 3    | 0     | 1
25       | 1    | 1           | 13   | 0     | 1
30       | 0    | 1           | 13   | 0     | 1
35       | 1    | 1           | 5    | 1     | 0
```
"""

# Prob134_2014_q3c: two outputs, reference has don't-care x values.
P134 = """\
```
time     | clk  | tb_mismatch | x    | y    | Y0_ref | Y0_dut | z_ref | z_dut
-----------------------------------------------------------------------------
0        | 0    | 0           | x    | x    | x      | x      | x     | x
5        | 1    | 1           | 1    | 4    | 0      | 0      | 1     | 0
10       | 0    | 0           | 1    | 1    | 0      | 0      | 0     | 0     <-- first mismatch
15       | 1    | 0           | 1    | 5    | x      | 1      | x     | 0
25       | 1    | 1           | 1    | 1    | 0      | 1      | 0     | 0
```
"""


# ---------------------------------------------------------------- parse

def test_parse_trace_columns_and_rows():
    t = parse_trace(P117)
    assert t.signals() == ["q"]
    assert t.inputs() == ["a"]
    assert len(t.rows) == 6
    assert t.rows[4]["q_dut"] == "7"           # marker stripped


def test_parse_trace_multi_output():
    t = parse_trace(P134)
    assert t.signals() == ["Y0", "z"]
    assert t.inputs() == ["x", "y"]


def test_parse_trace_absent():
    assert parse_trace("- ERROR [MISMATCH]: 3 of 9 samples") is None
    assert parse_trace("") is None
    assert parse_trace(None) is None


# --------------------------------------------------------------- x-state

def test_x_state_detected_prob053():
    notes = x_state_notes(parse_trace(P053))
    assert len(notes) == 1
    assert "'out' is X (unknown) in your design at t=0" in notes[0]
    assert "reference is 0" in notes[0]


def test_reference_dont_care_is_not_an_x_problem():
    """Prob134: ref x with dut defined is a don't-care, not our fault."""
    assert x_state_notes(parse_trace(P134)) == []


def test_no_x_note_when_both_defined():
    assert x_state_notes(parse_trace(P117)) == []


# ------------------------------------------------------- first divergence

def test_first_divergence_prob117():
    text = first_divergence(parse_trace(P117))
    assert "Last agreement at t=70: q=6 in both (inputs a=0)" in text
    assert "First disagreement at t=75" in text
    assert "reference q=0, yours q=7" in text


def test_first_divergence_skips_x_rows():
    """Prob053's only disagreement involves x; no numeric divergence."""
    assert first_divergence(parse_trace(P053)) == ""


def test_first_divergence_multi_output_names_only_wrong_signal():
    text = first_divergence(parse_trace(P134))
    assert "reference z=1, yours z=0" in text
    assert "Y0" not in text.split("First disagreement")[1]


# -------------------------------------------------------- counterexamples

def test_counterexamples_prob113():
    cex = counterexamples(parse_trace(P113))
    assert cex == [
        "x=4: f should be 1 (yours 0)",
        "x=9: f should be 0 (yours 1)",
        "x=3: f should be 0 (yours 1)",
        "x=13: f should be 0 (yours 1)",
        "x=5: f should be 1 (yours 0)",
    ]


def test_counterexamples_suppressed_for_sequential_prob117():
    """a=0 maps to q=5,6,0,1 -- output depends on state; a list misleads."""
    assert counterexamples(parse_trace(P117)) == []


def test_counterexamples_respect_limit():
    assert len(counterexamples(parse_trace(P113), limit=2)) == 2


# --------------------------------------------------------------- summary

def test_trace_summary_combines_findings():
    s = trace_summary(P113)
    assert "First disagreement at t=5" in s
    assert "Input combinations your design gets wrong:" in s
    assert "  x=9: f should be 0 (yours 1)" in s


def test_trace_summary_empty_without_trace():
    assert trace_summary("no table here") == ""


# ----------------------------------------------------------- diagnostics

@dataclass
class D:
    severity: str
    code: str
    message: str


def test_errors_first_and_noise_dropped():
    """Prob156 attempt 1: warnings filled the window, the error was cut."""
    diags = [
        D("warning", "DECLFILENAME", "Filename 'design' does not match MODULE name: 'TopModule'"),
        D("warning", "WIDTHEXPAND", "Operator ADD expects 32 bits"),
        D("warning", "MULTIDRIVENPROC", "Variable written ... 'counter'"),
        D("error", "", "Unsupported: Blocking and non-blocking assignments"),
    ]
    out = prioritise_diagnostics(diags)
    assert out[0].severity == "error"
    assert all(d.code != "DECLFILENAME" for d in out)
    assert [d.code for d in out[1:]] == ["WIDTHEXPAND", "MULTIDRIVENPROC"]


def test_noise_matched_by_message_when_code_missing():
    diags = [D("warning", "", "Filename 'design' does not match MODULE name"),
             D("error", "X", "real")]
    assert [d.message for d in prioritise_diagnostics(diags)] == ["real"]


def test_all_noise_is_never_silenced():
    only = [D("warning", "DECLFILENAME", "does not match MODULE name")]
    assert prioritise_diagnostics(only) == only


def test_prioritise_handles_none_and_empty():
    assert prioritise_diagnostics(None) == []
    assert prioritise_diagnostics([]) == []


# -------------------------------------------------------- repair evidence

def test_repair_evidence_includes_trace_and_unchanged_note(monkeypatch):
    monkeypatch.setattr("rtlforge.cone.cone_hint", lambda *a, **k: "")
    ev = repair_evidence("module m; endmodule", "m", P117)
    assert "First disagreement at t=75" in ev
    assert ev.endswith(UNCHANGED_NOTE)


def test_repair_evidence_orders_trace_before_cone(monkeypatch):
    monkeypatch.setattr("rtlforge.cone.cone_hint",
                        lambda *a, **k: "backward cone for 'q':")
    ev = repair_evidence("x", "m", P117)
    assert ev.index("First disagreement") < ev.index("backward cone")


def test_repair_evidence_disabled_for_ablation(monkeypatch):
    monkeypatch.setenv("RTLFORGE_EVIDENCE", "0")
    assert repair_evidence("x", "m", P117) == ""


def test_repair_evidence_empty_when_nothing_found(monkeypatch):
    monkeypatch.setattr("rtlforge.cone.cone_hint", lambda *a, **k: "")
    assert repair_evidence("x", "m", "plain text, no trace") == ""


def test_repair_evidence_survives_cone_crash(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("x")
    monkeypatch.setattr("rtlforge.cone.cone_hint", boom)
    assert "First disagreement" in repair_evidence("x", "m", P117)


# ------------------------------------------------------- no-effect note
# Prob117, evidence-on run: attempts 1 and 2 had different code and the
# identical 192-mismatch behaviour.

from rtlforge.evidence import no_effect_note

P117_FULL = ("- ERROR [MISMATCH] at unknown line: 192 of 245 samples did not "
             "match the reference.\n" + P117)


def test_no_effect_detected_on_identical_behaviour():
    note = no_effect_note(P117_FULL, P117_FULL)
    assert "had no effect on behaviour: still 192 of 245" in note


def test_no_effect_silent_when_count_changes():
    other = P117_FULL.replace("192 of 245", "150 of 245")
    assert no_effect_note(P117_FULL, other) == ""


def test_no_effect_silent_when_divergence_changes():
    other = P117_FULL.replace("| 0     | 7    \n80", "| 0     | 5    \n80")
    other = other.replace("75       | 1    | 1           | 0    | 0     | 7",
                          "75       | 1    | 1           | 0    | 0     | 5")
    assert no_effect_note(P117_FULL, other) == ""


def test_no_effect_silent_without_previous_or_after_lint():
    assert no_effect_note(P117_FULL, "") == ""
    assert no_effect_note(P117_FULL, "- ERROR [PROCASSWIRE] at line 54") == ""


def test_no_effect_note_goes_first(monkeypatch):
    monkeypatch.setattr("rtlforge.cone.cone_hint", lambda *a, **k: "CONE")
    ev = repair_evidence("x", "m", P117_FULL, previous=P117_FULL)
    assert ev.startswith("Your previous change had no effect")
    assert ev.index("First disagreement") < ev.index("CONE")


# ------------------------------------------------------ ablation arms

from rtlforge.evidence import evidence_parts


def test_parts_parsing(monkeypatch):
    for raw, want in [("1", {"trace", "cone", "noeffect"}), ("on", {"trace", "cone", "noeffect"}),
                      ("0", set()), ("off", set()), ("cone", {"cone"}),
                      ("trace,noeffect", {"trace", "noeffect"}), ("bogus", set())]:
        monkeypatch.setenv("RTLFORGE_EVIDENCE", raw)
        assert evidence_parts() == want, raw


def test_cone_only_arm_has_no_trace_reading(monkeypatch):
    monkeypatch.setenv("RTLFORGE_EVIDENCE", "cone")
    monkeypatch.setattr("rtlforge.cone.cone_hint", lambda *a, **k: "CONE")
    ev = repair_evidence("x", "m", P117_FULL, previous=P117_FULL)
    assert "CONE" in ev
    assert "First disagreement" not in ev and "no effect" not in ev


def test_trace_only_arm_never_builds_a_cone(monkeypatch):
    monkeypatch.setenv("RTLFORGE_EVIDENCE", "trace,noeffect")
    def boom(*a, **k):
        raise AssertionError("cone built in trace-only arm")
    monkeypatch.setattr("rtlforge.cone.cone_hint", boom)
    ev = repair_evidence("x", "m", P117_FULL, previous=P117_FULL)
    assert "First disagreement" in ev and "no effect on behaviour" in ev
