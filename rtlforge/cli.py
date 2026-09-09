"""Command line entry point.

    python -m rtlforge.cli run   --problem problems/counter --level full
    python -m rtlforge.cli sweep --problem problems/counter --trials 3
    python -m rtlforge.cli check --problem problems/counter --rtl my.v
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

from . import runners
from .loop import Problem, STAGES_BY_LEVEL, run_problem
from .providers import LLMClient, LLMError


def _results_dir(root: Path) -> Path:
    d = root / "results" / datetime.now().strftime("%Y%m%d-%H%M%S")
    d.mkdir(parents=True, exist_ok=True)
    return d


def cmd_check(args) -> int:
    """Run the EDA pipeline on a hand-written file. No LLM involved.

    This is the harness self-test: if this does not work, nothing downstream
    will, and debugging it with a model in the loop is far harder.
    """
    problem = Problem(Path(args.problem))
    work = Path(args.workdir) / "check"
    work.mkdir(parents=True, exist_ok=True)

    design = work / "design.v"
    shutil.copy(args.rtl, design)
    tb = work / problem.tb_path.name
    shutil.copy(problem.tb_path, tb)

    ok = True
    for name, fn in [
        ("lint", lambda: runners.lint(design, work)),
        ("compile", lambda: runners.compile_rtl(design, work, tb=tb)),
        ("simulate", lambda: runners.simulate(work)),
        ("synth", lambda: runners.synthesize(design, work, problem.top)),
    ]:
        sr = fn()
        status = "SKIP" if sr.skipped else ("PASS" if sr.passed else "FAIL")
        extra = ""
        if sr.metrics and sr.metrics.cells is not None:
            extra = f"  cells={sr.metrics.cells}"
        print(f"[{status:4}] {name}{extra}{'  ' + sr.note if sr.note else ''}")
        for d in sr.diagnostics:
            print(f"         {d.render()}")
        if not sr.passed and not sr.skipped:
            ok = False
            break
    return 0 if ok else 1


def cmd_run(args) -> int:
    problem = Problem(Path(args.problem))
    try:
        client = LLMClient(provider=args.provider, model=args.model)
    except LLMError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    result = run_problem(
        problem,
        client,
        workdir=Path(args.workdir) / problem.name,
        feedback_level=args.level,
        max_iterations=args.max_iterations,
        strict_lint=args.strict_lint,
    )

    out = _results_dir(Path(args.results_root))
    (out / f"{problem.name}_{args.level}.json").write_text(
        json.dumps(result.as_dict(), indent=2)
    )
    verdict = "PASS" if result.passed else "FAIL"
    print(
        f"{verdict}  {problem.name}  level={args.level}  "
        f"iters={result.iterations}  cells={result.final_cells}  "
        f"tokens={result.prompt_tokens}+{result.completion_tokens}  "
        f"{result.wall_clock_s:.1f}s"
    )
    print(f"log: {out}")
    return 0 if result.passed else 1


def cmd_sweep(args) -> int:
    """Run every feedback level N times. This is the actual experiment."""
    problem = Problem(Path(args.problem))
    out = _results_dir(Path(args.results_root))
    rows = []

    for level in args.levels:
        for trial in range(1, args.trials + 1):
            try:
                client = LLMClient(provider=args.provider, model=args.model)
            except LLMError as e:
                print(f"error: {e}", file=sys.stderr)
                return 2
            r = run_problem(
                problem,
                client,
                workdir=Path(args.workdir) / f"{problem.name}-{level}-{trial}",
                feedback_level=level,
                max_iterations=args.max_iterations,
                strict_lint=args.strict_lint,
            )
            rows.append(r.as_dict())
            print(
                f"  {level:8} trial {trial}: "
                f"{'PASS' if r.passed else 'FAIL'} "
                f"in {r.iterations} iter(s), cells={r.final_cells}"
            )
            (out / "raw.json").write_text(json.dumps(rows, indent=2))

    print("\n=== summary ===")
    for level in args.levels:
        sub = [r for r in rows if r["feedback_level"] == level]
        if not sub:
            continue
        passes = sum(r["passed"] for r in sub)
        mean_iters = sum(r["iterations"] for r in sub) / len(sub)
        print(
            f"{level:8}  pass {passes}/{len(sub)}  "
            f"mean iters {mean_iters:.1f}"
        )
    print(f"\nlog: {out}")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="rtlforge")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("--problem", required=True)
        sp.add_argument("--workdir", default="work")
        sp.add_argument("--results-root", default=".")
        sp.add_argument("--provider", default="groq",
                        choices=["groq", "gemini", "ollama"])
        sp.add_argument("--model", default=None)
        sp.add_argument("--max-iterations", type=int, default=5)
        sp.add_argument("--strict-lint", action="store_true",
                        help="treat lint warnings as failures")

    sp = sub.add_parser("check", help="run EDA stages on an existing file")
    sp.add_argument("--problem", required=True)
    sp.add_argument("--rtl", required=True)
    sp.add_argument("--workdir", default="work")
    sp.set_defaults(func=cmd_check)

    sp = sub.add_parser("run", help="one problem, one feedback level")
    common(sp)
    sp.add_argument("--level", default="full", choices=sorted(STAGES_BY_LEVEL))
    sp.set_defaults(func=cmd_run)

    sp = sub.add_parser("sweep", help="all feedback levels, N trials each")
    common(sp)
    sp.add_argument("--levels", nargs="+",
                    default=["none", "lint", "compile", "sim", "full"])
    sp.add_argument("--trials", type=int, default=3)
    sp.set_defaults(func=cmd_sweep)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
