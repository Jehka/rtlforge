"""Backend stages: technology mapping and static timing analysis.

Where the frontend stages ask "is this design correct?", these ask "is it
implementable, and does it meet timing?". Two things change:

* **The metric becomes continuous.** Simulation gives pass/fail. Timing gives
  slack, which is a number the agent can be pushed to improve. That is the
  first stage where "better" is meaningful rather than just "passing".

* **The action space forks.** Negative slack can be fixed by changing the RTL
  (add a pipeline stage), by relaxing the SDC (slow the clock), or by changing
  synthesis effort. Only the first is real engineering. `TimingResult` carries
  the constraint it was measured under so a report can never be compared
  against a target that was quietly moved -- see `SDC_IS_IMMUTABLE` below.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from .parsers import Diagnostic
from .runners import (
    DEFAULT_TIMEOUT,
    PD_BACKEND,
    StageResult,
    _missing,
    _run,
    infra_failure,
)

# ORFS installs its tools outside the default PATH.
ORFS_BIN = "/OpenROAD-flow-scripts/tools/install/OpenROAD/bin"

# The agent may never edit the SDC during a benchmark run. A design that meets
# timing because the clock was slowed has not been improved, and an agent given
# write access to its own target will find that out faster than you will.
SDC_IS_IMMUTABLE = True


# ------------------------------------------------------------ technology map

_AREA_RE = re.compile(r"Chip area for module '[^']*':\s*(?P<area>[\d.]+)")
_SEQ_AREA_RE = re.compile(
    r"used for sequential elements:\s*(?P<area>[\d.]+)"
)

# Yosys changed the `stat` layout between versions and both are in the wild:
#
#   0.33 and earlier          0.68 and later
#   --------------------      ------------------------
#   Number of cells:  11             11   31.122 cells
#        DFFR_X1       4              4    21.28   DFFR_X1
#
# The newer format puts the count first and the name last, so a parser written
# for one reads the other's leading digit as a cell name and silently returns
# nothing -- which is how this surfaced: area parsed, cells came back None.
_CELL_OLD_RE = re.compile(r"^\s+(?P<cell>[A-Za-z_][\w]*)\s+(?P<n>\d+)\s*$")
_CELL_NEW_RE = re.compile(
    r"^\s+(?P<n>\d+)\s+(?P<area>[\d.]+)\s+(?P<cell>[A-Za-z_][\w]*)\s*$"
)
_TOTAL_OLD_RE = re.compile(r"Number of cells:\s+(?P<n>\d+)")
_TOTAL_NEW_RE = re.compile(r"^\s+(?P<n>\d+)\s+[\d.]+\s+cells\s*$", re.M)
_NOT_A_CELL = re.compile(r"^\s*(Number|Chip|Estimated|Area)\b")


@dataclass
class MapMetrics:
    area_um2: Optional[float] = None
    cells: Optional[int] = None
    cell_mix: dict = field(default_factory=dict)
    flops: Optional[int] = None
    seq_area_um2: Optional[float] = None   # area in flops; high share = state-heavy


def parse_techmap(output: str) -> tuple[MapMetrics, List[Diagnostic]]:
    """Parse `stat -liberty` output after mapping to a real cell library.

    Handles both Yosys stat layouts; see the regex block above.
    """
    diags = [
        Diagnostic("error", None, "YOSYS", line.strip()[7:])
        for line in output.splitlines() if line.strip().startswith("ERROR:")
    ]

    mix: dict = {}
    for line in output.splitlines():
        if _NOT_A_CELL.match(line):
            continue
        stripped = line.rstrip()
        m = _CELL_NEW_RE.match(stripped)
        if m and m.group("cell") != "cells":
            mix[m.group("cell")] = int(m.group("n"))
            continue
        m = _CELL_OLD_RE.match(stripped)
        if m:
            mix[m.group("cell")] = int(m.group("n"))

    total = None
    m = _TOTAL_NEW_RE.search(output)
    if m:
        total = int(m.group("n"))
    else:
        counts = _TOTAL_OLD_RE.findall(output)
        if counts:
            total = int(counts[-1])
    if total is None and mix:
        total = sum(mix.values())

    areas = _AREA_RE.findall(output)
    seq = _SEQ_AREA_RE.search(output)

    # Flop count is the headline sequential metric and a good sanity check:
    # a design that should have 4 registers and maps to 40 has a problem.
    flops = sum(n for c, n in mix.items() if re.match(r"^(DFF|SDFF|DLH|DLL)", c))

    return MapMetrics(
        area_um2=float(areas[-1]) if areas else None,
        cells=total,
        cell_mix=mix,
        flops=flops or None,
        seq_area_um2=float(seq.group("area")) if seq else None,
    ), diags


def techmap(design: Path, workdir: Path, top: str, liberty: Path) -> StageResult:
    """Synthesize and map to a real standard-cell library.

    Produces `mapped.v`, the gate-level netlist STA needs. Generic `synth`
    gives abstract cell counts; this gives area in um^2 and the actual cells,
    which is what a backend flow consumes.
    """
    if (miss := _missing("yosys", "map", backend=PD_BACKEND)):
        return miss
    # Only meaningful locally: under a container backend the liberty lives in
    # that container's filesystem, not this one. Checking here would skip a
    # stage that would have worked -- the same mistake as looking for a tool
    # on the agent's PATH when it runs in the sandbox.
    if PD_BACKEND == "local" and not liberty.exists():
        return StageResult("map", False, skipped=True,
                           note=f"liberty file not found: {liberty}")
    if not str(liberty):
        return StageResult("map", False, skipped=True,
                           note="no liberty file set (RTLFORGE_LIBERTY)")

    script = (
        f"read_verilog -sv {design.name}; "
        f"synth -top {top}; "
        f"dfflibmap -liberty {liberty}; "
        f"abc -liberty {liberty}; "
        f"opt_clean; "
        f"write_verilog -noattr mapped.v; "
        f"stat -liberty {liberty}"
    )
    rc, out = _run(["yosys", "-p", script], workdir, timeout=180,
                   backend=PD_BACKEND)
    if (why := infra_failure(rc, out)):
        return StageResult("map", False, note=f"tool did not run: {why}",
                           raw=out)
    metrics, diags = parse_techmap(out)
    passed = rc == 0 and not diags and (workdir / "mapped.v").exists()
    sr = StageResult("map", passed, diags if not passed else [], out)
    sr.map_metrics = metrics
    return sr


# ---------------------------------------------------------------------- STA

# Real OpenSTA report_checks output ends each path with a slack line:
#            9.43   slack (MET)
#           -0.34   slack (VIOLATED)
_SLACK_RE = re.compile(r"^\s*(?P<slack>-?[\d.]+)\s+slack\s+\((?P<verdict>MET|VIOLATED)\)")
_START_RE = re.compile(r"^Startpoint:\s*(?P<p>.+?)\s*$")
_END_RE = re.compile(r"^Endpoint:\s*(?P<p>.+?)\s*$")
_TNS_RE = re.compile(r"^\s*tns\s+(?P<v>-?[\d.]+)", re.M)
_WNS_RE = re.compile(r"^\s*wns\s+(?P<v>-?[\d.]+)", re.M)


@dataclass
class TimingResult:
    wns: Optional[float] = None          # worst negative slack, ns
    tns: Optional[float] = None          # total negative slack, ns
    clock_period_ns: Optional[float] = None   # what it was measured against
    startpoint: str = ""
    endpoint: str = ""
    critical_path: List[str] = field(default_factory=list)
    violated: bool = False

    @property
    def reg_to_reg(self) -> bool:
        """Whether the worst path runs between two flip-flops."""
        return ("flip-flop" in self.startpoint and
                "flip-flop" in self.endpoint)

    @property
    def fmax_mhz(self) -> Optional[float]:
        """Achievable frequency implied by the worst path.

        Requires three things, and returns None without any of them:

        * the slack,
        * the constraint it was measured against -- a great WNS against a
          100ns clock is not an achievement,
        * a register-to-register worst path. `write_sdc` scales I/O delay as
          a fraction of the period, so at a loose constraint the worst path
          is often an input or output path. Deriving a frequency from that
          describes the constraint, not the logic, and produces the
          contradiction of one design reporting two different Fmax values at
          two different clock periods.
        """
        if self.wns is None or self.clock_period_ns is None:
            return None
        if not self.reg_to_reg:
            return None
        achieved = self.clock_period_ns - self.wns
        return 1000.0 / achieved if achieved > 0 else None


def parse_sta(output: str, clock_period_ns: Optional[float] = None
              ) -> tuple[TimingResult, List[Diagnostic]]:
    """Parse `report_checks` output into slack plus the critical path.

    The path table is the useful part for repair: it names the cells and the
    delay contributed by each, which tells the agent *where* to pipeline
    rather than just that it is too slow.
    """
    res = TimingResult(clock_period_ns=clock_period_ns)
    diags: List[Diagnostic] = []

    slacks: List[float] = []
    current_path: List[str] = []
    start = end = ""

    for raw in output.splitlines():
        line = raw.rstrip()

        m = _START_RE.match(line)
        if m:
            start, current_path = m.group("p"), []
            continue
        m = _END_RE.match(line)
        if m:
            end = m.group("p")
            continue

        # "   0.23    0.23 v r2/Q (DFF_X1)" -- a delay contribution
        m = re.match(r"^\s+(?P<d>[\d.]+)\s+[\d.]+\s+[v^]\s+(?P<pin>\S+)\s+\((?P<cell>[^)]+)\)", line)
        if m and float(m.group("d")) > 0:
            current_path.append(
                f"{m.group('pin')} ({m.group('cell')}) +{m.group('d')}ns"
            )
            continue

        m = _SLACK_RE.match(line)
        if m:
            slack = float(m.group("slack"))
            slacks.append(slack)
            if res.wns is None or slack < res.wns:
                res.wns = slack
                res.startpoint, res.endpoint = start, end
                res.critical_path = list(current_path)
            continue

    # Prefer explicit report_wns/report_tns when present.
    m = _WNS_RE.search(output)
    if m:
        res.wns = float(m.group("v"))
    m = _TNS_RE.search(output)
    if m:
        res.tns = float(m.group("v"))
    elif slacks:
        res.tns = sum(s for s in slacks if s < 0) or 0.0

    res.violated = res.wns is not None and res.wns < 0
    if res.violated:
        path = " -> ".join(res.critical_path[:6]) or "(path unavailable)"
        diags.append(Diagnostic(
            "error", None, "TIMING",
            f"setup violation: WNS {res.wns:.3f}ns"
            + (f" against a {clock_period_ns}ns clock" if clock_period_ns else "")
            + f". Critical path {res.startpoint} -> {res.endpoint}: {path}",
        ))
    return res, diags


def write_sdc(path: Path, clock_port: str, period_ns: float,
              input_delay_frac: float = 0.2) -> None:
    """Write a minimal but honest SDC.

    Input/output delays matter: without them the tool sees combinational
    paths from ports as having a full clock period available, and reports
    timing that no real chip would meet.
    """
    io = period_ns * input_delay_frac
    path.write_text(
        f"create_clock -name clk -period {period_ns} [get_ports {clock_port}]\n"
        f"set_input_delay {io:.3f} -clock clk [all_inputs]\n"
        f"set_output_delay {io:.3f} -clock clk [all_outputs]\n"
        f"set_input_delay 0 -clock clk [get_ports {clock_port}]\n"
    )


def run_sta(workdir: Path, top: str, liberty: Path, clock_port: str,
            period_ns: float, timeout: int = DEFAULT_TIMEOUT) -> StageResult:
    """Run OpenSTA on the mapped netlist against a fixed clock constraint."""
    if PD_BACKEND == "local":
        exe = shutil.which("sta") or shutil.which("opensta")
        if exe is None:
            return StageResult("sta", False, skipped=True,
                               note="OpenSTA not found on PATH (try: sta)")
    else:
        # Absolute path: ORFS puts sta here and not on the default PATH.
        exe = f"{ORFS_BIN}/sta"
    if not (workdir / "mapped.v").exists():
        return StageResult("sta", False, skipped=True,
                           note="no mapped.v; run the map stage first")

    sdc = workdir / "constraints.sdc"
    write_sdc(sdc, clock_port, period_ns)

    script = workdir / "sta.tcl"
    script.write_text(
        f"read_liberty {liberty}\n"
        f"read_verilog mapped.v\n"
        f"link_design {top}\n"
        f"read_sdc {sdc.name}\n"
        f"report_checks -path_delay max -digits 3\n"
        f"report_wns\n"
        f"report_tns\n"
        f"exit\n"
    )

    rc, out = _run([exe, "-no_splash", "-exit", script.name], workdir,
                   timeout=timeout, backend=PD_BACKEND)
    if (why := infra_failure(rc, out)):
        return StageResult("sta", False, note=f"tool did not run: {why}",
                           raw=out)
    timing, diags = parse_sta(out, clock_period_ns=period_ns)
    sr = StageResult("sta", not timing.violated,
                     diags if timing.violated else [], out)
    sr.timing = timing
    return sr
