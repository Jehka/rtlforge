"""Oracle regression tests.

A testbench that stops catching bugs is worse than no testbench -- it turns
every result into a silent false positive. These tests assert that each
trusted testbench still passes its known-good design and still fails each
known-bad one, at the expected stage.

Run these whenever you touch a testbench, upgrade a tool, or add a problem.

Skipped automatically if the EDA tools are not installed.
"""

import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from rtlforge import runners  # noqa: E402
from rtlforge.loop import Problem  # noqa: E402

TOOLS_PRESENT = all(
    shutil.which(t) for t in ("verilator", "iverilog", "yosys", "vvp")
)
needs_tools = pytest.mark.skipif(
    not TOOLS_PRESENT, reason="EDA tools not on PATH"
)

# (problem, rtl file, stage expected to fail; None means all stages pass)
CASES = [
    # ---- counter
    ("counter", "counter_good.v", None),
    ("counter", "counter_syntax_error.v", "lint"),
    ("counter", "counter_sync_reset_bug.v", "simulate"),
    ("counter", "counter_missing_enable_bug.v", "simulate"),
    # ---- sequence detector
    ("seq_detect", "seq_detect_good.v", None),
    ("seq_detect", "seq_detect_nonoverlap_bug.v", "simulate"),
    ("seq_detect", "seq_detect_level_held_bug.v", "simulate"),
    # ---- fifo
    ("fifo", "fifo_good.v", None),
    ("fifo", "fifo_overflow_bug.v", "simulate"),
    ("fifo", "fifo_comb_read_bug.v", "simulate"),
    ("fifo", "fifo_depth7_bug.v", "simulate"),
    # ---- 32-bit adder. Two correct implementations with different critical
    # paths, so the problem discriminates on timing rather than only on
    # function -- which is the point of having it.
    ("adder32", "adder32_ripple.v", None),
    ("adder32", "adder32_fast.v", None),
    ("adder32", "adder32_nocarry_bug.v", "simulate"),
    # Model-generated carry-lookahead: right structure, one off-by-one.
    ("adder32", "adder32_cla_offbyone_bug.v", "simulate"),
    ("adder32", "adder32_cla_fixed.v", None),
]


def _pipeline(problem_name: str, rtl_name: str, tmp_path: Path):
    """Run all stages, returning the first failing stage or None."""
    problem = Problem(ROOT / "problems" / problem_name)
    design = tmp_path / "design.v"
    shutil.copy(ROOT / "examples" / rtl_name, design)
    tb = tmp_path / problem.tb_path.name
    shutil.copy(problem.tb_path, tb)

    for name, fn in [
        ("lint", lambda: runners.lint(design, tmp_path)),
        ("compile", lambda: runners.compile_rtl(design, tmp_path, tb=tb)),
        ("simulate", lambda: runners.simulate(tmp_path)),
        ("synth", lambda: runners.synthesize(design, tmp_path, problem.top)),
    ]:
        sr = fn()
        if sr.skipped:
            continue
        if not sr.passed:
            return name
    return None


@needs_tools
@pytest.mark.parametrize("problem,rtl,expected_stage", CASES)
def test_oracle(problem, rtl, expected_stage, tmp_path):
    got = _pipeline(problem, rtl, tmp_path)
    if expected_stage is None:
        assert got is None, (
            f"{rtl} is a known-good design but failed at {got}. "
            "The testbench is wrong, not the design."
        )
    else:
        assert got == expected_stage, (
            f"{rtl} should fail at {expected_stage}, but "
            + (f"failed at {got}" if got else "passed everything -- "
               "the testbench no longer catches this bug")
        )
