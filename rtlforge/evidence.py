"""Turn raw tool output into evidence a repair prompt can act on.

Built from the 32-run diagnostic of 2026-11-07 (qwen2.5-coder:32b,
16-problem discriminating set). Every function here answers a failure
seen in those logs:

* prioritise_diagnostics -- lint feedback listed 12 warnings and then
  "(further diagnostics omitted)", cutting off the actual error. Five-
  attempt lint loops on Prob151/Prob156 never saw what failed them.
  Errors now come first, and the harness-caused DECLFILENAME warning
  (we name the file design.v) is dropped.

* trace_summary -- the waveform trace reached every prompt, but the model
  read it poorly. Three deterministic readings of the same table:
    - X state: the design outputs x where the reference is defined
      (Prob053, Prob074 -- missing initialisation, masking other bugs)
    - first divergence: the last cycle both agreed, then what each did
      (Prob117 -- reference 6->0, design 6->7, stated as a sentence)
    - counterexamples: input -> expected output pairs, emitted only when
      the reference behaves as a function of the shown inputs
      (Prob113 -- the truth table was in the window, unused)

* repair_evidence -- trace summary + backward cone, with an explicit
  note that unchanged code fails again: 15 of 20 failed runs ended with
  the model regenerating identical RTL.

RTLFORGE_EVIDENCE selects which parts run, for ablation:
  1 / on   everything                      0 / off  nothing
  cone     AST backtracking only (cone + unread inputs)
  trace,noeffect   trace readings + no-effect note, no AST
Any comma-separated subset of {trace, cone, noeffect} works.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

# ----------------------------------------------------------- diagnostics

#: Warning codes caused by the harness, not by the design.
NOISE_CODES = {"DECLFILENAME"}
_NOISE_MESSAGES = ("does not match MODULE name",)


def _is_noise(diag) -> bool:
    code = (getattr(diag, "code", None) or "").upper()
    msg = getattr(diag, "message", None) or ""
    return code in NOISE_CODES or any(m in msg for m in _NOISE_MESSAGES)


def _is_error(diag) -> bool:
    return str(getattr(diag, "severity", "")).lower().startswith("err")


def prioritise_diagnostics(diags):
    """Errors first, harness noise removed, order otherwise preserved.

    Never returns an empty list for a non-empty input: if everything was
    noise, the original list goes through so a failure is never silent.
    """
    diags = list(diags or [])
    kept = [d for d in diags if not _is_noise(d)]
    if not kept:
        return diags
    return sorted(kept, key=lambda d: 0 if _is_error(d) else 1)


# ---------------------------------------------------------------- trace

@dataclass
class Trace:
    columns: list[str]
    rows: list[dict[str, str]] = field(default_factory=list)

    def signals(self) -> list[str]:
        """Output names that have both a _ref and a _dut column."""
        out = []
        for col in self.columns:
            if col.endswith("_ref"):
                base = col[:-4]
                if f"{base}_dut" in self.columns:
                    out.append(base)
        return out

    def inputs(self) -> list[str]:
        skip = {"time", "clk", "tb_mismatch"}
        sigs = set(self.signals())
        return [c for c in self.columns
                if c not in skip and not c.endswith(("_ref", "_dut"))
                and c not in sigs]


_MARKER = re.compile(r"<--.*$")


def parse_trace(feedback: str) -> Trace | None:
    """Parse the signal-trace table embedded in the feedback text."""
    lines = (feedback or "").splitlines()
    for i, line in enumerate(lines):
        if "|" in line and "_ref" in line and "_dut" in line:
            columns = [c.strip() for c in line.split("|")]
            trace = Trace(columns)
            for row in lines[i + 1:]:
                stripped = row.strip()
                if not stripped or stripped.startswith("```"):
                    if trace.rows:
                        break
                    continue
                if set(stripped) <= {"-", "+", "|", " "}:
                    continue
                cells = [c.strip() for c in _MARKER.sub("", row).split("|")]
                if len(cells) != len(columns) or not cells[0].isdigit():
                    if trace.rows:
                        break
                    continue
                trace.rows.append(dict(zip(columns, cells)))
            return trace if trace.rows and trace.signals() else None
    return None


def _unknown(value: str) -> bool:
    return bool(re.search(r"[xXzZ]", value or ""))


def _fmt_inputs(trace: Trace, row: dict[str, str]) -> str:
    names = trace.inputs()
    return ", ".join(f"{n}={row[n]}" for n in names) if names else ""


def x_state_notes(trace: Trace) -> list[str]:
    notes = []
    for sig in trace.signals():
        for row in trace.rows:
            ref, dut = row[f"{sig}_ref"], row[f"{sig}_dut"]
            if _unknown(dut) and not _unknown(ref):
                notes.append(
                    f"'{sig}' is X (unknown) in your design at t={row['time']}"
                    f", where the reference is {ref}. A register with no "
                    "reset or initial value starts as X in simulation, and "
                    "X keeps propagating through any logic that feeds back "
                    "on it. If the specification implies a starting value, "
                    "initialise to it.")
                break
    return notes


def first_divergence(trace: Trace) -> str:
    sigs = trace.signals()

    def agrees(row: dict[str, str]) -> bool:
        return all(row[f"{s}_ref"] == row[f"{s}_dut"] for s in sigs)

    for i, row in enumerate(trace.rows):
        wrong = [s for s in sigs
                 if row[f"{s}_ref"] != row[f"{s}_dut"]
                 and not _unknown(row[f"{s}_ref"])
                 and not _unknown(row[f"{s}_dut"])]
        if not wrong:
            continue
        prev = next((r for r in reversed(trace.rows[:i]) if agrees(r)
                     and not any(_unknown(r[f"{s}_ref"]) for s in sigs)),
                    None)
        diffs = "; ".join(
            f"reference {s}={row[f'{s}_ref']}, yours {s}={row[f'{s}_dut']}"
            for s in wrong)
        inputs_now = _fmt_inputs(trace, row)
        text = (f"First disagreement at t={row['time']}"
                + (f" (inputs {inputs_now})" if inputs_now else "")
                + f": {diffs}.")
        if prev is not None:
            same = ", ".join(f"{s}={prev[f'{s}_ref']}" for s in wrong)
            inputs_prev = _fmt_inputs(trace, prev)
            text = (f"Last agreement at t={prev['time']}: {same} in both"
                    + (f" (inputs {inputs_prev})" if inputs_prev else "")
                    + ". " + text)
        return text
    return ""


def counterexamples(trace: Trace, limit: int = 8) -> list[str]:
    """Input -> expected pairs, only if the reference looks combinational.

    For a sequential design the output depends on state as well as the
    inputs shown, and an input -> output list would mislead. So the list
    is emitted only when every repeated input tuple in the window maps to
    the same reference output.
    """
    names = trace.inputs()
    sigs = trace.signals()
    if not names:
        return []
    seen: dict[tuple, tuple] = {}
    for row in trace.rows:
        key = tuple(row[n] for n in names)
        if any(_unknown(v) for v in key):
            continue
        val = tuple(row[f"{s}_ref"] for s in sigs)
        if key in seen and seen[key] != val:
            return []                              # not a function: skip
        seen[key] = val

    out, done = [], set()
    for row in trace.rows:
        key = tuple(row[n] for n in names)
        if key in done or any(_unknown(v) for v in key):
            continue
        wrong = [s for s in sigs if row[f"{s}_ref"] != row[f"{s}_dut"]
                 and not _unknown(row[f"{s}_ref"])]
        if not wrong:
            continue
        done.add(key)
        lhs = ", ".join(f"{n}={v}" for n, v in zip(names, key))
        rhs = ", ".join(f"{s} should be {row[f'{s}_ref']} "
                        f"(yours {row[f'{s}_dut']})" for s in wrong)
        out.append(f"{lhs}: {rhs}")
        if len(out) >= limit:
            break
    return out


def trace_summary(feedback: str) -> str:
    trace = parse_trace(feedback)
    if trace is None:
        return ""
    parts: list[str] = []
    parts.extend(x_state_notes(trace))
    div = first_divergence(trace)
    if div:
        parts.append(div)
    cex = counterexamples(trace)
    if cex:
        parts.append("Input combinations your design gets wrong:\n"
                     + "\n".join(f"  {c}" for c in cex))
    return "\n".join(parts)


# ------------------------------------------------------- no-op repairs
# Evidence-on run, 2026-11-07: on Prob117 the model got "reference q=0,
# yours q=7", rewrote the if/else into an equivalent form, and failed with
# the identical 192 mismatches. Same on Prob134, 127, 070, 151. Saying the
# change was a no-op is cheap and the loop already has both feedbacks.

_TOTAL_RE = re.compile(r"(\d+) of (\d+) samples did not match")
_PER_OUT_RE = re.compile(r"Output\s*'([^']+)'\s*has\s*(\d+)\s*mismatch")


def _behaviour_signature(feedback: str) -> tuple | None:
    total = _TOTAL_RE.search(feedback or "")
    if not total:
        return None
    per_out = tuple(sorted(_PER_OUT_RE.findall(feedback)))
    trace = parse_trace(feedback)
    div = first_divergence(trace) if trace else ""
    return (total.groups(), per_out, div)


def no_effect_note(feedback: str, previous: str) -> str:
    """Say so when a repair changed the code but not the behaviour."""
    cur = _behaviour_signature(feedback)
    if cur is None or cur != _behaviour_signature(previous):
        return ""
    wrong, total = cur[0]
    return (f"Your previous change had no effect on behaviour: still {wrong} "
            f"of {total} samples wrong, with the same first disagreement. "
            "Rewriting the code into an equivalent form will not fix it; "
            "the logic produces the wrong value. Change the value or "
            "condition the evidence below points at.")


# -------------------------------------------------------------- combine

UNCHANGED_NOTE = ("Returning the same code will fail in exactly the same "
                  "way. Change the behaviour this evidence points to.")


ALL_PARTS = frozenset({"trace", "cone", "noeffect"})


def evidence_parts() -> frozenset:
    raw = os.environ.get("RTLFORGE_EVIDENCE", "1").strip().lower()
    if raw in ("0", "off", "none", ""):
        return frozenset()
    if raw in ("1", "on", "all"):
        return ALL_PARTS
    parts = frozenset(p.strip() for p in raw.split(",")) & ALL_PARTS
    return parts


def evidence_enabled() -> bool:
    return bool(evidence_parts())


def repair_evidence(rtl: str, top: str | None, feedback: str,
                    previous: str = "") -> str:
    """Everything a functional repair gets beyond raw diagnostics.

    Never raises. Returns "" when disabled or when nothing useful exists.
    """
    use = evidence_parts()
    if not use:
        return ""
    parts: list[str] = []
    try:
        note = no_effect_note(feedback, previous) if "noeffect" in use else ""
        if note:
            parts.append(note)          # first: it changes how to read the rest
    except Exception:                                     # noqa: BLE001
        pass
    try:
        summary = trace_summary(feedback) if "trace" in use else ""
        if summary:
            parts.append(summary)
    except Exception:                                     # noqa: BLE001
        pass
    try:
        from .cone import cone_hint
        cone = cone_hint(rtl, top, feedback) if "cone" in use else ""
        if cone:
            parts.append(cone)
    except Exception:                                     # noqa: BLE001
        pass
    if not parts:
        return ""
    parts.append(UNCHANGED_NOTE)
    return "\n\n".join(parts)
