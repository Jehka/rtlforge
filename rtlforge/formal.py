"""Find where two RTL designs disagree, with a formal tool, not a testbench.

The stage-1 mechanism of the proof-carried-RTL vision: given two candidate
implementations of the same spec, either prove they agree up to a depth,
or produce the concrete input sequence after which they differ. That
sequence becomes a narrow question for the specification.

Built on Yosys alone (`miter` + the built-in `sat` solver), so it runs
anywhere the harness already runs: no SymbiYosys, no external solver, no
commercial tool.

Two checks, because they answer different questions (validated on
VerilogEval Prob053, 2026-11-09):

  logic   both designs start with every register at 0. Only differences
          in the logic show. A design missing an `initial` value is still
          *logically* identical to one that has it.
  init    registers start from their source-declared initial value, or X
          where there is none -- what a simulator and the VerilogEval
          testbench see. X in the reference is don't-care, matching the
          testbench's comparison.

Verdict:
  equivalent   no difference within `depth` steps in either check
  init-only    same logic, different power-up behaviour
  logic        the logic differs; counterexample from the logic check
  error        could not be compared (interface mismatch, unsupported
               construct, tool failure)

"equivalent" is bounded: it means no difference within `depth` steps.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field

YOSYS = os.environ.get("YOSYS", "yosys")

CLOCK_NAMES = {"clk", "clock", "clk_i", "i_clk"}

_INPUT_DECL = re.compile(r"\binput\b([^;)]*)")
_RESET_NAME = re.compile(r"^(a?reset|a?rst)(_?n|_b)?$|^(a?reset|a?rst)\w*$", re.I)
# CVDP names resets i_rst_n, presetn, axi_aresetn, i_reset_bar, hreset_n,
# w_rst, ... and clocks pclk, clk_in, axi_aclk, hclk, w_clk, clk1. The
# VerilogEval-era rules above missed them, so no reset was applied and the
# designs' power-up states were compared (2026-11-14 CVDP audit).
_RESET_TOKEN = re.compile(r"^[ahpsm]?(rst|reset)(n|b)?$", re.I)
_CLOCK_TOKEN = re.compile(r"^[a-z]?(clk|clock)\d*$", re.I)


def _tokens(name: str) -> list[str]:
    return [t for t in name.split("_") if t]


def is_clock_name(name: str) -> bool:
    return name in CLOCK_NAMES or any(_CLOCK_TOKEN.match(t) for t in _tokens(name))


def _reset_like(code: str, name: str) -> bool:
    if _RESET_NAME.match(name):
        return True
    toks = _tokens(name)
    if not any(_RESET_TOKEN.match(t) for t in toks):
        return False
    if any(t.lower() in ("val", "value", "data", "count", "cnt") for t in toks):
        return False                       # e.g. preset_value: a data input
    # a reset is used: on an edge, or tested in a condition
    n = re.escape(name)
    return bool(re.search(rf"\b(?:pos|neg)edge\s+{n}\b", code)
                or re.search(rf"\bif\s*\([^;]*\b{n}\b", code))


def edge_inputs(rtl: str) -> set[str]:
    """Inputs used as posedge/negedge in a sensitivity list."""
    code = re.sub(r"//[^\n]*|/\*.*?\*/", "", rtl, flags=re.S)
    return set(re.findall(r"\b(?:pos|neg)edge\s+(\w+)", code))


def reset_inputs(rtl: str) -> dict[str, int]:
    """Reset-like input ports and their active level.

    A testbench asserts reset before checking anything. Without the same
    assumption, formal search starts in states no real run reaches (e.g.
    all-zero in a one-hot FSM) and reports differences no testbench could
    ever see.
    """
    code = re.sub(r"//[^\n]*|/\*.*?\*/", "", rtl, flags=re.S)
    out: dict[str, int] = {}
    for decl in _INPUT_DECL.findall(code):
        decl = re.sub(r"\[[^\]]*\]", " ", decl)
        for name in re.findall(r"\b([A-Za-z_]\w*)\b", decl):
            if name in ("wire", "reg", "logic", "signed", "unsigned"):
                continue
            if _reset_like(code, name):
                level = _usage_level(code, name)
                if level is None:
                    lname = name.lower()
                    last = _tokens(lname)[-1]
                    active_low = (lname.endswith(("_n", "_b", "_bar", "_l"))
                                  or re.fullmatch(r"[ahpsm]?(rst|reset)(n|b)", last) is not None
                                  or lname in ("resetn", "aresetn", "rstn", "arstn"))
                    level = 0 if active_low else 1
                out[name] = level
    return out


def _usage_level(code: str, name: str) -> int | None:
    """Active level from how the RTL uses the reset, not its name.

    CVDP designs name active-low resets plain `reset` (negedge reset,
    if (!reset)); the name alone would assert the wrong level. Edge
    sensitivity decides first, then the polarity of the conditions that
    test it; None when the code says nothing either way.
    """
    n = re.escape(name)
    lo = len(re.findall(rf"\bnegedge\s+{n}\b", code))
    hi = len(re.findall(rf"\bposedge\s+{n}\b", code))
    if lo != hi:
        return 0 if lo > hi else 1
    zero, one = r"(?:1'[bhd]0|0)(?![\w'])", r"(?:1'[bhd]1|1)(?![\w'])"
    lo = len(re.findall(rf"\(\s*[!~]\s*{n}\s*\)", code)) + len(re.findall(
        rf"\b{n}\s*==\s*{zero}|\b{n}\s*!=\s*{one}", code))
    hi = len(re.findall(rf"\bif\s*\(\s*{n}\s*\)", code)) + len(re.findall(
        rf"\b{n}\s*==\s*{one}|\b{n}\s*!=\s*{zero}", code))
    if lo != hi:
        return 0 if lo > hi else 1
    return None


@dataclass
class Step:
    t: int
    inputs: dict[str, str] = field(default_factory=dict)
    gold: dict[str, str] = field(default_factory=dict)
    gate: dict[str, str] = field(default_factory=dict)
    differ: bool = False              # from the output values themselves
    trigger: bool = False             # the miter's own flag (can lag a step)


@dataclass
class Check:
    ran: bool = False
    proved: bool = False              # no difference found within depth
    steps: list[Step] = field(default_factory=list)

    def first_difference(self) -> Step | None:
        return next((s for s in self.steps if s.differ), None)

    def trace_until_difference(self) -> list[Step]:
        out = []
        for s in self.steps:
            out.append(s)
            if s.differ:
                break
        return out


@dataclass
class Distinction:
    verdict: str                       # equivalent | init-only | logic | error
    depth: int
    resets: dict = field(default_factory=dict)   # asserted in cycle 1
    logic: Check = field(default_factory=Check)
    init: Check = field(default_factory=Check)
    error: str = ""
    clocks: set = field(default_factory=set)     # edge inputs that are not resets

    @property
    def counterexample(self) -> Check | None:
        if self.verdict == "logic":
            return self.logic
        if self.verdict == "init-only":
            return self.init
        return None

    def differing_outputs(self) -> list[str]:
        cx = self.counterexample
        s = cx.first_difference() if cx else None
        if not s:
            return []
        return sorted(k for k in s.gold
                      if _differs(s.gold[k], s.gate.get(k, "")))


def _differs(gold: str, gate: str) -> bool:
    return "x" not in gold and gold != gate


# ------------------------------------------------------------- parsing

_ROW = re.compile(r"^\s*(init|\d+)\s+\\(\S+)\s+(\S+)\s+(\S+)\s+([01xz]+)\s*$")


def _value(binary: str) -> str:
    if re.search(r"[xz]", binary):
        return "x" if len(binary) == 1 else binary
    return str(int(binary, 2))


def _parse_check(section: str, skip: int = 0) -> Check:
    chk = Check(ran="SAT proof finished" in section)
    chk.proved = "no model found" in section
    steps: dict[int, Step] = {}
    for line in section.splitlines():
        m = _ROW.match(line)
        if not m or m.group(1) == "init":
            continue
        t, name, binary = int(m.group(1)), m.group(2), m.group(5)
        st = steps.setdefault(t, Step(t))
        if name == "trigger":
            st.trigger = binary == "1"
        elif name.startswith("in_"):
            st.inputs[name[3:]] = _value(binary)
        elif name.startswith("gold_"):
            st.gold[name[5:]] = _value(binary)
        elif name.startswith("gate_"):
            st.gate[name[5:]] = _value(binary)
    chk.steps = [steps[t] for t in sorted(steps)]
    # The miter's trigger fires one step after the outputs diverge when
    # -ignore_gold_x is set (observed on Prob117, Yosys 0.70). Compare the
    # outputs directly instead; gold X stays don't-care.
    for st in chk.steps:
        st.differ = st.t > skip and any(
            _differs(v, st.gate.get(k, "")) for k, v in st.gold.items())
    if not chk.proved and not any(st.differ for st in chk.steps):
        for st in chk.steps:          # should not happen; never lose a FAIL
            st.differ = st.trigger and st.t > skip
    return chk


def _section(log: str, start: str, end: str) -> str:
    m = re.search(rf"^{start}\s*$(.*?)^{end}\s*$", log, re.M | re.S)
    return m.group(1) if m else ""


# ---------------------------------------------------------------- main

#: Depths tried, in order, when looking for the shortest counterexample.
_DEEPEN = (1, 2, 3, 4, 5, 6, 7, 8, 10, 12, 14, 16, 18, 20, 24, 28, 32, 40)


def distinguish(rtl_gold: str, rtl_gate: str, top_gold: str = "TopModule",
                top_gate: str = "TopModule", depth: int = 20,
                timeout: int = 120, assume_reset: bool = True,
                shortest: bool = True) -> Distinction:
    """Compare two designs; `gold` is the one whose X counts as don't-care.

    With assume_reset, every reset-like input is asserted in cycle 1, as a
    testbench would, so both designs start from their reset state.

    With shortest, a found difference is re-searched at increasing depths
    and the first (shortest) counterexample kept. SAT otherwise returns any
    witness within the bound -- 14 cycles on Prob117 where 6 suffice, and a
    longer trace is a harder question to put to the specification.
    """
    resets = reset_inputs(rtl_gold) if assume_reset else {}
    reset_args = " ".join(f"-set-at 1 in_{n} {v}" for n, v in resets.items())
    # The reset cycle itself still shows the power-up state, which no spec
    # defines (M1a audit: 8 of 37 "errors" were differences in cycle 1 of
    # designs whose reset only takes effect at the end of that cycle).
    skip = 1 if resets else 0
    if skip:
        reset_args += f" -prove-skip {skip}"
    work = tempfile.mkdtemp(prefix="rtlforge-miter-")
    try:
        with open(os.path.join(work, "gold.v"), "w") as fh:
            fh.write(rtl_gold)
        with open(os.path.join(work, "gate.v"), "w") as fh:
            fh.write(rtl_gate)
        prep = "proc; memory -nomap; memory_map; flatten; async2sync; opt_clean"
        show = "-show-inputs -show-outputs"
        logic_args = f"-enable_undef -set-init-zero -set-def-inputs {reset_args}"
        init_args = f"-enable_undef -set-init-undef -set-def-inputs {reset_args}"
        prefix = (
            # Each design read into its own namespace: VerilogEval answers
            # often define helper modules with the same names.
            f"read_verilog -sv gold.v; hierarchy -top {top_gold}; {prep}; "
            f"rename {top_gold} gold; design -stash A; "
            f"read_verilog -sv gate.v; hierarchy -top {top_gate}; {prep}; "
            f"rename {top_gate} gate; design -stash B; "
            f"design -copy-from A -as gold gold; design -copy-from B -as gate gate; "
            f"miter -equiv -flatten -make_outputs -ignore_gold_x gold gate miter; "
            f"hierarchy -top miter; ")
        # Both checks model X explicitly so reference don't-cares show as x
        # and are skipped; one extra step absorbs the trigger lag.
        script = (prefix
                  + f"log @@LOGIC; sat -seq {depth + 1} {logic_args} "
                    f"-prove trigger 0 {show} miter; "
                  + f"log @@INIT; sat -seq {depth + 1} {init_args} "
                    f"-prove trigger 0 {show} miter; log @@END")

        def run(text: str) -> tuple[str | None, str]:
            try:
                p = subprocess.run([YOSYS, "-p", text], cwd=work,
                                   capture_output=True, text=True,
                                   timeout=timeout)
            except FileNotFoundError:
                return None, f"{YOSYS} not found"
            except subprocess.TimeoutExpired:
                return None, f"timeout after {timeout}s"
            out = p.stdout + p.stderr
            if p.returncode != 0 or "ERROR" in out:
                line = next((l for l in out.splitlines() if "ERROR" in l), "")
                return None, line.strip()[:240] or f"exit {p.returncode}"
            return out, ""

        log, err = run(script)
        if log is None:
            return Distinction("error", depth, resets, error=err)

        logic = _parse_check(_section(log, "@@LOGIC", "@@INIT"), skip)
        init = _parse_check(_section(log, "@@INIT", "@@END"), skip)
        if not (logic.ran and init.ran):
            return Distinction("error", depth, resets, logic, init,
                               error="SAT did not complete")
        if not logic.proved:
            verdict = "logic"
        elif not init.proved:
            verdict = "init-only"
        else:
            verdict = "equivalent"

        if shortest and verdict != "equivalent":
            args = logic_args if verdict == "logic" else init_args
            depths = [k for k in _DEEPEN if skip < k <= depth] + [depth]
            deepen = prefix + "".join(
                f"log @@D{k}; sat -seq {k + 1} {args} -prove trigger 0 "
                f"{show} miter; " for k in dict.fromkeys(depths)) + "log @@DEND"
            dlog, _ = run(deepen)
            if dlog:
                marks = list(dict.fromkeys(depths))
                for i, k in enumerate(marks):
                    nxt = f"@@D{marks[i + 1]}" if i + 1 < len(marks) else "@@DEND"
                    chk = _parse_check(_section(dlog, f"@@D{k}", nxt), skip)
                    if chk.ran and not chk.proved and chk.first_difference():
                        if verdict == "logic":
                            logic = chk
                        else:
                            init = chk
                        break
        return Distinction(verdict, depth, resets, logic, init,
                           clocks=edge_inputs(rtl_gold) - set(resets))
    finally:
        shutil.rmtree(work, ignore_errors=True)


def prove_equivalent(rtl_gold: str, rtl_gate: str, top_gold: str = "TopModule",
                     top_gate: str = "TopModule", max_steps: int = 24,
                     timeout: int = 180, assume_reset: bool = True) -> str:
    """Unbounded equivalence by temporal induction on the output miter.

    distinguish() is a bounded check: "no difference in 20 cycles". CVDP
    designs have deep state (a 24-hour clock first differs at the hour
    rollover, tens of thousands of cycles in), so bounded "equivalent"
    merged designs the testbench told apart (10 clusters on CVDP cid003,
    2026-11-13). Clustering must use proven equivalence only, as NoTB does.

    Returns "proven", "unproven" (induction did not close within max_steps,
    or the base case found a difference), or "error".
    """
    resets = reset_inputs(rtl_gold) if assume_reset else {}
    reset_args = " ".join(f"-set-at 1 in_{n} {v}" for n, v in resets.items())
    # -prove-skip is not allowed with -tempinduct; -seq N leaves the first
    # N-1 steps (here the reset cycle) unchecked instead.
    seq = 2 if resets else 1
    work = tempfile.mkdtemp(prefix="rtlforge-induct-")
    try:
        with open(os.path.join(work, "gold.v"), "w") as fh:
            fh.write(rtl_gold)
        with open(os.path.join(work, "gate.v"), "w") as fh:
            fh.write(rtl_gate)
        prep = "proc; memory -nomap; memory_map; flatten; async2sync; opt_clean"
        script = (
            f"read_verilog -sv gold.v; hierarchy -top {top_gold}; {prep}; "
            f"rename {top_gold} gold; design -stash A; "
            f"read_verilog -sv gate.v; hierarchy -top {top_gate}; {prep}; "
            f"rename {top_gate} gate; design -stash B; "
            f"design -copy-from A -as gold gold; design -copy-from B -as gate gate; "
            f"miter -equiv -flatten -make_outputs -ignore_gold_x gold gate miter; "
            f"hierarchy -top miter; "
            f"sat -tempinduct -prove trigger 0 -seq {seq} -maxsteps {max_steps} "
            f"-set-init-zero -enable_undef -set-def-inputs {reset_args} miter")
        try:
            p = subprocess.run([YOSYS, "-p", script], cwd=work, capture_output=True,
                               text=True, timeout=timeout)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return "error"
        out = p.stdout + p.stderr
        if p.returncode != 0 or "ERROR" in out:
            return "error"
        if re.search(r"Induction step proven|SUCCESS!", out):
            return "proven"
        return "unproven"
    finally:
        shutil.rmtree(work, ignore_errors=True)


# -------------------------------------------------------------- render
# Question wording, revised after the M1a audit (2026-11-10). The first
# version opened every question with "a registered output reflects the
# inputs of earlier cycles"; the model applied that to purely
# combinational designs (Prob070, 083, 102, 092, 116) and used it to argue
# that a reset raised in the final cycle had already acted (Prob066, 068,
# 117). Clockless designs are now asked as plain input -> output questions,
# and sequential ones state the clock-edge timing exactly.

_TIMING = ("Each row is one clock cycle. Inputs are held steady for the whole "
           "cycle, and there is one rising clock edge at the end of every "
           "cycle. The outputs listed for a cycle are the values during that "
           "cycle: they include the effect of every clock edge up to the end "
           "of the previous cycle, but not the edge at the end of this cycle.")


def _fmt(v: str) -> str:
    if v == "x":
        return "x (undefined)"
    if v.isdigit() and int(v) > 255:
        return hex(int(v))            # 31-digit decimals were unreadable
    return v


def _bit_note(a: str, b: str) -> str:
    """For wide values, name the bits that differ."""
    if not (a.isdigit() and b.isdigit()) or max(int(a), int(b)) <= 255:
        return ""
    diff = int(a) ^ int(b)
    bits = [i for i in range(diff.bit_length()) if diff >> i & 1]
    shown = ", ".join(str(i) for i in bits[:12]) + (" ..." if len(bits) > 12 else "")
    return f" (they differ in bit{'s' if len(bits) > 1 else ''} {shown})"


def _describe(d: Distinction, first: dict, second: dict) -> list[str]:
    """Shared body: the scenario and both candidates' outputs.

    `first` / `second` are the output dicts shown as candidate A / B.
    """
    cx = d.counterexample
    trace = cx.trace_until_difference()
    last = trace[-1]
    outs = d.differing_outputs()
    all_inputs = sorted({k for s in trace for k in s.inputs})
    isclk = lambda k: k in d.clocks or is_clock_name(k)
    clocked = any(isclk(k) for k in all_inputs) or bool(d.clocks)
    inputs = [k for k in all_inputs if not isclk(k)]
    row = lambda s: ", ".join(f"{k}={_fmt(s.inputs.get(k, '?'))}" for k in inputs) or "(no inputs)"

    lines = ["Two implementations of this specification behave differently."]
    if not clocked:
        lines.append(f"With inputs {row(last)}:")
    else:
        if d.verdict == "init-only":
            lines.append("They have the same logic but behave differently from power-up.")
        if d.resets:
            names = ", ".join(f"{n}={v}" for n, v in d.resets.items())
            lines.append(f"Reset ({names}) is asserted in cycle 1.")
        lines.append(_TIMING)
        lines += [f"  cycle {s.t}: {row(s)}" for s in trace]
        lines.append(f"During cycle {last.t}:")
    for o in outs:
        a, b = first.get(o, "?"), second.get(o, "?")
        lines.append(f"  candidate A outputs {o}={_fmt(a)}")
        lines.append(f"  candidate B outputs {o}={_fmt(b)}{_bit_note(a, b)}")
    return lines


def render_question(d: Distinction, label_a: str = "A",
                    label_b: str = "B") -> str:
    """The disagreement as a question the specification can answer.

    `label_a` names the gold design, `label_b` the gate design.
    """
    cx = d.counterexample
    if cx is None:
        return ""
    last = cx.trace_until_difference()[-1]
    body = _describe(d, last.gold, last.gate)
    body = [l.replace("candidate A", f"candidate {label_a}")
             .replace("candidate B", f"candidate {label_b}") for l in body]
    outs = d.differing_outputs()
    body.append("According to the specification, what must "
                + (", ".join(outs) or "the output") + " be?")
    return "\n".join(body)


def render_ab_question(d: Distinction, gold_is_a: bool) -> tuple[str, str]:
    """A/B form for evaluation: (question, label of the gold design).

    Candidate A is always described first, so the order of mention never
    reveals which candidate is the reference; `gold_is_a` decides which
    side the reference sits on, and the caller randomises it.
    """
    cx = d.counterexample
    if cx is None:
        return "", ""
    last = cx.trace_until_difference()[-1]
    first, second = (last.gold, last.gate) if gold_is_a else (last.gate, last.gold)
    return "\n".join(_describe(d, first, second)), ("A" if gold_is_a else "B")
