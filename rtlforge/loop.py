"""The generate -> verify -> repair loop.

Design notes that matter for the experiment:

* The testbench is never shown to the design model and never modified. A
  design and testbench written from the same misunderstanding pass together
  and prove nothing.
* Stages are gated in order. Feeding simulation output to a model whose RTL
  does not compile just wastes tokens.
* Every attempt is written to disk before the next one starts, so a run that
  dies at iteration 4 still yields analysable data.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from . import backend, runners
from .parsers import failure_magnitude, render_feedback
from .providers import LLMClient, extract_verilog

DESIGN_FILENAME = "design.v"

SYSTEM_PROMPT = (
    "You are an RTL design engineer. You write synthesizable Verilog-2005 / "
    "SystemVerilog. Respond with exactly one fenced code block containing the "
    "complete module and nothing else -- no prose, no explanation, no "
    "testbench. Do not use delays, $display, initial blocks, or any other "
    "non-synthesizable construct in the design."
)


@dataclass
class Attempt:
    index: int
    rtl: str
    stages: List[dict] = field(default_factory=list)
    passed: bool = False
    rtl_hash: str = ""          # identical hashes across attempts = a stall
    failed_stage: Optional[str] = None
    magnitude: Optional[float] = None   # fraction of samples mismatching
    rolled_back: bool = False   # this attempt was repaired from an earlier one
    behaviour: str = ""         # fingerprint of the observed failure


@dataclass
class RunResult:
    problem: str
    provider: str
    model: str
    feedback_level: str
    passed: bool
    iterations: int
    attempts: List[Attempt] = field(default_factory=list)
    wall_clock_s: float = 0.0
    terminal_stage: Optional[str] = None   # failed where this level is blind
    truncated: bool = False                # a response hit the token ceiling
    candidates_drawn: int = 1              # best-of-N sampling width
    stop_reason: str = ""                  # why the loop ended
    prompt_tokens: int = 0
    completion_tokens: int = 0
    final_cells: Optional[int] = None
    final_area_um2: Optional[float] = None
    final_wns_ns: Optional[float] = None

    def as_dict(self) -> dict:
        return {
            "problem": self.problem,
            "provider": self.provider,
            "model": self.model,
            "feedback_level": self.feedback_level,
            "passed": self.passed,
            "iterations": self.iterations,
            "wall_clock_s": round(self.wall_clock_s, 2),
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "final_cells": self.final_cells,
            "final_area_um2": self.final_area_um2,
            "final_wns_ns": self.final_wns_ns,
            "terminal_stage": self.terminal_stage,
            "truncated": self.truncated,
            "candidates_drawn": self.candidates_drawn,
            "stop_reason": self.stop_reason,
            "attempts": [
                {"index": a.index, "passed": a.passed, "stages": a.stages,
                 "rtl_hash": a.rtl_hash, "failed_stage": a.failed_stage,
                 "magnitude": a.magnitude, "rolled_back": a.rolled_back,
                 "behaviour": a.behaviour}
                for a in self.attempts
            ],
        }


# Feedback levels are the experiment's independent variable. They control what
# the model is TOLD, never what it is GRADED on. Every run is scored against
# the full stage set; only the repair signal differs. Grading a lint-only run
# on lint alone would compare a spelling quiz against a full exam.
STAGES_BY_LEVEL = {
    "none": [],
    "lint": ["lint"],
    "compile": ["lint", "compile"],
    "sim": ["lint", "compile", "simulate"],
    "full": ["lint", "compile", "simulate", "synth"],
}

# Always run all four. This is the scoreboard.
SCORING_STAGES = ["lint", "compile", "simulate", "synth"]

# How far down the pipeline a design is taken. Verification and
# synthesizability apply to every design; timing and physical implementation
# are opt-in, because they cost minutes to hours per design where simulation
# costs seconds. Most RTL never needs a layout.
FLOW_TIERS = {
    "verify":   ["lint", "compile", "simulate"],
    "synth":    ["lint", "compile", "simulate", "synth"],          # default
    "map":      ["lint", "compile", "simulate", "synth", "map"],
    "sta":      ["lint", "compile", "simulate", "synth", "map", "sta"],
    "gds":      ["lint", "compile", "simulate", "synth", "map", "sta",
                 "floorplan", "place", "cts", "route", "gds"],     # not built
}

# Stages where a failure is repaired by rewriting the RTL. Beyond this, the
# fix is a constraint or a tool parameter, and letting the agent edit RTL
# there produces designs that chase timing by mangling the logic. See
# BACKEND.md for the action-space table.
RTL_ACTION_STAGES = {"lint", "compile", "simulate", "synth", "map", "sta"}


class Problem:
    """A benchmark problem: a spec, a top module name, and a trusted testbench."""

    def __init__(self, directory: Path):
        self.dir = Path(directory)
        self.name = self.dir.name
        meta = json.loads((self.dir / "problem.json").read_text())
        self.top = meta["top"]
        self.spec = (self.dir / "spec.md").read_text()
        self.tb_path = self.dir / meta.get("testbench", "tb.v")
        if not self.tb_path.exists():
            raise FileNotFoundError(f"missing trusted testbench: {self.tb_path}")
        # Extra sources the testbench needs (e.g. a golden RefModule).
        self.extra_sources = [
            self.dir / n for n in meta.get("extra_sources", [])
        ]
        for e in self.extra_sources:
            if not e.exists():
                raise FileNotFoundError(f"missing source: {e}")
        self.sim_format = meta.get("sim_format", "markers")
        # Timing constraint. Lives with the problem, not with the agent: the
        # target is part of the specification, not something under repair.
        self.clock_port = meta.get("clock_port", "clk")
        self.clock_period_ns = float(meta.get("clock_period_ns", 2.0))


def _generate_prompt(problem: Problem) -> List[dict]:
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"Write a Verilog module named `{problem.top}` "
                f"implementing this specification.\n\n{problem.spec}"
            ),
        },
    ]


# How far through the pipeline an attempt got. Higher is better, and every
# stage that can fail must appear: a stage missing from this table defaults to
# 0 and therefore ranks below a lint failure, so rollback would discard a
# design that was picoseconds from closing timing in favour of one that does
# not parse. None means nothing failed.
STAGE_RANK = {"generate": 0, "lint": 1, "compile": 2, "simulate": 3,
              "synth": 4, "map": 5, "sta": 6,
              # Not implemented yet, ranked now so that adding them cannot
              # silently reintroduce the bug this table exists to prevent.
              "floorplan": 7, "place": 8, "cts": 9, "route": 10, "gds": 11,
              None: 12}


def _behaviour_key(stage_name: str, stage: dict) -> str:
    """Fingerprint what the tools observed, independent of how the RTL reads.

    Built from the failing stage plus the diagnostic messages, which for a
    simulation failure include the per-output mismatch counts and first
    mismatch times. Two attempts sharing this key are functionally the same
    design however differently they are written.
    """
    msgs = sorted(
        d.get("message", "") for d in stage.get("diagnostics", [])
        if d.get("code") != "HINT" or "mismatch" in d.get("message", "").lower()
    )
    return hashlib.sha256(
        (stage_name + "|" + "|".join(msgs)).encode()
    ).hexdigest()[:12]


def _magnitude_from(stage: dict) -> Optional[float]:
    """How badly this stage failed, on a 0..1 scale where lower is better.

    Simulation gives the fraction of mismatching samples. Timing gives the
    violation as a fraction of the clock period -- without this, every timing
    failure scored identically, so rollback could not tell an improving
    attempt from a regressing one and reverted on ties. Timing is the one
    stage with a genuinely continuous metric; discarding it defeats the point
    of having it.
    """
    timing = stage.get("timing") or {}
    wns = timing.get("wns")
    period = timing.get("clock_period_ns")
    if wns is not None and wns < 0 and period:
        # 0.043ns short of a 1.2ns clock -> 0.036. Comparable to a sim
        # mismatch fraction, and clamped so a hopeless design cannot outrank
        # a functional failure.
        return min(1.0, -wns / period)

    from .parsers import Diagnostic as _D
    diags = [_D(d["severity"], d["line"], d["code"], d["message"])
             for d in stage.get("diagnostics", [])]
    return failure_magnitude(diags)


def _progress(attempt: Attempt) -> tuple:
    """Rank an attempt so a regression can be detected.

    Further through the pipeline is better; at the same stage, fewer
    mismatching samples is better.
    """
    rank = STAGE_RANK.get(attempt.failed_stage, 0)
    mag = attempt.magnitude if attempt.magnitude is not None else 1.0
    return (rank, -mag)


def _history_block(attempts: List[Attempt], limit: int = 3) -> str:
    """Compact record of what has already been tried and how it went.

    Without this the model re-proposes fixes it has already made, which is
    exactly the oscillation seen on multi-output FSMs: mismatch counts that
    go down, then up, then back to a previous value.
    """
    prior = [a for a in attempts if not a.passed][-limit:]
    if len(prior) < 2:
        return ""
    lines = []
    for a in prior:
        mag = (f"{a.magnitude:.1%} of samples mismatched"
               if a.magnitude is not None else "did not reach simulation")
        lines.append(f"- attempt {a.index}: failed at {a.failed_stage}, {mag}")
    return (
        "\nAttempts so far:\n" + "\n".join(lines) +
        "\n\nDo not repeat a fix that has already been tried. If the last "
        "change made things worse, reconsider the approach rather than "
        "adjusting it further.\n"
    )


TIMING_SYSTEM_PROMPT = (
    "You are an RTL design engineer closing timing. The design is already "
    "functionally correct and must stay that way: do not change the module "
    "name, port list, or observable behaviour. Reduce the delay on the "
    "critical path by restructuring logic -- balancing a carry chain, "
    "rebalancing a wide mux, precomputing a term, or adding a pipeline stage "
    "only if the specification permits extra latency. Respond with exactly "
    "one fenced code block containing the complete module and nothing else."
)


def _timing_repair_prompt(problem: Problem, rtl: str, feedback: str,
                          history: str = "") -> List[dict]:
    """Repair prompt for a timing violation.

    Deliberately separate from the functional one. "Your design is wrong" and
    "your design is too slow" are different tasks: the first invites changing
    behaviour, which is exactly what must not happen here. Reusing the
    functional prompt gets logic rewritten that was already correct.

    The clock constraint is stated as fixed. The agent has no way to edit the
    SDC -- it is written by the runner each time -- but saying so discourages
    the model from proposing that as the fix.
    """
    return [
        {"role": "system", "content": TIMING_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"Specification for module `{problem.top}`:\n\n{problem.spec}\n\n"
                f"This implementation is functionally correct but fails "
                f"timing at a fixed {problem.clock_period_ns}ns clock period. "
                f"The clock constraint cannot be changed.\n\n"
                f"```verilog\n{rtl}\n```\n\n"
                f"Timing report:\n{feedback}\n"
                f"{history}\n"
                "Restructure the logic on the critical path to reduce its "
                "delay. Keep the module name, ports and behaviour identical."
            ),
        },
    ]


def _repair_prompt(problem: Problem, rtl: str, stage: str,
                   feedback: str, history: str = "") -> List[dict]:
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"Specification for module `{problem.top}`:\n\n{problem.spec}\n\n"
                f"This implementation failed at the {stage} stage:\n\n"
                f"```verilog\n{rtl}\n```\n\n"
                f"Tool diagnostics:\n{feedback}\n"
                f"{history}\n"
                "Fix the underlying cause and return the complete corrected "
                "module. Do not suppress warnings with pragmas and do not "
                "change the module name or port list unless the "
                "specification requires it."
            ),
        },
    ]


def _generate(client, messages, result) -> str:
    """One model call, with truncation surfaced immediately.

    A cut-off response yields half a module, which lint reports as
    "unexpected end of file" and Icarus reports as "Unknown module type".
    Both look like model errors and are not. Catching it here keeps the
    experiment measuring RTL quality rather than token budget.
    """
    text = client.chat(messages)
    if client.last_finish_reason == "length":
        result.truncated = True
    return extract_verilog(text)


def _score_candidate(problem: Problem, rtl: str, workdir: Path,
                     tb: Path, extra: List[Path], strict_lint: bool) -> tuple:
    """Run a candidate through the pipeline and rank it. Higher is better."""
    design = workdir / DESIGN_FILENAME
    design.write_text(rtl)
    for name in SCORING_STAGES:
        if name == "lint":
            sr = runners.lint(design, workdir, strict=strict_lint)
        elif name == "compile":
            sr = runners.compile_rtl(design, workdir, tb=tb, extra=extra)
        elif name == "simulate":
            sr = runners.simulate(workdir, sim_format=problem.sim_format)
        else:
            sr = runners.synthesize(design, workdir, problem.top)
        if sr.skipped:
            continue
        if not sr.passed:
            mag = failure_magnitude(sr.diagnostics)
            return (STAGE_RANK.get(name, 0), -(mag if mag is not None else 1.0))
    return (STAGE_RANK[None], 0.0)


def run_problem(
    problem: Problem,
    client: LLMClient,
    workdir: Path,
    feedback_level: str = "full",
    max_iterations: int = 5,
    strict_lint: bool = False,
    candidates: int = 1,
    through: str = "synth",
    liberty: Optional[Path] = None,
) -> RunResult:
    if feedback_level not in STAGES_BY_LEVEL:
        raise ValueError(
            f"unknown feedback level {feedback_level!r}; "
            f"choose from {sorted(STAGES_BY_LEVEL)}"
        )

    repair_stages = STAGES_BY_LEVEL[feedback_level]
    scoring_stages = FLOW_TIERS.get(through, SCORING_STAGES)
    # Backend stages are only repairable when the level reaches them.
    if "synth" in repair_stages:
        repair_stages = list(repair_stages) + ["map", "sta"]
    liberty = Path(liberty or os.environ.get("RTLFORGE_LIBERTY", ""))
    workdir = runners.make_workdir(Path(workdir))

    # The trusted testbench is copied in fresh each run and never regenerated.
    tb_local = workdir / problem.tb_path.name
    shutil.copy(problem.tb_path, tb_local)
    extra_local = []
    for e in problem.extra_sources:
        dst = workdir / e.name
        shutil.copy(e, dst)
        extra_local.append(dst)

    design = workdir / DESIGN_FILENAME
    result = RunResult(
        problem=problem.name,
        provider=client.provider,
        model=client.model,
        feedback_level=feedback_level,
        passed=False,
        iterations=0,
    )

    start = time.time()
    messages = _generate_prompt(problem)

    # Best-of-N sampling. MAGE reports a large gain from drawing several
    # candidates at higher temperature and keeping the one that gets furthest
    # through the tools -- the tools, not the model, pick the winner. N=1
    # keeps the original single-draw behaviour.
    if candidates > 1:
        saved_temp = client.temperature
        client.temperature = max(saved_temp, 0.8)
        best_rtl_c, best_score = None, None
        for _ in range(candidates):
            cand = _generate(client, messages, result)
            if f"module {problem.top}" not in cand:
                continue
            score = _score_candidate(problem, cand, workdir, tb_local,
                                     extra_local, strict_lint)
            if best_score is None or score > best_score:
                best_rtl_c, best_score = cand, score
        client.temperature = saved_temp
        result.candidates_drawn = candidates
        rtl = best_rtl_c if best_rtl_c else _generate(client, messages, result)
    else:
        rtl = _generate(client, messages, result)
    seen_hashes: set = set()
    # Behavioural fingerprints, not just textual ones. A model can rewrite
    # comments and signal names while producing functionally identical RTL --
    # the hash changes, the behaviour does not, and the loop pays for
    # iterations that cannot help. Observed on multi-output FSMs: three
    # consecutive attempts with distinct hashes and an identical mismatch
    # count.
    seen_behaviour: dict = {}
    best: Optional[Attempt] = None
    best_rtl = rtl

    # "none" is single-shot: generate, score once, never repair.
    iterations = 1 if feedback_level == "none" else max_iterations

    for i in range(1, iterations + 1):
        result.iterations = i
        design.write_text(rtl)
        rtl_hash = hashlib.sha256(rtl.encode()).hexdigest()[:12]

        # Keep every attempt, not just the one that ran last. The failed ones
        # are often the most informative: a timing run that plateaued showed
        # the model reaching its best structure immediately, then repeatedly
        # attempting something more ambitious and mis-writing it. That is only
        # visible if the broken attempts survive.
        (workdir / f"attempt_{i:02d}.v").write_text(rtl)
        attempt = Attempt(index=i, rtl=rtl, rtl_hash=rtl_hash)

        # An identical regeneration means the model has stopped responding to
        # feedback. Further iterations cost tokens and change nothing.
        if rtl_hash in seen_hashes:
            attempt.failed_stage = "generate"
            attempt.stages.append({
                "stage": "generate", "passed": False, "skipped": False,
                "note": "identical to an earlier attempt; loop stalled",
                "diagnostics": [{"severity": "error", "line": None,
                                 "code": "STALLED",
                                 "message": "regenerated identical RTL"}],
                "metrics": None,
            })
            result.attempts.append(attempt)
            result.stop_reason = "stalled"
            break
        seen_hashes.add(rtl_hash)

        # Cheap structural check before invoking any tool. Catches truncated
        # or empty generations with an unambiguous message.
        if f"module {problem.top}" not in rtl or "endmodule" not in rtl:
            note = ("response truncated at the token limit"
                    if client.last_finish_reason == "length"
                    else f"generated text does not define module "
                         f"`{problem.top}` with a matching endmodule")
            attempt.stages.append({
                "stage": "generate", "passed": False, "skipped": False,
                "note": note,
                "diagnostics": [{"severity": "error", "line": None,
                                 "code": "TRUNCATED", "message": note}],
                "metrics": None,
            })
            attempt.passed = False
            result.attempts.append(attempt)
            if feedback_level == "none" or i == iterations:
                break
            rtl = _generate(client, _generate_prompt(problem), result)
            continue

        failed_stage = None
        feedback = ""

        # Score against every stage in the tier, always. The feedback level
        # controls what the model is told; the tier controls how far the
        # design is taken.
        for stage in scoring_stages:
            if stage == "lint":
                sr = runners.lint(design, workdir, strict=strict_lint)
            elif stage == "compile":
                sr = runners.compile_rtl(design, workdir, tb=tb_local,
                                         extra=extra_local)
            elif stage == "simulate":
                sr = runners.simulate(workdir,
                                      sim_format=problem.sim_format)
            elif stage == "synth":
                sr = runners.synthesize(design, workdir, problem.top)
            elif stage == "map":
                sr = backend.techmap(design, workdir, problem.top, liberty)
            elif stage == "sta":
                sr = backend.run_sta(workdir, problem.top, liberty,
                                     problem.clock_port,
                                     problem.clock_period_ns)
            else:
                continue

            attempt.stages.append(sr.as_dict())

            if sr.skipped:
                continue

            # Record measurements before the pass check: a violated WNS is
            # still a measurement, and it is the one you most want in the log.
            #
            # Keep the BEST slack seen, not the last. The loop rolls back to
            # its best attempt internally, so reporting the final attempt's
            # number describes whichever restructuring the model happened to
            # try last -- a run whose best attempt reached -0.322 reported
            # -0.343 because attempt 5 was worse.
            if getattr(sr, "map_metrics", None):
                result.final_area_um2 = sr.map_metrics.area_um2
            if getattr(sr, "timing", None) and sr.timing.wns is not None:
                if (result.final_wns_ns is None
                        or sr.timing.wns > result.final_wns_ns):
                    result.final_wns_ns = sr.timing.wns

            if not sr.passed:
                failed_stage = stage
                feedback = render_feedback(sr.diagnostics)
                if sr.waveform:
                    feedback += (
                        "\n\nSignal trace around the first mismatch "
                        "(_ref is correct, _dut is yours):\n```\n"
                        + sr.waveform + "\n```"
                    )
                break
            if stage == "synth" and sr.metrics:
                result.final_cells = sr.metrics.cells

        attempt.passed = failed_stage is None
        attempt.failed_stage = failed_stage
        if failed_stage and attempt.stages:
            attempt.magnitude = _magnitude_from(attempt.stages[-1])
            attempt.behaviour = _behaviour_key(failed_stage, attempt.stages[-1])
        result.attempts.append(attempt)

        if attempt.passed:
            result.passed = True
            result.stop_reason = "passed"
            break

        # Three attempts with the same observable failure means the loop is
        # exploring rewordings, not fixes.
        if attempt.behaviour:
            seen_behaviour[attempt.behaviour] = \
                seen_behaviour.get(attempt.behaviour, 0) + 1
            if seen_behaviour[attempt.behaviour] >= 3:
                result.stop_reason = "behaviourally stalled"
                break
        if feedback_level == "none":
            result.stop_reason = "single-shot"
            break
        if i == iterations:
            result.stop_reason = "iteration budget"
            break

        # A failure at a stage outside this level's repair set is terminal:
        # the configuration under test has no signal to act on. This is the
        # point of the ablation -- a lint-only agent genuinely cannot see a
        # functional bug, and must be scored as failing when it hits one.
        if failed_stage not in repair_stages:
            result.terminal_stage = failed_stage
            result.stop_reason = "no feedback signal for this stage"
            break

        # Roll back to the best attempt so far if this one regressed. Without
        # this the loop can repair a worse design into an even worse one --
        # observed as attempts that introduce new lint errors after previously
        # passing lint.
        if best is None or _progress(attempt) >= _progress(best):
            best, best_rtl = attempt, rtl
            repair_from, rolled = rtl, False
        else:
            repair_from, rolled = best_rtl, True

        history = _history_block(result.attempts)
        prompt = (
            _timing_repair_prompt(problem, repair_from, feedback, history)
            if failed_stage == "sta"
            else _repair_prompt(problem, repair_from, failed_stage, feedback,
                                history)
        )
        rtl = _generate(client, prompt, result)
        if rolled:
            # Mark the attempt this repair was based on, not the one it replaced.
            attempt.rolled_back = True

    result.wall_clock_s = time.time() - start
    result.prompt_tokens = client.usage.prompt_tokens
    result.completion_tokens = client.usage.completion_tokens
    return result
