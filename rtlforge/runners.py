"""Thin subprocess wrappers around the EDA tools.

Each runner returns a StageResult with the same shape regardless of tool, so
the loop never has to know which tool produced a failure.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from .parsers import (
    Diagnostic,
    SynthMetrics,
    parse_icarus,
    parse_simulation,
    parse_verilator,
    parse_yosys,
)

DEFAULT_TIMEOUT = 60  # seconds; a hung simulation must not stall the loop

# Where EDA tools actually run.
#   unset / "local"  -> tools must be on PATH (bare-metal development)
#   "docker:<name>"  -> commands are dispatched into that container
#
# This is the one knob that lets the same code run bare-metal on your laptop
# and inside the two-container split without changing the loop.
EDA_BACKEND = os.environ.get("RTLFORGE_EDA", "local")
EDA_WORKDIR = os.environ.get("RTLFORGE_EDA_WORKDIR", "/work")


@dataclass
class StageResult:
    stage: str
    passed: bool
    diagnostics: List[Diagnostic] = field(default_factory=list)
    raw: str = ""
    metrics: Optional[SynthMetrics] = None
    skipped: bool = False
    note: str = ""

    def as_dict(self) -> dict:
        return {
            "stage": self.stage,
            "passed": self.passed,
            "skipped": self.skipped,
            "note": self.note,
            "diagnostics": [d.as_dict() for d in self.diagnostics],
            "metrics": self.metrics.__dict__ if self.metrics else None,
        }


def _wrap(cmd: List[str], cwd: Path) -> List[str]:
    """Rewrite a command for the configured EDA backend."""
    if EDA_BACKEND == "local":
        return cmd
    if EDA_BACKEND.startswith("docker:"):
        container = EDA_BACKEND.split(":", 1)[1]
        # cwd on the host maps to EDA_WORKDIR inside the container via the
        # bind mount declared in docker-compose.yml.
        rel = cwd.name if cwd.name else "."
        return [
            "docker", "exec", "-w", f"{EDA_WORKDIR}/{rel}", container
        ] + cmd
    raise RuntimeError(f"unknown RTLFORGE_EDA backend: {EDA_BACKEND!r}")


def _run(cmd: List[str], cwd: Path, timeout: int = DEFAULT_TIMEOUT):
    """Run a command, merging stdout+stderr. Never raises on tool failure."""
    cmd = _wrap(cmd, cwd)
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, f"TIMEOUT: command exceeded {timeout}s: {' '.join(cmd)}"
    except FileNotFoundError:
        return 127, f"NOTFOUND: {cmd[0]} is not installed"


def _missing(tool: str, stage: str) -> Optional[StageResult]:
    # Under a docker backend the tool lives in the container, not on this PATH.
    if EDA_BACKEND != "local":
        return None
    if shutil.which(tool) is None:
        return StageResult(
            stage=stage, passed=False, skipped=True,
            note=f"{tool} not found on PATH",
        )
    return None


def lint(design: Path, workdir: Path, strict: bool = False) -> StageResult:
    """Verilator lint.

    strict=False treats warnings as advisory: style warnings like
    DECLFILENAME would otherwise send the model into pointless repair cycles
    on designs that are functionally fine. Set strict=True to study whether
    warning-level feedback helps or hurts -- that ablation is the interesting
    part.
    """
    if (miss := _missing("verilator", "lint")):
        return miss

    rc, out = _run(
        ["verilator", "--lint-only", "-Wall", design.name], workdir
    )
    diags = parse_verilator(out)
    errors = [d for d in diags if d.severity == "error"]
    passed = not errors if not strict else not diags
    return StageResult("lint", passed, diags if not passed else [], out)


def compile_rtl(design: Path, workdir: Path,
                tb: Optional[Path] = None) -> StageResult:
    """Icarus compile. Catches things Verilator's lint pass lets through."""
    if (miss := _missing("iverilog", "compile")):
        return miss

    sources = [design.name] + ([tb.name] if tb else [])
    rc, out = _run(
        ["iverilog", "-g2012", "-o", "sim.out"] + sources, workdir
    )
    diags = parse_icarus(out)
    errors = [d for d in diags if d.severity == "error"]
    passed = rc == 0 and not errors
    return StageResult("compile", passed, diags if not passed else [], out)


def simulate(workdir: Path, timeout: int = DEFAULT_TIMEOUT) -> StageResult:
    """Run the compiled simulation. Requires compile_rtl to have run with a tb."""
    if (miss := _missing("vvp", "simulate")):
        return miss
    if not (workdir / "sim.out").exists():
        return StageResult(
            "simulate", False, skipped=True,
            note="no sim.out; compile stage did not produce a binary",
        )

    rc, out = _run(["vvp", "sim.out"], workdir, timeout=timeout)
    passed, diags = parse_simulation(out)
    return StageResult("simulate", passed, [] if passed else diags, out)


def synthesize(design: Path, workdir: Path, top: str) -> StageResult:
    """Yosys generic synthesis. Confirms synthesizability and yields area."""
    if (miss := _missing("yosys", "synth")):
        return miss

    script = f"read_verilog -sv {design.name}; synth -top {top}; stat"
    rc, out = _run(["yosys", "-p", script], workdir)
    metrics, diags = parse_yosys(out)
    passed = rc == 0 and not diags
    return StageResult(
        "synth", passed, diags if not passed else [], out, metrics=metrics
    )
