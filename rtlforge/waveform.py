"""Extract a textual waveform window around the first mismatch.

VerilogEval testbenches dump `wave.vcd` containing the inputs plus paired
`<out>_ref` and `<out>_dut` signals. A failure report that says "first mismatch
at time 130" tells the model *when* but not *what*: it cannot see the input
sequence that led there or how its output diverged. This module turns that into
a small table.

This is the state-checkpoint idea from MAGE (Zhao et al.), obtained here for
free because the benchmark's own testbenches already dump the trace.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

_VAR_RE = re.compile(
    r"^\$var\s+\w+\s+(?P<width>\d+)\s+(?P<sym>\S+)\s+(?P<name>\S+)"
)
_SCALAR_RE = re.compile(r"^(?P<val>[01xzXZ])(?P<sym>\S+)$")
_VECTOR_RE = re.compile(r"^b(?P<val>[01xzXZ]+)\s+(?P<sym>\S+)$")

MAX_ROWS = 12          # rows of trace returned; more is noise and tokens
MAX_SIGNALS = 14       # columns; wide traces stop being readable


def parse_vcd(path: Path) -> Tuple[List[str], List[Tuple[int, Dict[str, str]]]]:
    """Return (signal names, [(time, {name: value})]) with values carried forward.

    Deliberately minimal: scalars and vectors only, no real numbers, no
    multi-scope disambiguation beyond the leaf name.
    """
    sym2name: Dict[str, str] = {}
    order: List[str] = []
    rows: List[Tuple[int, Dict[str, str]]] = []
    current: Dict[str, str] = {}
    time = 0
    in_defs = True

    with open(path, "r", errors="replace") as fh:
        for raw in fh:
            line = raw.strip()
            if not line:
                continue

            if in_defs:
                m = _VAR_RE.match(line)
                if m:
                    name = m.group("name")
                    sym = m.group("sym")
                    # Several symbols can alias one name (clk appears twice);
                    # keep the first and let later ones update the same column.
                    sym2name[sym] = name
                    if name not in order:
                        order.append(name)
                    continue
                if line.startswith("$enddefinitions"):
                    in_defs = False
                continue

            if line.startswith("#"):
                if current:
                    rows.append((time, dict(current)))
                try:
                    time = int(line[1:])
                except ValueError:
                    pass
                continue

            m = _SCALAR_RE.match(line)
            if m and m.group("sym") in sym2name:
                current[sym2name[m.group("sym")]] = m.group("val")
                continue
            m = _VECTOR_RE.match(line)
            if m and m.group("sym") in sym2name:
                bits = m.group("val")
                try:
                    current[sym2name[m.group("sym")]] = f"{int(bits, 2):d}"
                except ValueError:
                    current[sym2name[m.group("sym")]] = bits
                continue

    if current:
        rows.append((time, dict(current)))
    return order, rows


def _order_signals(names: List[str]) -> List[str]:
    """Inputs first, then each output's ref/dut pair adjacent."""
    pairs, singles = [], []
    seen = set()
    for n in names:
        if n in seen:
            continue
        if n.endswith("_ref"):
            base = n[:-4]
            dut = base + "_dut"
            if dut in names:
                pairs.extend([n, dut])
                seen.update({n, dut})
                continue
        if n.endswith("_dut"):
            continue
        singles.append(n)
        seen.add(n)
    return singles + pairs


def window_at(path: Path, mismatch_time: Optional[int],
              before: int = 4, after: int = 6) -> str:
    """Render a compact trace table around `mismatch_time`.

    Returns an empty string if the trace is unusable, so callers can simply
    fall back to text-only feedback.
    """
    try:
        names, rows = parse_vcd(path)
    except OSError:
        return ""
    if not rows:
        return ""

    names = _order_signals(names)
    # tb_mismatch is the testbench's own flag; useful, but not a design signal
    names = [n for n in names if n != "stim1.clk"][:MAX_SIGNALS]

    idx = 0
    if mismatch_time is not None:
        for i, (t, _) in enumerate(rows):
            if t >= mismatch_time:
                idx = i
                break
        else:
            idx = len(rows) - 1

    lo = max(0, idx - before)
    hi = min(len(rows), idx + after)
    chunk = rows[lo:hi]
    if len(chunk) > MAX_ROWS:
        chunk = chunk[:MAX_ROWS]

    widths = {n: max(len(n), 4) for n in names}
    header = "time     | " + " | ".join(n.ljust(widths[n]) for n in names)
    sep = "-" * len(header)
    lines = [header, sep]
    for t, vals in chunk:
        marker = " <-- first mismatch" if (
            mismatch_time is not None and t == rows[idx][0]
        ) else ""
        cells = " | ".join(
            str(vals.get(n, "-")).ljust(widths[n]) for n in names
        )
        lines.append(f"{str(t).ljust(8)} | {cells}{marker}")
    return "\n".join(lines)


_FIRST_MISMATCH_RE = re.compile(
    r"[Ff]irst mismatch occurred at time (?P<t>\d+)"
)


def first_mismatch_time(output: str) -> Optional[int]:
    """Earliest mismatch time mentioned across all output hints."""
    times = [int(m.group("t")) for m in _FIRST_MISMATCH_RE.finditer(output)]
    return min(times) if times else None
