"""Tests for the AST dependency graph and backward cone walker.

The trees here mirror the structure observed in real
``verilator --json-only`` output (5.052): ``loc`` is
``file,startline:startcol,endline:endcol`` and assignment children are
RHS first (``op1p``), LHS second (``op2p``).
"""

import json

import pytest

from rtlforge.cone import (
    Cone,
    DepGraph,
    Edge,
    annotate_source,
    backward_cone,
    build_graph,
    check_orientation,
    parse_loc,
    render_cone,
    reset_notes,
)


def ref(name, line=1):
    return {"type": "VARREF", "name": name, "loc": f"e,{line}:1,{line}:5"}


def const(value, line=1):
    return {"type": "CONST", "name": value, "loc": f"e,{line}:1,{line}:5"}


def assign(lhs, rhs, line, dly=True):
    """RHS first, LHS second -- Verilator's order."""
    return {
        "type": "ASSIGNDLY" if dly else "ASSIGN",
        "loc": f"e,{line}:5,{line}:7",
        "op1p": [rhs],
        "op2p": [lhs],
    }


def module(name, vars_, stmts):
    return {
        "type": "NETLIST", "name": "$root", "loc": "a,0:0,0:0",
        "modulesp": [{
            "type": "MODULE", "name": name, "loc": "e,1:8,1:18",
            "stmtsp": [
                {"type": "VAR", "name": n, "loc": f"e,1:{i*10},1:{i*10+4}",
                 "direction": d}
                for i, (n, d) in enumerate(vars_, start=2)
            ] + stmts,
        }],
    }


COUNTER = module(
    "counter_en",
    [("clk", "input"), ("rst_n", "input"), ("en", "input"),
     ("count", "output")],
    [{
        "type": "ALWAYS", "loc": "e,2:5,2:11",
        "sensesp": [{
            "type": "SENTREE", "loc": "e,2:12,2:13",
            "sensesp": [
                {"type": "SENITEM", "loc": "e,2:14,2:21",
                 "sensp": [ref("clk", 2)]},
                {"type": "SENITEM", "loc": "e,2:29,2:36",
                 "sensp": [ref("rst_n", 2)]},
            ],
        }],
        "stmtsp": [{
            "type": "BEGIN", "loc": "e,2:44,2:49",
            "stmtsp": [{
                "type": "IF", "loc": "e,3:9,3:11",
                "condp": [ref("rst_n", 3)],
                "thensp": [{
                    "type": "IF", "loc": "e,4:14,4:16",
                    "condp": [ref("en", 4)],
                    "thensp": [assign(
                        ref("count", 4),
                        {"type": "ADD", "loc": "e,4:38,4:39",
                         "op1p": [const("4'h1", 4)],
                         "op2p": [ref("count", 4)]},
                        4)],
                }],
                "elsesp": [assign(ref("count", 3), const("4'h0", 3), 3)],
            }],
        }],
    }],
)

COUNTER_SRC = """\
module counter_en(input clk, input rst_n, input en, output reg [3:0] count);
  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) count <= 4'h0;
    else if (en) count <= count + 4'h1;
  end
endmodule
"""


# ------------------------------------------------------------------ loc

@pytest.mark.parametrize("loc,expected", [
    ("e,4:29,4:31", 4),
    ("a,0:0,0:0", None),          # the $root sentinel is not a real line
    ("e,117:1,117:9", 117),
    (None, None),
    ("", None),
    ("garbage", None),
    ("e,12:3", None),             # truncated loc must not be guessed at
])
def test_parse_loc(loc, expected):
    assert parse_loc(loc) == expected


# ---------------------------------------------------------------- graph

def test_ports_and_direction():
    g = build_graph(COUNTER)
    assert g.module == "counter_en"
    assert g.ports == {"clk": "input", "rst_n": "input", "en": "input",
                       "count": "output"}
    assert g.outputs() == ["count"]


def test_data_edge_from_rhs():
    g = build_graph(COUNTER)
    assert Edge("count", "data", 4) in g.deps["count"]


def test_control_edges_from_enclosing_conditions():
    """`en` and `rst_n` never appear on a RHS but still cause bad counts."""
    g = build_graph(COUNTER)
    sources = {(e.source, e.kind) for e in g.deps["count"]}
    assert ("en", "control") in sources
    assert ("rst_n", "control") in sources


def test_clocked_block_marks_target_sequential():
    g = build_graph(COUNTER)
    assert "count" in g.sequential


def test_lines_collected_from_both_branches():
    g = build_graph(COUNTER)
    assert g.lines["count"] == {3, 4}


def test_lhs_rhs_not_inverted():
    """The constant must not become a graph node, nor `count` a source only."""
    g = build_graph(COUNTER)
    assert "count" in g.deps
    assert "4'h1" not in g.signals()
    assert not g.positional
    assert g.warning == ""


def test_undriven_signal_flagged():
    tree = module("m", [("a", "input"), ("y", "output")],
                  [assign(ref("y", 2), ref("missing", 2), 2, dly=False)])
    g = build_graph(tree)
    assert "missing" in g.unknown
    assert "a" not in g.unknown           # declared ports are not unknown


def test_named_module_selected_among_several():
    tree = {
        "type": "NETLIST", "name": "$root", "loc": "a,0:0,0:0",
        "modulesp": [
            {"type": "MODULE", "name": "helper", "loc": "e,1:1,1:2",
             "stmtsp": []},
            {"type": "MODULE", "name": "TopModule", "loc": "e,9:1,9:2",
             "stmtsp": [{"type": "VAR", "name": "q", "loc": "e,9:5,9:6",
                         "direction": "output"}]},
        ],
    }
    assert build_graph(tree, module="TopModule").module == "TopModule"
    assert build_graph(tree).module == "helper"


def test_empty_tree_is_not_an_error():
    g = build_graph({"type": "NETLIST", "name": "$root", "loc": "a,0:0,0:0"})
    assert g.module == ""
    assert g.deps == {}


# ------------------------------------------------- key-agnostic discovery

def test_unknown_child_keys_still_walked():
    """Verilator renames child keys between versions; types are stable."""
    tree = {
        "type": "NETLIST", "name": "$root", "loc": "a,0:0,0:0",
        "someNewKey": [{
            "type": "MODULE", "name": "m", "loc": "e,1:1,1:2",
            "anotherKey": [
                {"type": "VAR", "name": "a", "loc": "e,1:5,1:6",
                 "direction": "input"},
                {"type": "VAR", "name": "y", "loc": "e,1:9,1:10",
                 "direction": "output"},
                {"type": "ASSIGN", "loc": "e,2:1,2:3",
                 "op1p": [ref("a", 2)], "op2p": [ref("y", 2)]},
            ],
        }],
    }
    g = build_graph(tree)
    assert g.ports["y"] == "output"
    assert Edge("a", "data", 2) in g.deps["y"]


def test_single_dict_child_is_walked():
    """Some nodes carry one child as a dict rather than a list."""
    tree = {
        "type": "NETLIST", "name": "$root", "loc": "a,0:0,0:0",
        "modulesp": {
            "type": "MODULE", "name": "m", "loc": "e,1:1,1:2",
            "stmtsp": [{"type": "VAR", "name": "y", "loc": "e,1:5,1:6",
                        "direction": "output"},
                       {"type": "ASSIGN", "loc": "e,2:1,2:3",
                        "op1p": [ref("a", 2)], "op2p": [ref("y", 2)]}],
        },
    }
    assert build_graph(tree).ports["y"] == "output"


def test_positional_fallback_used_when_keys_unknown():
    """Last child group is the LHS. Flagged, because it is a guess."""
    tree = module("m", [("a", "input"), ("y", "output")], [{
        "type": "ASSIGN", "loc": "e,2:1,2:3",
        "weirdRhs": [ref("a", 2)],
        "weirdLhs": [ref("y", 2)],
    }])
    g = build_graph(tree)
    assert g.positional
    assert Edge("a", "data", 2) in g.deps["y"]
    assert g.warning == ""        # orientation still came out right


# -------------------------------------------------------- orientation check

def test_inverted_split_is_detected():
    """An input port as an assignment target is impossible in legal RTL."""
    tree = module("m", [("a", "input"), ("y", "output")], [{
        "type": "ASSIGN", "loc": "e,2:1,2:3",
        "weirdLhs": [ref("y", 2)],       # LHS first -- wrong order
        "weirdRhs": [ref("a", 2)],
    }])
    g = build_graph(tree)
    assert g.positional
    assert "inverted" in g.warning
    assert "a" in g.warning


def test_outputs_never_assigned_is_detected():
    tree = module("m", [("a", "input"), ("y", "output")],
                  [assign(ref("tmp", 2), ref("a", 2), 2, dly=False)])
    g = build_graph(tree)
    assert "no declared output is ever assigned" in g.warning


def test_submodule_instance_excuses_undriven_output():
    tree = module("m", [("a", "input"), ("y", "output")], [{
        "type": "CELL", "name": "u0", "loc": "e,3:1,3:2",
        "pinsp": [ref("a", 3), ref("y", 3)],
    }])
    g = build_graph(tree)
    assert g.instances == 1
    assert g.warning == ""
    # The cone must cross the instance boundary rather than dead-ending.
    assert "a" in backward_cone(g, "y").signals()


def test_instance_edges_do_not_trigger_inversion_warning():
    """Mutual instance edges make inputs look assigned; they must not count."""
    tree = module("m", [("a", "input"), ("y", "output")], [
        {"type": "CELL", "name": "u0", "loc": "e,3:1,3:2",
         "pinsp": [ref("a", 3), ref("y", 3)]},
        assign(ref("y", 4), ref("a", 4), 4, dly=False),
    ])
    assert build_graph(tree).warning == ""


def test_check_orientation_on_empty_graph_is_quiet():
    assert check_orientation(DepGraph()) == ""


# ----------------------------------------------------------------- cone

def test_cone_reaches_control_dependencies():
    g = build_graph(COUNTER)
    cone = backward_cone(g, "count")
    assert set(cone.signals()) == {"count", "en", "rst_n"}
    assert cone.lines == [3, 4]


def test_cone_depth_recorded():
    g = build_graph(COUNTER)
    depths = {n.signal: n.depth for n in backward_cone(g, "count").nodes}
    assert depths["count"] == 0
    assert depths["en"] == 1


def test_cone_handles_combinational_feedback_without_hanging():
    tree = module("m", [("y", "output")], [
        assign(ref("y", 2), ref("t", 2), 2, dly=False),
        assign(ref("t", 3), ref("y", 3), 3, dly=False),
    ])
    cone = backward_cone(build_graph(tree), "y")
    assert set(cone.signals()) == {"y", "t"}


def test_cone_depth_limit_truncates():
    stmts, vars_ = [], [("y", "output")]
    chain = ["y"] + [f"n{i}" for i in range(8)]
    for i, (dst, src) in enumerate(zip(chain, chain[1:]), start=2):
        stmts.append(assign(ref(dst, i), ref(src, i), i, dly=False))
    cone = backward_cone(build_graph(module("m", vars_, stmts)), "y",
                         max_depth=2)
    assert cone.truncated
    assert len(cone.signals()) <= 4


def test_cone_signal_cap_truncates():
    stmts = [assign(ref("y", 2),
                    {"type": "ADD", "loc": "e,2:9,2:10",
                     "op1p": [ref(f"s{i}", 2) for i in range(40)][0:1],
                     "op2p": [ref(f"s{i}", 2) for i in range(1, 40)]},
                    2, dly=False)]
    cone = backward_cone(build_graph(module("m", [("y", "output")], stmts)),
                         "y", max_signals=5)
    assert cone.truncated
    assert len(cone.signals()) <= 5


def test_cone_for_unknown_signal_says_so():
    cone = backward_cone(build_graph(COUNTER), "nope")
    assert cone.missing
    assert cone.signals() == []
    text = render_cone(cone, build_graph(COUNTER))
    assert "does not appear" in text


def test_cone_target_with_no_dependencies():
    """A constant-driven output is a legitimate, if dull, cone."""
    tree = module("m", [("y", "output")],
                  [assign(ref("y", 2), const("1'b0", 2), 2, dly=False)])
    cone = backward_cone(build_graph(tree), "y")
    assert cone.signals() == ["y"]
    assert cone.lines == [2]


# ---------------------------------------------------------------- render

def test_render_includes_lines_and_tags():
    g = build_graph(COUNTER)
    text = render_cone(backward_cone(g, "count"), g, source=COUNTER_SRC)
    assert "count [output, registered on clk, rst_n] line 3, 4" in text
    assert "controls assignment" in text
    assert "count <= 4'h0" in text
    assert "count <= count + 4'h1" in text


def test_render_surfaces_graph_warning():
    tree = module("m", [("a", "input"), ("y", "output")], [{
        "type": "ASSIGN", "loc": "e,2:1,2:3",
        "weirdLhs": [ref("y", 2)],
        "weirdRhs": [ref("a", 2)],
    }])
    g = build_graph(tree)
    text = render_cone(backward_cone(g, "a"), g)
    assert text.startswith("warning:")


def test_render_marks_undriven_signal():
    tree = module("m", [("y", "output")],
                  [assign(ref("y", 2), ref("ghost", 2), 2, dly=False)])
    g = build_graph(tree)
    text = render_cone(backward_cone(g, "y"), g)
    assert "never driven" in text


def test_annotate_source_gaps_marked():
    src = "\n".join(f"line{i}" for i in range(1, 11))
    out = annotate_source(src, [2, 9])
    assert "  ..." in out
    assert any("line2" in o for o in out)
    assert any("line9" in o for o in out)


def test_annotate_source_context_window():
    src = "\n".join(f"line{i}" for i in range(1, 11))
    out = annotate_source(src, [5], context=1)
    assert len(out) == 3
    assert "line4" in out[0] and "line6" in out[2]


def test_annotate_source_ignores_out_of_range_lines():
    assert annotate_source("only one line", [1, 99]) == ["     1 | only one line"]


def test_cone_is_json_serialisable():
    """Attempt logs are JSON; the cone has to survive the round trip."""
    g = build_graph(COUNTER)
    cone = backward_cone(g, "count")
    payload = {
        "target": cone.target,
        "lines": cone.lines,
        "truncated": cone.truncated,
        "signals": [{"signal": n.signal, "depth": n.depth, "kind": n.kind,
                     "lines": n.lines} for n in cone.nodes],
    }
    assert json.loads(json.dumps(payload))["target"] == "count"


# ----------------------------------------------------- mismatch hints

HINTS = """\
Hint: Output 'q' has 12 mismatches. First mismatch occurred at time 25.
Hint: Output 'z' has 3 mismatches. First mismatch occurred at time 105.
Mismatches: 15 in 100 samples
"""


def test_parse_mismatch_hints():
    from rtlforge.cone import parse_mismatch_hints
    hints = parse_mismatch_hints(HINTS)
    assert [h.signal for h in hints] == ["q", "z"]       # worst first
    assert hints[0].count == 12
    assert hints[0].first_time == 25
    assert hints[1].first_time == 105


def test_parse_mismatch_hints_without_time():
    from rtlforge.cone import parse_mismatch_hints
    hints = parse_mismatch_hints("Hint: Output 'out' has 4 mismatches.")
    assert hints[0].signal == "out"
    assert hints[0].first_time is None


def test_parse_mismatch_hints_on_clean_run():
    from rtlforge.cone import parse_mismatch_hints
    assert parse_mismatch_hints("Mismatches: 0 in 100 samples") == []
    assert parse_mismatch_hints("") == []
    assert parse_mismatch_hints(None) == []


def test_parse_mismatch_hints_bus_name_with_brackets():
    from rtlforge.cone import parse_mismatch_hints
    hints = parse_mismatch_hints("Hint: Output 'out[3:0]' has 7 mismatches.")
    assert hints[0].signal == "out[3:0]"


# ------------------------------------------------------ name resolution

def test_cone_resolves_bus_bitselect_name():
    g = build_graph(COUNTER)
    cone = backward_cone(g, "count[3:0]")
    assert not cone.missing
    assert cone.target == "count"
    assert "en" in cone.signals()


def test_cone_resolves_hierarchical_name():
    g = build_graph(COUNTER)
    assert not backward_cone(g, "tb.dut.count").missing


def test_cone_still_missing_for_genuine_typo():
    assert backward_cone(build_graph(COUNTER), "kount").missing


# ------------------------------------------------- omission detection
# Verilator reports port directions in upper case ("INPUT"/"OUTPUT"),
# confirmed against 5.052 on the cluster. The real child keys are
# rhsp/lhsp, which _RHS_KEYS/_LHS_KEYS already cover.

def upper_module(name, vars_, stmts):
    tree = module(name, vars_, stmts)
    for node in tree["modulesp"][0]["stmtsp"]:
        if node.get("type") == "VAR":
            node["direction"] = node["direction"].upper()
    return tree


def always_ff(sense, body_stmts, line=2):
    return {
        "type": "ALWAYS", "loc": f"e,{line}:5,{line}:11",
        "sensesp": [{
            "type": "SENTREE", "loc": f"e,{line}:12,{line}:13",
            "sensesp": [{"type": "SENITEM", "loc": f"e,{line}:14,{line}:21",
                         "sensp": [ref(s, line)]} for s in sense],
        }],
        "stmtsp": body_stmts,
    }


MISSING_ENABLE = upper_module(
    "counter_en",
    [("clk", "input"), ("rst_n", "input"), ("en", "input"),
     ("count", "output")],
    [always_ff(["clk", "rst_n"], [{
        "type": "IF", "loc": "e,3:9,3:11",
        "condp": [ref("rst_n", 3)],
        "thensp": [assign(ref("count", 3), const("4'd0", 3), 3)],
        "elsesp": [assign(
            ref("count", 4),
            {"type": "ADD", "loc": "e,4:38,4:39",
             "op1p": [const("4'd1", 4)], "op2p": [ref("count", 4)]}, 4)],
    }])],
)


def test_uppercase_directions_handled():
    """Verilator emits INPUT/OUTPUT, not input/output."""
    g = build_graph(MISSING_ENABLE)
    assert g.outputs() == ["count"]
    assert g.warning == ""


def test_omitted_enable_shows_as_unread_input():
    """The bug the cone cannot reach: `en` is never referenced at all."""
    g = build_graph(MISSING_ENABLE)
    assert g.unread_inputs == {"en"}
    # And it is genuinely absent from the cone -- that is the point.
    assert "en" not in backward_cone(g, "count").signals()


def test_clock_and_async_reset_are_not_unread():
    """They arrive via the sensitivity list, not an edge. No false alarm."""
    g = build_graph(COUNTER)
    assert g.unread_inputs == set()
    assert "clk" in g.triggers


def test_unread_input_surfaced_in_render():
    g = build_graph(MISSING_ENABLE)
    text = render_cone(backward_cone(g, "count"), g)
    assert "declared but never read: en" in text
    assert "missing rather than wrong" in text


def test_unread_plural_wording():
    tree = upper_module("m", [("a", "input"), ("b", "input"),
                              ("y", "output")],
                        [assign(ref("y", 2), const("1'b0", 2), 2, dly=False)])
    text = render_cone(backward_cone(build_graph(tree), "y"),
                       build_graph(tree))
    assert "these inputs" in text


def test_no_unread_note_when_all_inputs_used():
    g = build_graph(COUNTER)
    assert "declared but never read" not in render_cone(
        backward_cone(g, "count"), g)


def test_output_is_never_an_unread_input():
    g = build_graph(MISSING_ENABLE)
    assert "count" not in g.unread_inputs


def test_input_read_only_as_control_counts_as_read():
    """`en` used solely as an if-condition is still used."""
    g = build_graph(COUNTER)
    assert "en" not in g.unread_inputs


# ------------------------------------------------ triggers and locals
# counter_sync_reset_bug.v produces a cone identical to the correct
# design -- its bug is in the sensitivity list. The report has to make
# the trigger visible or those two designs are indistinguishable.

SYNC_RESET = upper_module(
    "counter_en",
    [("clk", "input"), ("rst_n", "input"), ("en", "input"),
     ("count", "output")],
    [always_ff(["clk"], [{                      # no negedge rst_n
        "type": "IF", "loc": "e,3:9,3:11",
        "condp": [ref("rst_n", 3)],
        "thensp": [assign(ref("count", 3), const("4'd0", 3), 3)],
        "elsesp": [{
            "type": "IF", "loc": "e,4:14,4:16",
            "condp": [ref("en", 4)],
            "thensp": [assign(
                ref("count", 4),
                {"type": "ADD", "loc": "e,4:38,4:39",
                 "op1p": [const("4'd1", 4)], "op2p": [ref("count", 4)]},
                4)],
        }],
    }])],
)


def test_trigger_recorded_per_target():
    assert build_graph(COUNTER).target_triggers["count"] == {"clk", "rst_n"}
    assert build_graph(SYNC_RESET).target_triggers["count"] == {"clk"}


def test_sync_and_async_reset_render_differently():
    """The whole point: these two designs must not read identically."""
    async_g, sync_g = build_graph(COUNTER), build_graph(SYNC_RESET)
    a = render_cone(backward_cone(async_g, "count"), async_g)
    s = render_cone(backward_cone(sync_g, "count"), sync_g)
    assert a != s
    assert "registered on clk, rst_n" in a
    assert "registered on clk" in s
    assert "reset 'rst_n' is asynchronous" in a
    assert "reset 'rst_n' is synchronous" in s


def test_reset_notes_skip_combinational_signals():
    tree = upper_module("m", [("a", "input"), ("y", "output")],
                        [assign(ref("y", 2), ref("a", 2), 2, dly=False)])
    g = build_graph(tree)
    assert reset_notes(g, ["y"]) == []


def test_reset_notes_quiet_when_no_reset_signal():
    tree = upper_module("m", [("clk", "input"), ("d", "input"),
                              ("q", "output")],
                        [always_ff(["clk"],
                                   [assign(ref("q", 3), ref("d", 3), 3)])])
    assert reset_notes(build_graph(tree), ["q"]) == []


def test_localparams_and_regs_are_not_ports():
    """seq_detect showed S0/S1/S101/state as ports with direction NONE."""
    tree = module("seq", [("clk", "input"), ("found", "output")], [])
    for extra in ("S0", "S101", "state"):
        tree["modulesp"][0]["stmtsp"].append(
            {"type": "VAR", "name": extra, "loc": "e,2:1,2:4",
             "direction": "NONE"})
    g = build_graph(tree)
    assert set(g.ports) == {"clk", "found"}
    assert {"S0", "S101", "state"} <= g.locals_
    assert g.outputs() == ["found"]


def test_internal_signal_tagged_internal_not_none():
    tree = upper_module("m", [("clk", "input"), ("y", "output")], [
        {"type": "VAR", "name": "tmp", "loc": "e,2:1,2:4",
         "direction": "NONE"},
        assign(ref("y", 3), ref("tmp", 3), 3, dly=False),
    ])
    g = build_graph(tree)
    text = render_cone(backward_cone(g, "y"), g)
    assert "tmp [internal" in text
    assert "[none" not in text


def test_undriven_internal_signal_still_flagged():
    """An internal signal read but never driven is a real bug.

    Localparams do not reach here: Verilator constant-folds them before
    emitting the AST, which is why S101 never appeared as a node in the
    seq_detect cone despite `state == S101` being in the source.
    """
    tree = upper_module("m", [("y", "output")], [
        {"type": "VAR", "name": "tmp", "loc": "e,2:1,2:4",
         "direction": "NONE"},
        assign(ref("y", 3), ref("tmp", 3), 3, dly=False),
    ])
    g = build_graph(tree)
    assert "tmp" in g.locals_
    assert "tmp" in g.unknown


# ------------------------------------------------------- cone_hint

def test_cone_hint_returns_empty_without_verilator(monkeypatch):
    """No Verilator must mean no cone, never a wrong cone."""
    from rtlforge import cone as cone_mod
    from rtlforge.astdump import AstResult
    monkeypatch.setattr("rtlforge.astdump.dump_ast",
                        lambda *a, **k: AstResult(None, False, note="x"))
    assert cone_mod.cone_hint("module m; endmodule", "m", "") == ""


def test_cone_hint_suppressed_when_graph_warns(monkeypatch):
    from rtlforge import cone as cone_mod
    from rtlforge.astdump import AstResult
    bad = module("m", [("a", "input"), ("y", "output")], [{
        "type": "ASSIGN", "loc": "e,2:1,2:3",
        "weirdLhs": [ref("y", 2)], "weirdRhs": [ref("a", 2)],
    }])
    monkeypatch.setattr("rtlforge.astdump.dump_ast",
                        lambda *a, **k: AstResult(bad, True))
    assert cone_mod.cone_hint("x", "m", "Hint: Output 'a' has 1 mismatch") == ""


def test_cone_hint_uses_named_signal_and_time(monkeypatch):
    from rtlforge import cone as cone_mod
    from rtlforge.astdump import AstResult
    monkeypatch.setattr("rtlforge.astdump.dump_ast",
                        lambda *a, **k: AstResult(COUNTER, True))
    hint = cone_mod.cone_hint(
        COUNTER_SRC, "counter_en",
        "Hint: Output 'count' has 9 mismatches. "
        "First mismatch occurred at time 35.")
    assert "'count' is wrong, first at simulation time 35" in hint
    assert "controls assignment" in hint


def test_cone_hint_falls_back_to_sole_output(monkeypatch):
    from rtlforge import cone as cone_mod
    from rtlforge.astdump import AstResult
    monkeypatch.setattr("rtlforge.astdump.dump_ast",
                        lambda *a, **k: AstResult(COUNTER, True))
    assert "count" in cone_mod.cone_hint(COUNTER_SRC, "counter_en", "")


def test_cone_hint_silent_on_multi_output_without_hints(monkeypatch):
    """Too wide to be worth the tokens when nothing names the failure."""
    from rtlforge import cone as cone_mod
    from rtlforge.astdump import AstResult
    tree = upper_module("m", [("a", "input"), ("x", "output"),
                              ("y", "output")], [
        assign(ref("x", 2), ref("a", 2), 2, dly=False),
        assign(ref("y", 3), ref("a", 3), 3, dly=False),
    ])
    monkeypatch.setattr("rtlforge.astdump.dump_ast",
                        lambda *a, **k: AstResult(tree, True))
    assert cone_mod.cone_hint("x", "m", "") == ""


def test_cone_hint_reports_unread_input_with_no_cone(monkeypatch):
    from rtlforge import cone as cone_mod
    from rtlforge.astdump import AstResult
    monkeypatch.setattr("rtlforge.astdump.dump_ast",
                        lambda *a, **k: AstResult(MISSING_ENABLE, True))
    hint = cone_mod.cone_hint("x", "counter_en",
                              "Hint: Output 'nosuchsig' has 2 mismatches.")
    assert "declared but never read: en" in hint


def test_cone_hint_respects_line_budget(monkeypatch):
    from rtlforge import cone as cone_mod
    from rtlforge.astdump import AstResult
    stmts = [assign(ref("y", 2),
                    {"type": "ADD", "loc": "e,2:9,2:10",
                     "rhsp": [ref(f"s{i}", 2) for i in range(1)],
                     "lhsp": [ref(f"s{i}", 2) for i in range(1, 60)]},
                    2, dly=False)]
    tree = upper_module("m", [("y", "output")], stmts)
    monkeypatch.setattr("rtlforge.astdump.dump_ast",
                        lambda *a, **k: AstResult(tree, True))
    hint = cone_mod.cone_hint("\n" * 80, "m", "")
    assert len(hint.splitlines()) <= cone_mod.MAX_HINT_LINES + 1


def test_cone_hint_swallows_unexpected_errors(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("verilator exploded")
    monkeypatch.setattr("rtlforge.astdump.dump_ast", boom)
    from rtlforge import cone as cone_mod
    assert cone_mod.cone_hint("x", "m", "") == ""


# --------------------------------- hints as they really arrive
# Captured verbatim from results/20261106-165439 (Prob117_circuit9,
# qwen2.5-coder:32b). parse_verilogeval_sim strips the "Hint:" prefix,
# so the regex must not require it.

REAL_FEEDBACK = """\
152 of 245 samples did not match the reference.
Output 'q' has 152 mismatches. First mismatch occurred at time 100.
Total mismatched samples is 152 out of 245 samples
"""


def test_hints_parsed_without_hint_prefix():
    from rtlforge.cone import parse_mismatch_hints
    hints = parse_mismatch_hints(REAL_FEEDBACK)
    assert len(hints) == 1
    assert hints[0].signal == "q"
    assert hints[0].count == 152
    assert hints[0].first_time == 100


def test_hints_still_parsed_with_prefix():
    """Raw testbench output keeps the prefix; both forms must work."""
    from rtlforge.cone import parse_mismatch_hints
    hints = parse_mismatch_hints(
        "Hint: Output 'q' has 3 mismatches. First mismatch at time 5.")
    assert hints[0].signal == "q" and hints[0].count == 3


def test_summary_lines_are_not_mistaken_for_hints():
    from rtlforge.cone import parse_mismatch_hints
    assert parse_mismatch_hints(
        "Total mismatched samples is 152 out of 245 samples\n"
        "152 of 245 samples did not match the reference.") == []


def test_cone_hint_uses_real_feedback_on_multi_output(monkeypatch):
    """The case the missing prefix would have silently killed."""
    from rtlforge import cone as cone_mod
    from rtlforge.astdump import AstResult
    tree = upper_module("m", [("clk", "input"), ("d", "input"),
                              ("q", "output"), ("other", "output")], [
        always_ff(["clk"], [assign(ref("q", 3), ref("d", 3), 3)]),
        assign(ref("other", 4), ref("d", 4), 4, dly=False),
    ])
    monkeypatch.setattr("rtlforge.astdump.dump_ast",
                        lambda *a, **k: AstResult(tree, True))
    hint = cone_mod.cone_hint("x\n" * 6, "m", REAL_FEEDBACK)
    assert "'q' is wrong, first at simulation time 100" in hint
    assert "other" not in hint      # only the signal that actually failed


# ------------------------------------- condition lines are suspects
# Prob117_circuit9, attempt 1 (qwen2.5-coder:32b): the bug was the wrap
# condition `if (q == 3'b111)` on line 12, which only reached the report
# through display context. The guarding condition's line must be recorded.

CIRCUIT9 = upper_module("TopModule", [("clk", "input"), ("a", "input"),
                                      ("q", "output")], [
    always_ff(["clk"], [{
        "type": "IF", "loc": "e,8:5,8:7",
        "condp": [ref("a", 8)],
        "thensp": [assign(ref("q", 9), const("3'b100", 9), 9)],
        "elsesp": [
            assign(ref("q", 11),
                   {"type": "ADD", "loc": "e,11:16,11:17",
                    "op1p": [const("1", 11)], "op2p": [ref("q", 11)]}, 11),
            {"type": "IF", "loc": "e,12:9,12:11",
             "condp": [{"type": "EQ", "loc": "e,12:15,12:17",
                        "op1p": [const("3'b111", 12)],
                        "op2p": [ref("q", 12)]}],
             "thensp": [assign(ref("q", 13), const("3'b000", 13), 13)]},
        ],
    }], line=7),
])


def test_guard_condition_line_is_a_suspect_line():
    g = build_graph(CIRCUIT9)
    cone = backward_cone(g, "q")
    assert 12 in cone.lines            # the actual bug
    assert 8 in cone.lines             # the `if (a)` guard
    assert {9, 11, 13} <= set(cone.lines)


def test_sensitivity_header_not_a_suspect_line():
    """The always line carries the async reset edge, not a suspect line."""
    g = build_graph(COUNTER)
    assert 2 not in backward_cone(g, "count").lines
    assert ("rst_n", "control") in {(e.source, e.kind)
                                    for e in g.deps["count"]}


# ------------------------------------------------ case / default arms
# Prob117_circuit9, results/20261106-170505, attempt 1. The bug was
#   16 |   default: q <= 4;
# and line 16 was missing from the cone: the default CASEITEM has no
# condsp, so the positional fallback took its body for a condition.

def caseitem(line, value, body_line, rhs):
    item = {"type": "CASEITEM", "loc": f"e,{line}:13,{line}:14",
            "stmtsp": [assign(ref("q", body_line), rhs, body_line)]}
    if value is not None:
        item["condsp"] = [const(value, line)]
    return item


CASE_DEFAULT = upper_module("TopModule", [("clk", "input"), ("a", "input"),
                                          ("q", "output")], [
    always_ff(["clk"], [{
        "type": "IF", "loc": "e,8:5,8:7",
        "condp": [ref("a", 8)],
        "thensp": [assign(ref("q", 9), const("4", 9), 9)],
        "elsesp": [{
            "type": "CASE", "loc": "e,11:9,11:13",
            "exprp": [ref("q", 11)],
            "itemsp": [
                caseitem(12, "4", 12, const("5", 12)),
                caseitem(13, "5", 13, const("6", 13)),
                caseitem(14, "6", 14, const("0", 14)),
                caseitem(15, "0", 15, const("1", 15)),
                caseitem(16, None, 16, const("4", 16)),     # default
            ],
        }],
    }], line=7),
])


def test_default_arm_line_is_in_the_cone():
    cone = backward_cone(build_graph(CASE_DEFAULT), "q")
    assert 16 in cone.lines


def test_every_case_arm_line_is_in_the_cone():
    cone = backward_cone(build_graph(CASE_DEFAULT), "q")
    assert {8, 9, 11, 12, 13, 14, 15, 16} <= set(cone.lines)


def test_case_selector_is_a_control_dependency():
    g = build_graph(CASE_DEFAULT)
    assert ("q", "control") in {(e.source, e.kind) for e in g.deps["q"]}
    assert ("a", "control") in {(e.source, e.kind) for e in g.deps["q"]}


def test_constant_case_labels_do_not_trigger_fallback():
    """`4:` has no variables; the label must still be read as condsp."""
    g = build_graph(CASE_DEFAULT)
    assert not g.positional
    assert g.warning == ""


def test_statement_group_never_mistaken_for_condition():
    """Unknown keys plus a body-only node: walk the body, don't swallow it."""
    tree = upper_module("m", [("a", "input"), ("y", "output")], [{
        "type": "CASEITEM", "loc": "e,3:1,3:2",
        "mysteryBody": [assign(ref("y", 3), ref("a", 3), 3, dly=False)],
    }])
    g = build_graph(tree)
    assert Edge("a", "data", 3) in g.deps["y"]
