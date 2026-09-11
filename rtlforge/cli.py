"""Command line entry point.

    python -m rtlforge.cli run   --problem problems/counter --level full
    python -m rtlforge.cli sweep --problem problems/counter --trials 3
    python -m rtlforge.cli check --problem problems/counter --rtl my.v
"""

from __future__ import annotations

import argparse
import json
import random
import re
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
    extra = []
    for e in problem.extra_sources:
        dst = work / e.name
        shutil.copy(e, dst)
        extra.append(dst)

    ok = True
    for name, fn in [
        ("lint", lambda: runners.lint(design, work)),
        ("compile", lambda: runners.compile_rtl(design, work, tb=tb,
                                                extra=extra)),
        ("simulate", lambda: runners.simulate(
            work, sim_format=problem.sim_format)),
        ("synth", lambda: runners.synthesize(design, work, problem.top)),
    ]:
        sr = fn()
        status = "SKIP" if sr.skipped else ("PASS" if sr.passed else "FAIL")
        # NB: do not name this `extra` -- the lambdas above close over the
        # extra-sources list by reference and would see the string instead.
        detail = ""
        if sr.metrics and sr.metrics.cells is not None:
            detail = f"  cells={sr.metrics.cells}"
        print(f"[{status:4}] {name}{detail}{'  ' + sr.note if sr.note else ''}")
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

def cmd_import_verilogeval(args) -> int:
    """Convert a VerilogEval spec-to-rtl checkout into RTLForge problems.

    Each problem there is three files: a prompt, a golden RefModule, and a
    testbench that instantiates both and reports "Mismatches: N in M samples".
    Using these instead of hand-written oracles removes the single largest
    source of error in this project -- see problems/counter/tb.v, which was
    wrong twice -- and makes results comparable to published work.
    """
    src = Path(args.dataset)
    if not src.is_dir():
        print(f"not a directory: {src}", file=sys.stderr)
        return 1

    prompts = sorted(src.glob("*_prompt.txt"))
    if not prompts:
        print(
            f"no *_prompt.txt in {src}. Point --dataset at the "
            "dataset_spec-to-rtl folder of an NVlabs/verilog-eval checkout.",
            file=sys.stderr,
        )
        return 1

    dest_root = Path(args.into)
    dest_root.mkdir(parents=True, exist_ok=True)
    made = 0

    for prompt in prompts:
        stem = prompt.name[: -len("_prompt.txt")]
        ref = src / f"{stem}_ref.sv"
        test = src / f"{stem}_test.sv"
        if not (ref.exists() and test.exists()):
            print(f"skipping {stem}: missing ref or test", file=sys.stderr)
            continue
        if args.limit and made >= args.limit:
            break

        d = dest_root / stem
        d.mkdir(parents=True, exist_ok=True)
        shutil.copy(prompt, d / "spec.md")
        shutil.copy(ref, d / "ref.sv")
        shutil.copy(test, d / "tb.sv")
        (d / "problem.json").write_text(json.dumps({
            "top": "TopModule",
            "testbench": "tb.sv",
            "extra_sources": ["ref.sv"],
            "sim_format": "verilogeval",
            "source": "VerilogEval spec-to-rtl",
            "problem_id": stem,
        }, indent=2))
        made += 1

    print(f"imported {made} problem(s) into {dest_root}")
    print(
        "Note: these testbenches compare against a golden RefModule, so they "
        "are trustworthy without the hand-validation our own oracles needed. "
        "Synthesis still runs, but VerilogEval scores functional equivalence "
        "only."
    )
    return 0


def cmd_selftest(args) -> int:
    """Run each problem's own reference design through the pipeline.

    A reference that fails its own testbench means the toolchain cannot handle
    that problem -- usually an unsupported SystemVerilog construct. Scoring a
    model on it would record a failure that has nothing to do with the model.
    Run this once after importing, and exclude what it reports.
    """
    root = Path(args.problems)
    dirs = sorted(d for d in root.iterdir()
                  if d.is_dir() and (d / "problem.json").exists())
    if not dirs:
        print(f"no problems under {root}", file=sys.stderr)
        return 1

    usable, broken, spec_gap = [], [], []
    for d in dirs:
        try:
            problem = Problem(d)
        except (OSError, ValueError, KeyError) as e:
            broken.append((d.name, f"load error: {e}"))
            continue

        ref = d / "ref.sv"
        if not ref.exists():
            # Our hand-written problems have no reference; skip quietly.
            continue

        work = Path(args.workdir) / "selftest" / d.name
        work.mkdir(parents=True, exist_ok=True)
        design = work / "design.v"
        # The reference IS the correct answer, renamed to the expected top.
        design.write_text(
            re.sub(r"\bRefModule\b", problem.top, ref.read_text())
        )
        tb = work / problem.tb_path.name
        shutil.copy(problem.tb_path, tb)
        extras = []
        for e in problem.extra_sources:
            dst = work / e.name
            shutil.copy(e, dst)
            extras.append(dst)

        # A reference always passes its own testbench, so that check alone
        # cannot tell whether the PROMPT is sufficient to reach it. When the
        # reference relies on an `initial` block for power-on state and the
        # prompt never mentions an initial value, reset, or power-on
        # behaviour, a spec-compliant design starts at x and mismatches on
        # the first samples. The problem is then unwinnable from its own
        # prompt, and every failure recorded on it belongs to the benchmark,
        # not the model. (VerilogEval spec-to-rtl: 3 of 156.)
        ref_text = ref.read_text()
        spec_text = (d / "spec.md").read_text() if (d / "spec.md").exists() else ""
        if re.search(r"^\s*initial\b", ref_text, re.MULTILINE) and not re.search(
            r"initial|reset|power[- ]?on|starts? (?:at|in)", spec_text, re.I
        ):
            spec_gap.append(d.name)
            continue

        c = runners.compile_rtl(design, work, tb=tb, extra=extras)
        if not c.passed:
            msg = c.diagnostics[0].message if c.diagnostics else "compile failed"
            broken.append((d.name, f"compile: {msg}"))
            continue
        sim = runners.simulate(work, sim_format=problem.sim_format)
        if sim.passed:
            usable.append(d.name)
        else:
            msg = sim.diagnostics[0].message if sim.diagnostics else "sim failed"
            broken.append((d.name, f"simulate: {msg}"))

    print(f"usable: {len(usable)}   unusable: {len(broken)}   "
          f"spec gap: {len(spec_gap)}")
    for name, why in broken:
        print(f"  {name}: {why[:100]}")
    if spec_gap:
        print("\nexcluded -- reference needs power-on state the prompt never "
              "specifies, so no spec-compliant design can pass:")
        for name in spec_gap:
            print(f"  {name}")

    if args.write_manifest:
        out = root / "usable.json"
        out.write_text(json.dumps(sorted(usable), indent=2))
        print(f"\nwrote {out}")
    return 0


def cmd_benchmark(args) -> int:
    """Sweep many problems. This is how a VerilogEval-style pass rate is made.

    Two properties matter more than speed here:

    * Every completed run is written to disk immediately, and a re-run with
      --resume skips work already done. Free-tier daily caps mean a 155-problem
      benchmark spans several sessions; losing a session's work to a 429 on the
      last problem is unacceptable.
    * Rate-limit failures stop the benchmark cleanly rather than being recorded
      as model failures. A 429 says nothing about the RTL.
    """
    root = Path(args.problems)
    names = None
    manifest = Path(args.manifest) if args.manifest else root / "usable.json"
    if args.manifest or (args.use_manifest and manifest.exists()):
        if not manifest.exists():
            print(f"manifest not found: {manifest}", file=sys.stderr)
            return 1
        names = set(json.loads(manifest.read_text()))
        print(f"using manifest {manifest}: {len(names)} problem(s)")

    dirs = [d for d in sorted(root.iterdir())
            if d.is_dir() and (d / "problem.json").exists()
            and (names is None or d.name in names)]
    if not dirs:
        print(f"no problems under {root}", file=sys.stderr)
        return 1

    if args.shuffle:
        # A fixed seed keeps partial runs comparable across sessions while
        # avoiding a benchmark that only ever covers alphabetically early
        # (and easier) problems before the quota runs out.
        random.Random(args.seed).shuffle(dirs)
    if args.limit:
        dirs = dirs[: args.limit]

    out = Path(args.out) if args.out else _results_dir(Path(args.results_root))
    out.mkdir(parents=True, exist_ok=True)
    raw = out / "raw.json"

    rows = []
    done = set()
    if args.resume and raw.exists():
        rows = json.loads(raw.read_text())
        done = {(r["problem"], r["feedback_level"], r.get("trial", 1))
                for r in rows}
        print(f"resuming: {len(rows)} run(s) already complete")

    planned = [
        (d, lvl, t)
        for d in dirs
        for lvl in args.levels
        for t in range(1, args.trials + 1)
        if (d.name, lvl, t) not in done
    ]
    print(f"{len(planned)} run(s) to do across {len(dirs)} problem(s)\n")

    for n, (d, level, trial) in enumerate(planned, 1):
        try:
            problem = Problem(d)
            client = LLMClient(provider=args.provider, model=args.model,
                               max_tokens=args.max_tokens,
                               reasoning_effort=args.reasoning_effort)
            r = run_problem(
                problem, client,
                workdir=Path(args.workdir) / f"{d.name}-{level}-{trial}",
                feedback_level=level,
                max_iterations=args.max_iterations,
                strict_lint=args.strict_lint,
            )
        except LLMError as e:
            print(f"\nAPI error after {len(rows)} run(s), stopping: {e}",
                  file=sys.stderr)
            raw.write_text(json.dumps(rows, indent=2))
            print(f"progress saved. Resume with:\n"
                  f"  python -m rtlforge.cli benchmark --problems {root} "
                  f"--out {out} --resume ...", file=sys.stderr)
            _summarise_benchmark(rows, args.levels)
            return 3
        except (OSError, ValueError, KeyError) as e:
            print(f"  [{n}/{len(planned)}] {d.name} {level}: skipped ({e})")
            continue

        row = r.as_dict()
        row["trial"] = trial
        rows.append(row)
        raw.write_text(json.dumps(rows, indent=2))
        flag = " TRUNC" if row.get("truncated") else ""
        print(f"  [{n}/{len(planned)}] {d.name:34} {level:5} "
              f"{'PASS' if r.passed else 'FAIL'} "
              f"iters={r.iterations}{flag}")

    _summarise_benchmark(rows, args.levels)
    print(f"\nlog: {out}")
    return 0


def _summarise_benchmark(rows, levels) -> None:
    """Pass rate per level, plus where failures land."""
    if not rows:
        return
    clean = [r for r in rows if not r.get("truncated")]
    dropped = len(rows) - len(clean)
    print("\n=== benchmark summary ===")
    if dropped:
        print(f"(excluded {dropped} truncated run(s))")

    for level in levels:
        sub = [r for r in clean if r["feedback_level"] == level]
        if not sub:
            continue
        passes = sum(r["passed"] for r in sub)
        probs = {r["problem"] for r in sub}
        solved = {r["problem"] for r in sub if r["passed"]}
        mean_iters = sum(r["iterations"] for r in sub) / len(sub)
        print(f"{level:8}  {passes}/{len(sub)} runs "
              f"({100*passes/len(sub):.0f}%)  "
              f"{len(solved)}/{len(probs)} problems  "
              f"mean iters {mean_iters:.2f}")

    # Why runs ended -- stall and oscillation are the interesting failures.
    reasons = {}
    for r in clean:
        if not r["passed"]:
            reasons[r.get("stop_reason") or "?"] = \
                reasons.get(r.get("stop_reason") or "?", 0) + 1
    if reasons:
        print("\nfailure modes: " +
              ", ".join(f"{k}={v}" for k, v in sorted(reasons.items())))

    # Which problems feedback rescued: failed single-shot, passed with the loop.
    if "none" in levels and len(levels) > 1:
        base = {r["problem"] for r in clean
                if r["feedback_level"] == "none" and r["passed"]}
        for level in levels:
            if level == "none":
                continue
            sub = [r for r in clean if r["feedback_level"] == level]
            got = {r["problem"] for r in sub if r["passed"]}
            # A "rescue" that passed on attempt 1 saw no feedback at all --
            # it is sampling noise, not the repair loop working. Separating
            # these is the difference between a real effect and an inflated one.
            repaired = {r["problem"] for r in sub
                        if r["passed"] and r["iterations"] > 1}
            noise = sorted((got - base) - repaired)
            if noise:
                print(f"\nNOTE: {len(noise)} apparent rescue(s) under {level} "
                      f"passed on attempt 1 with no feedback delivered "
                      f"(sampling noise, not repair): {', '.join(noise[:6])}")
            rescued = sorted((got - base) & repaired)
            lost = sorted(base - got)
            if rescued:
                print(f"\nrepaired by {level} ({len(rescued)}): "
                      f"{', '.join(rescued[:8])}"
                      + (" ..." if len(rescued) > 8 else ""))
            if lost:
                print(f"regressed under {level} ({len(lost)}): "
                      f"{', '.join(lost[:8])}"
                      + (" ..." if len(lost) > 8 else ""))


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

    sp = sub.add_parser("import-verilogeval",
                        help="convert VerilogEval problems into RTLForge form")
    sp.add_argument("--dataset", required=True,
                    help="path to dataset_spec-to-rtl")
    sp.add_argument("--into", default="problems/verilogeval")
    sp.add_argument("--limit", type=int, default=0,
                    help="import only the first N problems (0 = all)")
    sp.set_defaults(func=cmd_import_verilogeval)

    sp = sub.add_parser("benchmark", help="sweep many problems at once")
    sp.add_argument("--problems", required=True)
    sp.add_argument("--levels", nargs="+", default=["none", "full"])
    sp.add_argument("--trials", type=int, default=1)
    sp.add_argument("--limit", type=int, default=0)
    sp.add_argument("--shuffle", action="store_true",
                    help="sample across the set rather than alphabetically")
    sp.add_argument("--seed", type=int, default=0)
    sp.add_argument("--use-manifest", action="store_true", default=True,
                    help="honour usable.json from selftest")
    sp.add_argument("--manifest", default=None,
                    help="JSON list of problem names to run instead of "
                         "usable.json; use to focus a limited token budget "
                         "on problems that actually discriminate")
    sp.add_argument("--out", default=None,
                    help="reuse an existing results dir (with --resume)")
    sp.add_argument("--resume", action="store_true")
    sp.add_argument("--workdir", default="work")
    sp.add_argument("--results-root", default=".")
    sp.add_argument("--provider", default="groq",
                    choices=["groq", "gemini", "ollama"])
    sp.add_argument("--model", default=None)
    sp.add_argument("--max-iterations", type=int, default=5)
    sp.add_argument("--strict-lint", action="store_true")
    sp.add_argument("--reasoning-effort", default=None,
                    choices=["low", "medium", "high"])
    sp.add_argument("--max-tokens", type=int, default=8192)
    sp.set_defaults(func=cmd_benchmark)

    sp = sub.add_parser("selftest",
                        help="check which problems this toolchain can run")
    sp.add_argument("--problems", required=True,
                    help="directory containing problem folders")
    sp.add_argument("--workdir", default="work")
    sp.add_argument("--write-manifest", action="store_true",
                    help="write usable.json listing problems that pass")
    sp.set_defaults(func=cmd_selftest)

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
