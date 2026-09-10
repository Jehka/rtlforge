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
        client = LLMClient(provider=args.provider, model=args.model,
                           max_tokens=args.max_tokens,
                           reasoning_effort=args.reasoning_effort)
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
                client = LLMClient(provider=args.provider, model=args.model,
                                   max_tokens=args.max_tokens,
                                   reasoning_effort=args.reasoning_effort)
                r = run_problem(
                    problem,
                    client,
                    workdir=Path(args.workdir) / f"{problem.name}-{level}-{trial}",
                    feedback_level=level,
                    max_iterations=args.max_iterations,
                    strict_lint=args.strict_lint,
                )
            except LLMError as e:
                # A sweep that crashes on the last trial must not discard the
                # ones that already succeeded.
                print(f"\nAPI error, stopping sweep: {e}", file=sys.stderr)
                (out / "raw.json").write_text(json.dumps(rows, indent=2))
                print(f"{len(rows)} completed run(s) saved to {out}",
                      file=sys.stderr)
                _summarise(rows, args.levels)
                return 3
            rows.append(r.as_dict())
            print(
                f"  {level:8} trial {trial}: "
                f"{'PASS' if r.passed else 'FAIL'} "
                f"in {r.iterations} iter(s), cells={r.final_cells}"
            )
            (out / "raw.json").write_text(json.dumps(rows, indent=2))

    _summarise(rows, args.levels)
    if any(r.get("truncated") for r in rows):
        print(
            "\nWARNING: some responses hit the token ceiling. Those runs "
            "measure output length, not RTL quality -- raise --max-tokens and "
            "re-run before drawing conclusions."
        )
    print(f"\nlog: {out}")
    return 0


def _summarise(rows, levels) -> None:
    if not rows:
        return
    print("\n=== summary ===")
    for level in levels:
        sub = [r for r in rows if r["feedback_level"] == level]
        if not sub:
            continue
        passes = sum(r["passed"] for r in sub)
        mean_iters = sum(r["iterations"] for r in sub) / len(sub)
        trunc = sum(r.get("truncated", False) for r in sub)
        warn = f"  [TRUNCATED in {trunc}]" if trunc else ""
        print(
            f"{level:8}  pass {passes}/{len(sub)}  "
            f"mean iters {mean_iters:.1f}{warn}"
        )

def cmd_report(args) -> int:
    """Merge raw.json files from several sweeps and summarise together.

    Free-tier daily caps force data collection across multiple sessions.
    Without this, each day's partial sweep looks like its own underpowered
    experiment instead of part of one dataset.
    """
    rows = []
    for path in args.inputs:
        p = Path(path)
        files = sorted(p.rglob("raw.json")) if p.is_dir() else [p]
        for f in files:
            try:
                rows.extend(json.loads(f.read_text()))
            except (OSError, json.JSONDecodeError) as e:
                print(f"skipping {f}: {e}", file=sys.stderr)

    if not rows:
        print("no runs found", file=sys.stderr)
        return 1

    # Truncated runs measured output length, not RTL quality. Excluding them
    # is the honest default; --include-truncated overrides for inspection.
    total = len(rows)
    if not args.include_truncated:
        rows = [r for r in rows if not r.get("truncated")]
        dropped = total - len(rows)
        if dropped:
            print(f"excluded {dropped} truncated run(s) of {total}")

    models = sorted({r.get("model", "?") for r in rows})
    problems = sorted({r.get("problem", "?") for r in rows})
    print(f"models: {', '.join(models)}")
    print(f"problems: {', '.join(problems)}")

    levels = sorted({r["feedback_level"] for r in rows},
                    key=lambda l: list(STAGES_BY_LEVEL).index(l)
                    if l in STAGES_BY_LEVEL else 99)
    _summarise(rows, levels)

    # Where runs die is the interesting part; aggregate pass rates hide it.
    print("\n=== first failing stage ===")
    for level in levels:
        sub = [r for r in rows if r["feedback_level"] == level and not r["passed"]]
        if not sub:
            continue
        tally = {}
        for r in sub:
            last = r["attempts"][-1] if r["attempts"] else None
            stage = "?"
            if last:
                failed = [s["stage"] for s in last["stages"] if not s["passed"]]
                stage = failed[0] if failed else "?"
            tally[stage] = tally.get(stage, 0) + 1
        detail = ", ".join(f"{k}={v}" for k, v in sorted(tally.items()))
        print(f"{level:8}  {detail}")
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
        sp.add_argument("--reasoning-effort", default=None,
                        choices=["low", "medium", "high"],
                        help="gpt-oss models only; 'low' cuts token use ~3x")
        sp.add_argument("--max-tokens", type=int, default=8192,
                        help="per-response ceiling; also reserved against "
                             "tokens-per-minute, so lower it if you see 429s")

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

    sp = sub.add_parser("report", help="merge and summarise sweep results")
    sp.add_argument("inputs", nargs="+",
                    help="raw.json files or directories to search")
    sp.add_argument("--include-truncated", action="store_true",
                    help="keep runs that hit the token ceiling")
    sp.set_defaults(func=cmd_report)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
