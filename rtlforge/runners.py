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

from . import waveform
from .parsers import (
    Diagnostic,
    SynthMetrics,
    parse_icarus,
    parse_simulation,
    parse_verilator,
    parse_verilogeval_sim,
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

# Backend stages need a different container. The ORFS image ships Yosys,
# OpenSTA and OpenROAD but no simulators; the verification image ships
# Verilator and Icarus but no backend tools. Rather than build one image with
# everything, each stage dispatches to the container that has its tools.
# Falls back to the verification backend when unset.
PD_BACKEND = os.environ.get("RTLFORGE_PD", EDA_BACKEND)


@dataclass
class StageResult:
    stage: str
    passed: bool
    diagnostics: List[Diagnostic] = field(default_factory=list)
    raw: str = ""
    metrics: Optional[SynthMetrics] = None
    skipped: bool = False
    note: str = ""
    waveform: str = ""   # trace window around the first mismatch, if available
    map_metrics: object = None   # MapMetrics, set by the backend map stage
    timing: object = None        # TimingResult, set by the backend sta stage

    def as_dict(self) -> dict:
        return {
            "stage": self.stage,
            "passed": self.passed,
            "skipped": self.skipped,
            "note": self.note,
            "diagnostics": [d.as_dict() for d in self.diagnostics],
            "metrics": self.metrics.__dict__ if self.metrics else None,
            "map_metrics": self.map_metrics.__dict__ if self.map_metrics else None,
            "timing": self.timing.__dict__ if self.timing else None,
        }


def make_workdir(path: Path) -> Path:
    """Create a work directory both containers can write to.

    The agent runs as root; the EDA sandbox runs as a non-root user so that
    generated RTL does not execute with root privileges. A root-owned work
    directory therefore looks writable to the agent and read-only to the tool
    that actually needs to write into it -- which shows up as a stage that
    fails with no diagnostics, because the tool never got far enough to
    produce any.
    """
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o777)
    except OSError:
        pass  # non-POSIX or already owned by someone else; not fatal
    return path


def _wrap(cmd: List[str], cwd: Path, backend: Optional[str] = None) -> List[str]:
    """Rewrite a command for the configured backend."""
    backend = backend or EDA_BACKEND
    if backend == "local":
        return cmd
    if backend.startswith("docker:"):
        container = backend.split(":", 1)[1]
        # cwd on the host maps to EDA_WORKDIR inside the container via the
        # bind mount declared in docker-compose.yml.
        rel = cwd.name if cwd.name else "."
        return [
            "docker", "exec", "-w", f"{EDA_WORKDIR}/{rel}", container
        ] + cmd
    raise RuntimeError(f"unknown backend spec: {backend!r}")


# Exit codes and markers that mean "the tool never ran", as distinct from
# "the tool ran and found problems". These must never reach a parser: an empty
# error list from a command that failed to execute looks exactly like a clean
# pass, which silently turns infrastructure breakage into green results.
INFRA_MARKERS = ("NOTFOUND:", "TIMEOUT:")
INFRA_CODES = (126, 127)


def infra_failure(rc: int, out: str) -> Optional[str]:
    """Return a reason string if the command could not be executed at all."""
    if rc in INFRA_CODES:
        return out.strip().splitlines()[0] if out.strip() else f"exit {rc}"
    for marker in INFRA_MARKERS:
        if out.startswith(marker):
            return out.strip().splitlines()[0]
    # `docker exec` failures surface as ordinary stderr, not a special code.
    low = out.lower()
    for phrase in ("is not running", "no such container",
                   "cannot connect to the docker daemon",
                   "command not found"):
        if phrase in low:
            return out.strip().splitlines()[0][:160]
    return None


def _run(cmd: List[str], cwd: Path, timeout: int = DEFAULT_TIMEOUT,
         backend: Optional[str] = None):
    """Run a command, merging stdout+stderr. Never raises on tool failure."""
    cmd = _wrap(cmd, cwd, backend)
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


def _missing(tool: str, stage: str,
             backend: Optional[str] = None) -> Optional[StageResult]:
    # Under a docker backend the tool lives in the container, not on this PATH.
    if (backend or EDA_BACKEND) != "local":
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
    if (why := infra_failure(rc, out)):
        return StageResult("lint", False, note=f"tool did not run: {why}",
                           raw=out)
    diags = parse_verilator(out)
    errors = [d for d in diags if d.severity == "error"]
    passed = not errors if not strict else not diags
    return StageResult("lint", passed, diags if not passed else [], out)


def compile_rtl(design: Path, workdir: Path,
                tb: Optional[Path] = None,
                extra: Optional[List[Path]] = None) -> StageResult:
    """Icarus compile. Catches things Verilator's lint pass lets through.

    `extra` carries additional sources the testbench needs -- VerilogEval
    testbenches instantiate a golden RefModule alongside the candidate.
    """
    if (miss := _missing("iverilog", "compile")):
        return miss

    sources = [design.name]
    sources += [e.name for e in (extra or [])]
    sources += [tb.name] if tb else []
    rc, out = _run(
        ["iverilog", "-g2012", "-o", "sim.out"] + sources, workdir
    )
    if (why := infra_failure(rc, out)):
        return StageResult("compile", False, note=f"tool did not run: {why}",
                           raw=out)
    diags = parse_icarus(out)
    errors = [d for d in diags if d.severity == "error"]
    passed = rc == 0 and not errors
    return StageResult("compile", passed, diags if not passed else [], out)


def simulate(workdir: Path, timeout: int = DEFAULT_TIMEOUT,
             sim_format: str = "markers") -> StageResult:
    """Run the compiled simulation. Requires compile_rtl to have run with a tb.

    sim_format "markers"     -> our TB_PASS / MISMATCH protocol
    sim_format "verilogeval" -> "Mismatches: N in M samples"
    """
    if (miss := _missing("vvp", "simulate")):
        return miss
    if not (workdir / "sim.out").exists():
        return StageResult(
            "simulate", False, skipped=True,
            note="no sim.out; compile stage did not produce a binary",
        )

    rc, out = _run(["vvp", "sim.out"], workdir, timeout=timeout)
    if (why := infra_failure(rc, out)):
        return StageResult("simulate", False, note=f"tool did not run: {why}",
                           raw=out)
    parse = (parse_verilogeval_sim if sim_format == "verilogeval"
             else parse_simulation)
    passed, diags = parse(out)

    # On failure, pull the trace window around the divergence. "First mismatch
    # at time 130" tells the model when but not what; the window shows the
    # inputs that led there and how its output differed from the reference.
    wave = ""
    if not passed:
        vcd = workdir / "wave.vcd"
        if vcd.exists():
            wave = waveform.window_at(vcd, waveform.first_mismatch_time(out))

    return StageResult("simulate", passed, [] if passed else diags, out,
                       waveform=wave)


def synthesize(design: Path, workdir: Path, top: str) -> StageResult:
    """Yosys generic synthesis. Confirms synthesizability and yields area."""
    if (miss := _missing("yosys", "synth")):
        return miss

    script = f"read_verilog -sv {design.name}; synth -top {top}; stat"
    rc, out = _run(["yosys", "-p", script], workdir)
    if (why := infra_failure(rc, out)):
        return StageResult("synth", False, note=f"tool did not run: {why}",
                           raw=out)
    metrics, diags = parse_yosys(out)
    passed = rc == 0 and not diags
    return StageResult(
        "synth", passed, diags if not passed else [], out, metrics=metrics
    )
