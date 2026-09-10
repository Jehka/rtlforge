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

import json
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from . import runners
from .parsers import render_feedback
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
    prompt_tokens: int = 0
    completion_tokens: int = 0
    final_cells: Optional[int] = None

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
            "terminal_stage": self.terminal_stage,
            "attempts": [
                {"index": a.index, "passed": a.passed, "stages": a.stages}
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


def _repair_prompt(problem: Problem, rtl: str, stage: str,
                   feedback: str) -> List[dict]:
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"Specification for module `{problem.top}`:\n\n{problem.spec}\n\n"
                f"This implementation failed at the {stage} stage:\n\n"
                f"```verilog\n{rtl}\n```\n\n"
                f"Tool diagnostics:\n{feedback}\n\n"
                "Fix the underlying cause and return the complete corrected "
                "module. Do not suppress warnings with pragmas and do not "
                "change the module name or port list unless the "
                "specification requires it."
            ),
        },
    ]


def run_problem(
    problem: Problem,
    client: LLMClient,
    workdir: Path,
    feedback_level: str = "full",
    max_iterations: int = 5,
    strict_lint: bool = False,
) -> RunResult:
    if feedback_level not in STAGES_BY_LEVEL:
        raise ValueError(
            f"unknown feedback level {feedback_level!r}; "
            f"choose from {sorted(STAGES_BY_LEVEL)}"
        )

    repair_stages = STAGES_BY_LEVEL[feedback_level]
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)

    # The trusted testbench is copied in fresh each run and never regenerated.
    tb_local = workdir / problem.tb_path.name
    shutil.copy(problem.tb_path, tb_local)

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
    rtl = extract_verilog(client.chat(messages))

    # "none" is single-shot: generate, score once, never repair.
    iterations = 1 if feedback_level == "none" else max_iterations

    for i in range(1, iterations + 1):
        result.iterations = i
        design.write_text(rtl)
        attempt = Attempt(index=i, rtl=rtl)

        failed_stage = None
        feedback = ""

        # Score against every stage, always.
        for stage in SCORING_STAGES:
            if stage == "lint":
                sr = runners.lint(design, workdir, strict=strict_lint)
            elif stage == "compile":
                sr = runners.compile_rtl(design, workdir, tb=tb_local)
            elif stage == "simulate":
                sr = runners.simulate(workdir)
            elif stage == "synth":
                sr = runners.synthesize(design, workdir, problem.top)
            else:
                continue

            attempt.stages.append(sr.as_dict())

            if sr.skipped:
                continue
            if not sr.passed:
                failed_stage = stage
                feedback = render_feedback(sr.diagnostics)
                break
            if stage == "synth" and sr.metrics:
                result.final_cells = sr.metrics.cells

        attempt.passed = failed_stage is None
        result.attempts.append(attempt)

        if attempt.passed:
            result.passed = True
            break
        if feedback_level == "none" or i == iterations:
            break

        # A failure at a stage outside this level's repair set is terminal:
        # the configuration under test has no signal to act on. This is the
        # point of the ablation -- a lint-only agent genuinely cannot see a
        # functional bug, and must be scored as failing when it hits one.
        if failed_stage not in repair_stages:
            result.terminal_stage = failed_stage
            break

        rtl = extract_verilog(
            client.chat(_repair_prompt(problem, rtl, failed_stage, feedback))
        )

    result.wall_clock_s = time.time() - start
    result.prompt_tokens = client.usage.prompt_tokens
    result.completion_tokens = client.usage.completion_tokens
    return result
