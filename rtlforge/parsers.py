"""Parse raw EDA tool output into structured, token-efficient records.

Every parser returns a list of Diagnostic. The loop feeds these back to the
model instead of raw logs -- raw Verilator output is roughly 4x the tokens for
the same information, and free-tier daily token ceilings bind before request
counts do.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, asdict
from typing import List, Optional


@dataclass
class Diagnostic:
    severity: str            # "error" | "warning"
    line: Optional[int]      # line number in the design file, if known
    code: Optional[str]      # tool-specific code, e.g. "WIDTHTRUNC"
    message: str

    def as_dict(self) -> dict:
        return asdict(self)

    def render(self) -> str:
        loc = f"line {self.line}" if self.line is not None else "unknown line"
        code = f" [{self.code}]" if self.code else ""
        return f"{self.severity.upper()}{code} at {loc}: {self.message}"


# ---------------------------------------------------------------- verilator

# %Error: file.v:9:9: syntax error, unexpected else
# %Warning-WIDTHTRUNC: warn.v:9:19: Operator ASSIGNDLY expects 4 bits ...
_VERILATOR_RE = re.compile(
    r"^%(?P<sev>Error|Warning)"
    r"(?:-(?P<code>[A-Z0-9_]+))?"
    r":\s+"
    r"(?P<file>[^:]+):(?P<line>\d+):(?P<col>\d+):\s+"
    r"(?P<msg>.*)$"
)

# "%Error: Exiting due to 2 warning(s)" is a summary line, not a diagnostic.
_VERILATOR_SUMMARY_RE = re.compile(r"^%Error:\s+Exiting due to", re.IGNORECASE)


def parse_verilator(output: str) -> List[Diagnostic]:
    diags: List[Diagnostic] = []
    for raw in output.splitlines():
        line = raw.rstrip()
        if not line or _VERILATOR_SUMMARY_RE.match(line):
            continue
        # Skip source echo ("    9 | ..."), carets, and "... note:" / "... For
        # warning description" continuation lines.
        if line.lstrip().startswith(("...", "|", "^")):
            continue
        m = _VERILATOR_RE.match(line)
        if not m:
            continue
        diags.append(
            Diagnostic(
                severity=m.group("sev").lower(),
                line=int(m.group("line")),
                code=m.group("code"),
                message=m.group("msg").strip(),
            )
        )
    return diags


# ------------------------------------------------------------------ icarus

# bad.v:9: syntax error
# bad.v:10: Syntax in assignment statement l-value.
_ICARUS_RE = re.compile(
    r"^(?P<file>[^:\s]+\.s?v):(?P<line>\d+):\s*(?P<msg>.+)$"
)


def parse_icarus(output: str) -> List[Diagnostic]:
    diags: List[Diagnostic] = []
    for raw in output.splitlines():
        line = raw.strip()
        if not line:
            continue
        m = _ICARUS_RE.match(line)
        if not m:
            continue
        msg = m.group("msg").strip()
        severity = "warning" if msg.lower().startswith("warning") else "error"
        diags.append(
            Diagnostic(
                severity=severity,
                line=int(m.group("line")),
                code=None,
                message=msg,
            )
        )
    return diags


# ---------------------------------------------------------------- simulation

# Testbench convention: print "TB_PASS" or "TB_FAIL: <reason>" and $finish.
# Mismatch lines are surfaced individually so the model sees the actual
# functional divergence, not just a pass/fail bit.
_TB_FAIL_RE = re.compile(r"^TB_FAIL:\s*(?P<msg>.+)$")
_TB_PASS_RE = re.compile(r"^TB_PASS\b")
_MISMATCH_RE = re.compile(r"^MISMATCH:\s*(?P<msg>.+)$")

MAX_MISMATCHES = 8  # cap fed back to the model; full log stays on disk


def parse_simulation(output: str) -> tuple[bool, List[Diagnostic]]:
    """Return (passed, diagnostics).

    Passed only if TB_PASS appears and no TB_FAIL/MISMATCH does. Absence of
    both markers counts as failure -- a testbench that never printed a verdict
    did not verify anything.
    """
    diags: List[Diagnostic] = []
    saw_pass = False
    saw_fail = False
    mismatches = 0

    for raw in output.splitlines():
        line = raw.strip()
        if _TB_PASS_RE.match(line):
            saw_pass = True
            continue
        m = _TB_FAIL_RE.match(line)
        if m:
            saw_fail = True
            diags.append(Diagnostic("error", None, "TB_FAIL", m.group("msg")))
            continue
        m = _MISMATCH_RE.match(line)
        if m:
            saw_fail = True
            mismatches += 1
            if mismatches <= MAX_MISMATCHES:
                diags.append(
                    Diagnostic("error", None, "MISMATCH", m.group("msg"))
                )
            continue

    if mismatches > MAX_MISMATCHES:
        diags.append(
            Diagnostic(
                "error", None, "MISMATCH",
                f"({mismatches - MAX_MISMATCHES} further mismatches omitted)",
            )
        )

    if not saw_pass and not saw_fail:
        diags.append(
            Diagnostic(
                "error", None, "NO_VERDICT",
                "Simulation produced no TB_PASS or TB_FAIL marker.",
            )
        )

    return (saw_pass and not saw_fail), diags


# ------------------------------------------------------------------- yosys

_CELLS_RE = re.compile(r"Number of cells:\s+(?P<n>\d+)")
_WIRES_RE = re.compile(r"Number of wires:\s+(?P<n>\d+)")
_YOSYS_ERR_RE = re.compile(r"^ERROR:\s*(?P<msg>.+)$")


@dataclass
class SynthMetrics:
    cells: Optional[int] = None
    wires: Optional[int] = None


def parse_yosys(output: str) -> tuple[SynthMetrics, List[Diagnostic]]:
    """Return (metrics, diagnostics).

    `stat` prints per-module blocks; the last "Number of cells" belongs to the
    top-level summary, so we take the final match rather than the first.
    """
    diags = [
        Diagnostic("error", None, "YOSYS", m.group("msg").strip())
        for m in (_YOSYS_ERR_RE.match(l.strip()) for l in output.splitlines())
        if m
    ]

    cells = _CELLS_RE.findall(output)
    wires = _WIRES_RE.findall(output)
    metrics = SynthMetrics(
        cells=int(cells[-1]) if cells else None,
        wires=int(wires[-1]) if wires else None,
    )
    return metrics, diags


# ------------------------------------------------------------------ helpers

def render_feedback(diags: List[Diagnostic], limit: int = 12) -> str:
    """Compact, deduplicated feedback block for the repair prompt."""
    seen: set = set()
    lines: List[str] = []
    for d in diags:
        key = (d.severity, d.line, d.code, d.message)
        if key in seen:
            continue
        seen.add(key)
        lines.append("- " + d.render())
        if len(lines) >= limit:
            lines.append(f"- (further diagnostics omitted)")
            break
    return "\n".join(lines)
