"""Backward dependency cones from Verilator's JSON AST.

The repair prompt gets better when it is told *which lines could have
caused this output to be wrong* instead of just "output q mismatched".
This module builds a signal-level dependency graph from
``verilator --json-only`` output and walks it backwards from a
mismatching signal.

Two deliberate design choices:

1. Child discovery is key-agnostic. The AST dump confirmed node *types*
   (VARREF, ASSIGNDLY, IF, ...) but Verilator's child key names vary by
   node and across versions. So any value that is a list of dicts
   carrying a "type" is treated as a child group. The only place key
   names matter is telling an assignment's LHS from its RHS, and that
   has an explicit map plus a positional fallback.

2. Control dependencies are kept. A signal assigned inside
   ``if (en)`` depends on ``en`` even though ``en`` never appears on the
   RHS. Those are the bugs the loop actually hits -- a missing enable
   term, a reset polarity flip -- so they are edges, tagged ``control``.
"""

from __future__ import annotations

import json
import re
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Iterable

# ---------------------------------------------------------------- loc

_LOC_RE = re.compile(
    r"^(?P<file>[^,]*),(?P<l1>\d+):(?P<c1>\d+),(?P<l2>\d+):(?P<c2>\d+)$")


def parse_loc(loc: str | None) -> int | None:
    """Return the start line from a Verilator loc string.

    Format is ``file,startline:startcol,endline:endcol`` where *file* is
    an index into Verilator's file table, not a path.
    """
    if not loc:
        return None
    m = _LOC_RE.match(loc)
    if not m:
        return None
    line = int(m.group("l1"))
    return line or None


# ------------------------------------------------------------- node types

ASSIGN_TYPES = {
    "ASSIGN", "ASSIGNDLY", "ASSIGNW", "ASSIGNPOST", "ASSIGNPRE", "ASSIGNALIAS",
}

# Verilator orders assignment children RHS first, LHS second (confirmed
# against 5.052 output). Keep the key names we have seen and fall back to
# position for anything else.
_RHS_KEYS = ("op1p", "rhsp", "fromp")
_LHS_KEYS = ("op2p", "lhsp", "top")

# Children of these keys are conditions: variables read there control
# whatever is assigned in the node's other branches.
# condsp: a CASEITEM's match values (Verilator AstCaseItem op1). Missing it
# sent case items down the positional fallback, which treated a `default`
# arm's statements as its condition -- the bug line on Prob117 vanished.
_COND_KEYS = ("condp", "condsp", "exprp")

# Node types that are statements, never conditions. The positional
# fallback must not swallow a group containing any of these.
_STMT_TYPES = ASSIGN_TYPES | {"IF", "CASE", "BEGIN", "WHILE", "STMTEXPR"}

CONTROL_TYPES = {"IF", "CASE", "CASEITEM", "WHILE", "COND", "TERNARY"}

# A SENTREE lists the clock / async-reset edges of an ALWAYS block. Those
# are real dependencies but naming them in a repair hint is mostly noise,
# so they are collected separately and reported as the block's trigger.
SENSE_TYPES = {"SENTREE", "SENITEM"}


def _child_groups(node: dict) -> list[tuple[str, list[dict]]]:
    """Every (key, children) pair under *node*, in document order."""
    out: list[tuple[str, list[dict]]] = []
    for key, value in node.items():
        if isinstance(value, list) and value and all(
                isinstance(v, dict) and "type" in v for v in value):
            out.append((key, value))
        elif isinstance(value, dict) and "type" in value:
            out.append((key, [value]))
    return out


def _split_assign(node: dict) -> tuple[list[dict], list[dict], bool]:
    """Return (lhs_nodes, rhs_nodes, used_positional_fallback)."""
    groups = _child_groups(node)
    lhs: list[dict] = []
    rhs: list[dict] = []
    named = False
    for key, children in groups:
        if key in _LHS_KEYS:
            lhs.extend(children)
            named = True
        elif key in _RHS_KEYS:
            rhs.extend(children)
            named = True
    if named:
        return lhs, rhs, False
    # Positional fallback: last group is the LHS, everything before is RHS.
    if len(groups) >= 2:
        return (list(groups[-1][1]),
                [c for _, g in groups[:-1] for c in g], True)
    if groups:
        return list(groups[0][1]), [], True
    return [], [], False


def _varrefs(node: dict, acc: list[tuple[str, int | None]] | None = None
             ) -> list[tuple[str, int | None]]:
    """All (signal, line) VARREFs in the subtree rooted at *node*."""
    acc = [] if acc is None else acc
    if node.get("type") == "VARREF":
        name = node.get("name")
        if name:
            acc.append((name, parse_loc(node.get("loc"))))
    for _, children in _child_groups(node):
        for child in children:
            _varrefs(child, acc)
    return acc


# ------------------------------------------------------------------ graph

@dataclass(frozen=True)
class Edge:
    """``target`` depends on ``source``."""
    source: str
    kind: str          # "data" | "control"
    line: int | None   # line of the assignment, not of the reference


@dataclass
class DepGraph:
    module: str = ""
    deps: dict[str, set[Edge]] = field(default_factory=dict)
    lines: dict[str, set[int]] = field(default_factory=dict)
    ports: dict[str, str] = field(default_factory=dict)   # signal -> direction
    sequential: set[str] = field(default_factory=set)
    unknown: set[str] = field(default_factory=set)
    unread_inputs: set[str] = field(default_factory=set)
    triggers: set[str] = field(default_factory=set)   # clock / async reset
    locals_: set[str] = field(default_factory=set)    # regs, wires, params
    target_triggers: dict[str, set[str]] = field(default_factory=dict)
    positional: bool = False      # had to guess LHS/RHS from child order
    warning: str = ""             # set when the graph looks untrustworthy
    instances: int = 0            # submodule instantiations seen

    def add(self, target: str, edge: Edge) -> None:
        self.deps.setdefault(target, set()).add(edge)
        if edge.line:
            self.lines.setdefault(target, set()).add(edge.line)

    def signals(self) -> set[str]:
        out = set(self.deps)
        for edges in self.deps.values():
            out.update(e.source for e in edges)
        out.update(self.ports)
        return out

    def outputs(self) -> list[str]:
        return sorted(s for s, d in self.ports.items() if d.lower() == "output")


def _walk(node: dict, graph: DepGraph, conds: list[tuple[str, int | None]],
          clocked: bool, triggers: tuple[str, ...] = ()) -> None:
    ntype = node.get("type", "")

    if ntype == "VAR":
        name = node.get("name")
        direction = (node.get("direction") or node.get("dir") or "").strip()
        if not name:
            return
        # Verilator gives internal regs, wires and localparams a VAR with
        # direction "NONE". Those are not ports, and calling them ports put
        # "[none]" tags and localparam names into the cone report.
        if direction.lower() in ("input", "output", "inout"):
            graph.ports[name] = direction
        else:
            graph.locals_.add(name)
        return

    if ntype in ("CELL", "CELLINLINE"):
        # A submodule drives signals we cannot see into from here. Count it
        # so the orientation check does not mistake that for an empty graph,
        # and treat its port connections as mutual dependencies so the cone
        # does not simply stop at the instance boundary.
        graph.instances += 1
        line = parse_loc(node.get("loc"))
        refs = _varrefs(node)
        names = [n for n, _ in refs]
        for target in names:
            for src in names:
                if src != target:
                    graph.add(target, Edge(src, "instance", line))
        return

    if ntype == "ALWAYS":
        trigger: list[tuple[str, int | None]] = []
        body: list[dict] = []
        for _, children in _child_groups(node):
            for child in children:
                if child.get("type") in SENSE_TYPES:
                    trigger.extend(_varrefs(child))
                else:
                    body.append(child)
        # An edge-triggered block with a clock in its sensitivity list
        # produces state; mark its targets so the cone report can say so.
        is_clocked = bool(trigger)
        graph.triggers.update(n for n, _ in trigger)
        # Async reset appears in the sense list too, so it stays a control
        # dependency of everything in the block.
        # Line deliberately dropped: the edge matters, but the always header
        # is not a suspect line -- reset_notes() reports the reset style.
        async_conds = [(n, None) for n, _ in trigger
                       if re.search(r"(rst|reset|clr)", n, re.I)]
        for child in body:
            _walk(child, graph, conds + async_conds, clocked or is_clocked,
                  triggers=tuple(n for n, _ in trigger))
        return

    if ntype in ASSIGN_TYPES:
        lhs_nodes, rhs_nodes, positional = _split_assign(node)
        if positional:
            graph.positional = True
        line = parse_loc(node.get("loc"))
        targets = [n for n, _ in (
            ref for lhs in lhs_nodes for ref in _varrefs(lhs))]
        sources: list[tuple[str, int | None]] = []
        for rhs in rhs_nodes:
            sources.extend(_varrefs(rhs))
        for target in targets:
            if clocked:
                graph.sequential.add(target)
            if triggers:
                graph.target_triggers.setdefault(target, set()).update(
                    triggers)
            for src, _ in sources:
                graph.add(target, Edge(src, "data", line))
            for src, cond_line in conds:
                graph.add(target, Edge(src, "control", line))
                # The guarding condition is itself a suspect line. On
                # Prob117_circuit9 the bug was `if (q == 3'b111)` -- a
                # condition, not an assignment -- and it only surfaced
                # through display context until this was recorded.
                if cond_line:
                    graph.lines.setdefault(target, set()).add(cond_line)
            # A target with no sources at all still deserves a line, so the
            # cone can point at a constant assignment.
            if not sources and not conds and line:
                graph.lines.setdefault(target, set()).add(line)
                graph.deps.setdefault(target, set())
        return

    if ntype in CONTROL_TYPES:
        extra: list[tuple[str, int | None]] = []
        rest: list[dict] = []
        groups = _child_groups(node)
        found_cond = False
        for key, children in groups:
            if key in _COND_KEYS:
                found_cond = True
                for child in children:
                    extra.extend(_varrefs(child))
            else:
                rest.extend(children)
        # Positional fallback only when no condition key was present at
        # all -- not merely when the condition held no variables, which is
        # every constant case label. And never when the first group holds
        # statements: a `default:` arm has a body and no condition.
        if (not found_cond and len(groups) > 1
                and not any(c.get("type") in _STMT_TYPES
                            for c in groups[0][1])):
            for child in groups[0][1]:
                extra.extend(_varrefs(child))
            rest = [c for _, g in groups[1:] for c in g]
        for child in rest:
            _walk(child, graph, conds + extra, clocked, triggers)
        return

    for _, children in _child_groups(node):
        for child in children:
            _walk(child, graph, conds, clocked, triggers)


def build_graph(tree: dict, module: str | None = None) -> DepGraph:
    """Build a dependency graph from a parsed Verilator JSON AST."""
    modules: list[dict] = []

    def find_modules(node: dict) -> None:
        if node.get("type") == "MODULE":
            modules.append(node)
            return
        for _, children in _child_groups(node):
            for child in children:
                find_modules(child)

    find_modules(tree)
    if not modules:
        return DepGraph()

    chosen = modules[0]
    if module:
        for cand in modules:
            if cand.get("name") == module:
                chosen = cand
                break

    graph = DepGraph(module=chosen.get("name", ""))
    for _, children in _child_groups(chosen):
        for child in children:
            _walk(child, graph, [], False)

    # Anything read but never assigned and not a port is suspicious -- a
    # typo'd signal name, or a wire the model forgot to drive.
    assigned = set(graph.deps)
    for signal in graph.signals():
        if signal not in assigned and signal not in graph.ports:
            graph.unknown.add(signal)

    # The mirror of an undriven signal: an input the design declares and
    # then never reads. A backward cone cannot find a bug of *omission* --
    # it only contains signals the RTL actually references -- so a missing
    # enable term, a dropped reset condition or an ignored mode bit is
    # invisible to the walk. An unread input is the evidence that one
    # happened. Clock and async-reset lines arrive via the sensitivity
    # list rather than an edge, so they are not candidates.
    read_anywhere = {e.source for edges in graph.deps.values() for e in edges}
    for signal, direction in graph.ports.items():
        if direction.lower() != "input":
            continue
        if signal in read_anywhere or signal in graph.triggers:
            continue
        graph.unread_inputs.add(signal)

    graph.warning = check_orientation(graph)
    return graph


def check_orientation(graph: DepGraph) -> str:
    """Catch a silently inverted LHS/RHS split.

    If Verilator renames its assignment child keys, ``_split_assign``
    falls back to position and may get the orientation backwards. A
    backwards graph still walks and still prints a cone -- it just points
    at the wrong lines, which is worse than no cone at all. Two checks
    make that loud:

    * An input port appearing as an assignment *target* is impossible in
      legal RTL, so it means the split is inverted.
    * No declared output appearing as a target means outputs are never
      driven, which a design that lints clean cannot be.

    Returns "" when the graph looks sound.
    """
    if not graph.deps and not graph.ports:
        return ""
    # Instance edges are deliberately mutual (we cannot see which side of a
    # port connection drives), so they make inputs look assigned. Only real
    # assignment edges count as evidence of an inverted split.
    targets = {s for s, edges in graph.deps.items()
               if not edges or any(e.kind != "instance" for e in edges)}
    driven_inputs = sorted(
        s for s in targets
        if graph.ports.get(s, "").lower() == "input")
    if driven_inputs:
        return ("dependency graph looks inverted: input port(s) "
                + ", ".join(driven_inputs)
                + " appear as assignment targets"
                + (" (LHS/RHS inferred positionally)" if graph.positional
                   else ""))
    outputs = set(graph.outputs())
    if outputs and not graph.instances and not (outputs & targets):
        return ("dependency graph looks inverted or empty: no declared "
                "output is ever assigned"
                + (" (LHS/RHS inferred positionally)" if graph.positional
                   else ""))
    return ""


def load_graph(path: str, module: str | None = None) -> DepGraph:
    with open(path) as fh:
        return build_graph(json.load(fh), module=module)


# ------------------------------------------------------------------- cone

@dataclass
class ConeNode:
    signal: str
    depth: int
    kind: str                       # how it was reached
    lines: list[int] = field(default_factory=list)


@dataclass
class Cone:
    target: str
    nodes: list[ConeNode] = field(default_factory=list)
    lines: list[int] = field(default_factory=list)
    truncated: bool = False
    missing: bool = False           # target not in the graph at all

    def signals(self) -> list[str]:
        return [n.signal for n in self.nodes]


_BITSEL_RE = re.compile(r"\s*\[[^\]]*\]\s*$")


def resolve_signal(graph: DepGraph, name: str) -> str | None:
    """Map a testbench-reported name onto a graph signal.

    Hints name buses as ``out[3:0]`` while the AST carries the bare
    ``out``, and hierarchical paths arrive as ``tb.dut.q``. Both have to
    land on the same node or the cone silently reports "not found".
    """
    signals = graph.signals()
    if name in signals:
        return name
    bare = _BITSEL_RE.sub("", name)
    if bare in signals:
        return bare
    tail = bare.rsplit(".", 1)[-1]
    if tail in signals:
        return tail
    return None


def backward_cone(graph: DepGraph, target: str, max_depth: int = 4,
                  max_signals: int = 24) -> Cone:
    """Signals and lines that could have made *target* wrong."""
    resolved = resolve_signal(graph, target)
    if resolved is None:
        return Cone(target=target, missing=True)
    target = resolved

    cone = Cone(target=target)
    seen = {target}
    queue: deque[tuple[str, int, str]] = deque([(target, 0, "target")])
    lines: set[int] = set()

    while queue:
        signal, depth, kind = queue.popleft()
        own = sorted(graph.lines.get(signal, ()))
        cone.nodes.append(ConeNode(signal, depth, kind, own))
        lines.update(own)
        if depth >= max_depth:
            cone.truncated = True
            continue
        for edge in sorted(graph.deps.get(signal, ()),
                           key=lambda e: (e.kind, e.source, e.line or 0)):
            if edge.source in seen:
                continue
            if len(seen) >= max_signals:
                cone.truncated = True
                break
            seen.add(edge.source)
            queue.append((edge.source, depth + 1, edge.kind))

    cone.lines = sorted(lines)
    return cone


_RESET_RE = re.compile(r"(rst|reset|clr)", re.I)


def reset_notes(graph: DepGraph, signals: Iterable[str]) -> list[str]:
    """State whether each registered signal's reset is sync or async.

    A reset coded in the block body but absent from the sensitivity list
    is synchronous; present in both, asynchronous. Both are legal RTL, so
    this reports the style rather than calling it a bug -- but it is the
    only difference between some correct designs and some broken ones,
    and a backward cone alone cannot see it.
    """
    notes: list[str] = []
    for signal in signals:
        if signal not in graph.sequential:
            continue
        trig = graph.target_triggers.get(signal, set())
        resets = sorted({
            e.source for e in graph.deps.get(signal, ())
            if e.kind == "control" and _RESET_RE.search(e.source)})
        for rst in resets:
            style = "asynchronous" if rst in trig else "synchronous"
            notes.append(
                f"{signal}: reset '{rst}' is {style}"
                + (f" (sensitivity list: {', '.join(sorted(trig))})"
                   if trig else ""))
    return notes


def render_cone(cone: Cone, graph: DepGraph, source: str | None = None,
                context: int = 0, include_unread: bool = True) -> str:
    """Human- and model-readable cone summary for a repair prompt."""
    if cone.missing:
        return (f"signal '{cone.target}' does not appear in the elaborated "
                f"design -- it is never declared or never driven")

    out: list[str] = []
    if graph.warning:
        out.append(f"warning: {graph.warning}")
    out.append(f"backward cone for '{cone.target}':")
    by_depth: dict[int, list[ConeNode]] = {}
    for node in cone.nodes:
        by_depth.setdefault(node.depth, []).append(node)

    for depth in sorted(by_depth):
        for node in sorted(by_depth[depth], key=lambda n: n.signal):
            bits = []
            if node.signal in graph.ports:
                bits.append(graph.ports[node.signal].lower())
            elif node.signal in graph.locals_:
                bits.append("internal")
            if node.signal in graph.sequential:
                # The sensitivity list is the only thing distinguishing a
                # sync-reset design from an async-reset one, so it has to
                # be stated rather than left implicit. Reported as fact,
                # not judged: both are legal, and which is correct depends
                # on the spec.
                trig = sorted(graph.target_triggers.get(node.signal, ()))
                bits.append("registered on " + ", ".join(trig) if trig
                            else "registered")
            if node.signal in graph.unknown:
                bits.append("never driven")
            if node.kind == "control":
                bits.append("controls assignment")
            elif node.kind == "instance":
                bits.append("via submodule port")
            tag = f" [{', '.join(bits)}]" if bits else ""
            where = (" line " + ", ".join(str(l) for l in node.lines)
                     if node.lines else "")
            indent = "  " * (depth + 1)
            out.append(f"{indent}{node.signal}{tag}{where}")

    if cone.truncated:
        out.append("  (cone truncated)")

    notes = reset_notes(graph, cone.signals())
    if notes:
        out.append("")
        out.extend(notes)

    # Reported even though it is by definition outside the cone: this is
    # the one bug class the walk cannot reach.
    if include_unread and graph.unread_inputs:
        out.append("")
        out.append("declared but never read: "
                   + ", ".join(sorted(graph.unread_inputs))
                   + " -- the design ignores "
                   + ("this input" if len(graph.unread_inputs) == 1
                      else "these inputs")
                   + " entirely, so required behaviour is missing rather "
                     "than wrong")

    if source and cone.lines:
        out.append("")
        out.append("suspect lines:")
        out.extend(annotate_source(source, cone.lines, context=context))
    return "\n".join(out)


@dataclass
class MismatchHint:
    signal: str
    count: int
    first_time: int | None = None


# VerilogEval v2 testbenches print, per failing output:
#   Hint: Output 'q' has 12 mismatches. First mismatch occurred at time 25.
# That names the signal to walk back from and the time to window the VCD on,
# which is strictly more than "Mismatches: N in M samples" tells us.
#
# The "Hint:" prefix is NOT required: parse_verilogeval_sim strips it before
# the text reaches here, so a diagnostic message reads
#   Output 'q' has 152 mismatches. First mismatch occurred at time 100.
# Requiring the prefix made this silently match nothing on real feedback.
_HINT_RE = re.compile(
    r"(?:Hint:\s*)?Output\s*'(?P<sig>[^']+)'\s*has\s*(?P<n>\d+)\s*mismatch",
    re.I)
_HINT_TIME_RE = re.compile(
    r"First\s+mismatch\s+occurred\s+at\s+time\s+(?P<t>\d+)", re.I)


def parse_mismatch_hints(output: str) -> list[MismatchHint]:
    """Failing output signals named by a VerilogEval testbench.

    Returns hints worst-first, so the cone is built for the signal with
    the most mismatches when only one can be afforded in the prompt.
    """
    hints: list[MismatchHint] = []
    for line in (output or "").splitlines():
        m = _HINT_RE.search(line)
        if not m:
            continue
        t = _HINT_TIME_RE.search(line)
        hints.append(MismatchHint(
            signal=m.group("sig"),
            count=int(m.group("n")),
            first_time=int(t.group("t")) if t else None,
        ))
    hints.sort(key=lambda h: (-h.count, h.signal))
    return hints


def annotate_source(source: str, lines: Iterable[int], context: int = 0
                    ) -> list[str]:
    """Render the numbered source lines in the cone."""
    text = source.splitlines()
    wanted: set[int] = set()
    for line in lines:
        for n in range(line - context, line + context + 1):
            if 1 <= n <= len(text):
                wanted.add(n)
    out: list[str] = []
    previous: int | None = None
    for n in sorted(wanted):
        if previous is not None and n > previous + 1:
            out.append("  ...")
        out.append(f"  {n:4d} | {text[n - 1]}")
        previous = n
    return out


# --------------------------------------------------------------- hint

#: Cap on cone text injected into a repair prompt. The budget matters:
#: gpt-oss-20b truncated at 2048 output tokens, and prompt tokens compete
#: with the model's room to answer.
MAX_HINT_LINES = 60


def cone_hint(rtl: str, top: str | None, sim_output: str = "",
              max_depth: int = 3, context: int = 1,
              max_targets: int = 2) -> str:
    """Line-level evidence for a repair prompt, or "" if unavailable.

    Never raises and never fails a design: a missing Verilator, an AST
    that will not parse, or a graph that fails its own orientation check
    all return "". The repair then proceeds on tool diagnostics alone,
    exactly as it did before cones existed.

    Call this only after a *functional* mismatch. A design that did not
    compile has no elaborated AST to walk, and a lint or compile error
    already carries its own line number.
    """
    try:
        from .astdump import dump_ast

        ast = dump_ast(rtl, top=top)
        if not ast.available or not ast.tree:
            return ""
        graph = build_graph(ast.tree, module=top)
        if graph.warning:
            # An inverted or empty graph points at the wrong lines, which
            # is worse than saying nothing.
            return ""

        targets = [h.signal for h in parse_mismatch_hints(sim_output)]
        times = {h.signal: h.first_time
                 for h in parse_mismatch_hints(sim_output)}
        if not targets:
            outs = graph.outputs()
            # Without a named failing output, only a single-output design
            # gives a cone narrow enough to be worth the tokens.
            if len(outs) != 1:
                return ""
            targets = outs

        blocks: list[str] = []
        for name in targets[:max_targets]:
            cone = backward_cone(graph, name, max_depth=max_depth)
            if cone.missing:
                continue
            head = f"'{name}' is wrong"
            when = times.get(name)
            if when is not None:
                head += f", first at simulation time {when}"
            # Unread inputs are a property of the design, not of one
            # output: reported once below, not once per cone (Prob134
            # repeated it per target and hit the line cap).
            blocks.append(head + ".\n" + render_cone(
                cone, graph, source=rtl, context=context,
                include_unread=False))

        if not blocks and not graph.unread_inputs:
            return ""
        if graph.unread_inputs:
            names = sorted(graph.unread_inputs)
            # First, not last: on Prob134 this was the correct diagnosis
            # and the line cap could truncate it away.
            blocks.insert(0,
                "declared but never read: " + ", ".join(names)
                + " -- the design ignores "
                + ("this input" if len(names) == 1 else "these inputs")
                + " entirely, so required behaviour is missing rather "
                  "than wrong")

        text = "\n\n".join(blocks).splitlines()
        if len(text) > MAX_HINT_LINES:
            text = text[:MAX_HINT_LINES] + ["  (evidence truncated)"]
        return "\n".join(text)
    except Exception:
        return ""
