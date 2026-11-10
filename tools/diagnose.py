#!/usr/bin/env python3
"""One-shot diagnostic: preflight, run, and a single report to send back.

Replaces the probe-one-thing-per-round-trip pattern. Everything needed to
judge whether the repair loop and its evidence are working lands in one
file:

  * preflight  -- python env, EDA tool versions, Ollama reachable, model
                  present, GPU pinned, loop.py patched, logging fields live
  * per run    -- pass/fail, iterations, stop reason
  * per attempt-- failed stage, full feedback (waveform included), the
                  cone sent to the next prompt, and the RTL the model wrote
  * per problem-- the reference implementation, for side-by-side reading
  * summary    -- how often hints parsed, cones built, traces present

    python tools/diagnose.py                       # discriminating set, 1 trial
    python tools/diagnose.py --trials 2
    python tools/diagnose.py --problems Prob117_circuit9 Prob107_fsm1s
    python tools/diagnose.py --preflight-only

Long runs: launch with nohup so a dropped SSH session does not kill it.
    nohup python tools/diagnose.py --trials 2 > diag.log 2>&1 &
"""

from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROBLEMS = os.path.join(ROOT, "problems", "verilogeval")
MANIFEST = os.path.join(ROOT, "problems", "discriminating.json")
LOOP = os.path.join(ROOT, "rtlforge", "loop.py")

LOOP_MARKERS = {
    "cone_hint import": "from .cone import cone_hint",
    "cone in repair prompt": "cone: str = \"\") -> List[dict]",
    # Either name: the evidence patch renames cone_hint( to
    # repair_evidence(. Checking only the old name produced a false FAIL.
    "evidence computed on simulate": ("cone = cone_hint(",
                                      "cone = repair_evidence("),
    "cone logged": '"cone": a.cone',
    "feedback logged": '"feedback": a.feedback',
    "rtl logged": '"rtl": a.rtl',
    "errors-first feedback": "prioritise_diagnostics(sr.diagnostics)",
    "trace + cone evidence": ("repair_evidence(repair_from",
                              "repair_from, problem.top, feedback,"),
    "no-effect detection": "previous=(",
    "redraw on identical regeneration": "resampled_identical = True",
}

# Ablation arms -> RTLFORGE_EVIDENCE values (see rtlforge/evidence.py).
ARMS = {"on": "1", "off": "0", "cone": "cone", "trace": "trace,noeffect"}

# Source the report quotes in its appendix, so reading the harness never
# needs a separate round trip: (file, kind, needle).
APPENDIX = [
    ("rtlforge/parsers.py", "def", "render_feedback"),
    ("rtlforge/runners.py", "def", "lint"),
    ("rtlforge/loop.py", "around", "regenerated identical RTL"),
    ("rtlforge/loop.py", "around", "seen_behaviour["),
    ("rtlforge/providers.py", "around", "temperature"),
    ("rtlforge/runners.py", "def", "simulate"),
    ("rtlforge/waveform.py", "def", "window_at"),
    ("rtlforge/waveform.py", "def", "first_mismatch_time"),
]


# ------------------------------------------------------------- helpers

def sh(cmd: list[str], timeout: int = 30) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout)
        return p.returncode, (p.stdout + p.stderr).strip()
    except FileNotFoundError:
        return 127, "not found"
    except subprocess.TimeoutExpired:
        return 124, "timed out"


def first_line(text: str) -> str:
    return text.splitlines()[0] if text else ""


# ----------------------------------------------------------- preflight

def preflight(model: str) -> tuple[list[str], bool]:
    """Return (report lines, ok_to_run)."""
    out = ["## Preflight", ""]
    fatal = False

    def row(name: str, ok: bool | None, detail: str) -> None:
        mark = {True: "ok  ", False: "FAIL", None: "warn"}[ok]
        out.append(f"- `{mark}` **{name}**: {detail}")

    row("python", "micromamba" in sys.executable or None,
        sys.executable)

    for tool, args in (("verilator", ["--version"]),
                       ("iverilog", ["-V"]),
                       ("yosys", ["-V"])):
        rc, text = sh([tool] + args)
        row(tool, rc == 0, first_line(text) if rc == 0 else text)
        if rc != 0:
            fatal = True

    rc, text = sh(["iverilog", "-V"])
    m = re.search(r"version\s+(\d+)", text)
    if m and int(m.group(1)) >= 13:
        # Icarus 13 rejects the forward reference to tb_mismatch in the
        # VerilogEval testbenches -- this produced the fake 0/96 before.
        row("iverilog version", False,
            f"v{m.group(1)} breaks VerilogEval testbenches; need 12")
        fatal = True

    base = os.environ.get("OLLAMA_BASE_URL", "")
    row("OLLAMA_BASE_URL", bool(base) or None,
        base or "unset -- providers.py falls back to localhost:11434")
    api = (base or "http://localhost:11434/v1").rsplit("/v1", 1)[0]
    try:
        with urllib.request.urlopen(api + "/api/tags", timeout=5) as resp:
            tags = json.load(resp)
        names = [m["name"] for m in tags.get("models", [])]
        row("ollama server", True, f"{api} up, {len(names)} model(s)")
        have = model in names or f"{model}:latest" in names
        row("model present", have, model if have
            else f"{model} not in {names}")
        fatal |= not have
    except Exception as exc:                                  # noqa: BLE001
        row("ollama server", False, f"{api} unreachable: {exc}")
        fatal = True

    try:
        free_gb = shutil.disk_usage(ROOT).free / 1e9
        row("disk free (repo)", free_gb > 1 or None, f"{free_gb:.1f} GB")
    except OSError as exc:
        row("disk free (repo)", None, str(exc))
    rc, text = sh(["quota", "-s"])
    if rc == 0 and text and "over" in text.lower():
        row("quota", False, first_line(text))
        fatal = True

    cvd = os.environ.get("CUDA_VISIBLE_DEVICES")
    row("CUDA_VISIBLE_DEVICES", bool(cvd) or None,
        cvd or "unset -- ollama may load onto a full GPU")
    ctx = os.environ.get("OLLAMA_CONTEXT_LENGTH")
    row("OLLAMA_CONTEXT_LENGTH", bool(ctx) or None,
        ctx or "unset -- ollama may size context to total node VRAM")

    rc, text = sh(["nvidia-smi", "--query-gpu=index,memory.used,"
                   "memory.total,utilization.gpu",
                   "--format=csv,noheader,nounits"])
    if rc == 0:
        for line in text.splitlines():
            idx, used, total, util = [x.strip() for x in line.split(",")]
            if cvd is None or idx in cvd.split(","):
                free = (int(total) - int(used)) / 1024
                row(f"gpu {idx}", free > 21 or None,
                    f"{free:.1f} GiB free, {util}% util")
    else:
        row("nvidia-smi", None, text)

    try:
        src = open(LOOP).read()
        for name, marker in LOOP_MARKERS.items():
            options = marker if isinstance(marker, tuple) else (marker,)
            present = any(m in src for m in options)
            row(f"loop.py: {name}", present, "present" if present
                else "missing -- run tools/patch_cone_into_loop.py")
            fatal |= not present
    except OSError as exc:
        row("loop.py", False, str(exc))
        fatal = True

    out.append("")
    return out, not fatal


# --------------------------------------------------------------- runs

def problem_list(args) -> list[str]:
    if args.problems:
        return args.problems
    if args.all:
        return sorted(d for d in os.listdir(PROBLEMS)
                      if os.path.exists(os.path.join(PROBLEMS, d, "ref.sv"))
                      and os.path.exists(os.path.join(PROBLEMS, d, "tb.sv")))
    with open(MANIFEST) as fh:
        data = json.load(fh)
    if isinstance(data, dict):
        for key in ("problems", "include", "names"):
            if key in data:
                data = data[key]
                break
    names = []
    for item in data:
        names.append(item if isinstance(item, str)
                     else item.get("name") or item.get("problem"))
    return [n for n in names if n]


_CLI_FLAGS: str | None = None


def cli_supports(flag: str) -> bool:
    """Whether `rtlforge.cli run` accepts a flag (checked once via --help)."""
    global _CLI_FLAGS
    if _CLI_FLAGS is None:
        _, _CLI_FLAGS = sh([sys.executable, "-m", "rtlforge.cli", "run", "--help"],
                           timeout=60)
    return flag in _CLI_FLAGS


def run_one(name: str, args) -> dict:
    cmd = [sys.executable, "-m", "rtlforge.cli", "run",
           "--problem", os.path.join(PROBLEMS, name),
           "--provider", "ollama", "--model", args.model,
           "--level", args.level, "--max-tokens", str(args.max_tokens)]
    if args.reasoning_effort and cli_supports("--reasoning-effort"):
        cmd += ["--reasoning-effort", args.reasoning_effort]
    t0 = time.time()
    try:
        p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                           timeout=args.timeout)
        text = p.stdout + p.stderr
    except subprocess.TimeoutExpired:
        return {"name": name, "error": f"timed out after {args.timeout}s",
                "elapsed": time.time() - t0}

    m = re.search(r"^log:\s*(\S+)", text, re.M)
    if not m:
        return {"name": name, "error": "no log line in CLI output",
                "cli_output": text[-3000:], "elapsed": time.time() - t0}
    logdir = os.path.join(ROOT, m.group(1))
    files = glob.glob(os.path.join(logdir, f"{name}_*.json"))
    if not files:
        return {"name": name, "error": f"no JSON in {logdir}",
                "cli_output": text[-3000:], "elapsed": time.time() - t0}
    data = json.load(open(files[0]))
    records = data if isinstance(data, list) else [data]
    return {"name": name, "record": records[0], "logdir": m.group(1),
            "summary": first_line(text), "elapsed": time.time() - t0}


# ------------------------------------------------------------- report

def fence(text: str, lang: str = "") -> str:
    """Code-fence *text*, using a fence longer than any it contains.

    The feedback embeds the waveform trace in its own ``` block, which
    closed a fixed ``` fence early and broke the rest of the report.
    """
    text = (text or "").rstrip()
    if not text:
        return "_(empty)_"
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    ticks = "`" * max(3, longest + 1)
    return f"{ticks}{lang}\n{text}\n{ticks}"


def _source_excerpt(rel: str, kind: str, needle: str,
                    radius: int = 25) -> str:
    path = os.path.join(ROOT, rel)
    try:
        lines = open(path).read().splitlines()
    except OSError as exc:
        return f"_(unreadable: {exc})_"
    if kind == "def":
        start = next((i for i, l in enumerate(lines)
                      if re.match(rf"def {re.escape(needle)}\(", l)), None)
        if start is None:
            return f"_(no `def {needle}` in {rel})_"
        end = next((i for i in range(start + 1, len(lines))
                    if re.match(r"(def |class |\S)", lines[i])
                    and not lines[i].startswith((" ", "\t", ")"))), len(lines))
        chunk = range(start, min(end, start + 120))
    else:
        hits = [i for i, l in enumerate(lines) if needle in l]
        if not hits:
            return f"_(`{needle}` not found in {rel})_"
        chunk = range(max(0, hits[0] - radius),
                      min(len(lines), hits[0] + radius))
    return fence("\n".join(f"{n + 1:4d}  {lines[n]}" for n in chunk),
                 "python")


def render_appendix() -> list[str]:
    out = ["## Appendix: harness source", ""]
    for rel, kind, needle in APPENDIX:
        out += [f"<details><summary>{rel} — {needle}</summary>", "",
                _source_excerpt(rel, kind, needle), "", "</details>", ""]
    return out


def write_report(path: str, runs: list[dict], pre: list[str], args,
                 started: str, crash: str = "") -> None:
    """Render fully, then atomically replace the report.

    The earlier version opened the file (truncating it) before rendering,
    so any failure mid-write -- an exception, a full quota, a killed job
    -- left an empty report and no trace of why. That is what the
    2026-11-07 evidence-on run produced.
    """
    import traceback
    try:
        text = render(runs, pre, args, started)
    except Exception:                                     # noqa: BLE001
        text = ("# RTLForge diagnostic — RENDER FAILED\n\n"
                + fence(traceback.format_exc()) + "\n\n"
                + f"{len(runs)} run(s) completed; raw records follow.\n\n"
                + fence(json.dumps(runs, indent=1, default=str)[:200000]))
    if crash:
        text += "\n\n## RUN ABORTED\n\n" + fence(crash) + "\n"
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        fh.write(text)
    os.replace(tmp, path)


def render(runs: list[dict], pre: list[str], args, started: str) -> str:
    out = [f"# RTLForge diagnostic — {started}", "",
           f"model `{args.model}`, level `{args.level}`, "
           f"trials {args.trials}, max-tokens {args.max_tokens}, "
           f"evidence **{args.evidence}**", ""]
    out += pre

    # ---- summary
    stats = dict(runs=0, passed=0, errors=0, attempts=0, repairs=0,
                 sim_fail=0, cone=0, hint=0, trace=0, rtl=0, ident=0,
                 lint_fail=0, ev_x=0, ev_div=0, ev_cex=0, ev_unread=0,
                 ev_noop=0, resampled=0, resampled_pass=0)
    rows = []
    for r in runs:
        stats["runs"] += 1
        if "error" in r:
            stats["errors"] += 1
            rows.append(f"| {r['name']} | ERROR | – | – | {r['error']} |")
            continue
        rec = r["record"]
        stats["passed"] += bool(rec.get("passed"))
        atts = rec.get("attempts", [])
        stats["attempts"] += len(atts)
        for a in atts:
            fb = a.get("feedback") or ""
            stats["rtl"] += bool(a.get("rtl"))
            failed_sim = any(s.get("stage") == "simulate"
                             and not s.get("passed")
                             for s in a.get("stages", []))
            if failed_sim:
                stats["sim_fail"] += 1
                stats["hint"] += bool(re.search(
                    r"Output\s*'[^']+'\s*has\s*\d+\s*mismatch", fb))
                stats["trace"] += "Signal trace" in fb
                stats["cone"] += bool(a.get("cone"))
        stats["repairs"] += max(0, len(atts) - 1)
        if any(a.get("resampled") for a in atts):
            stats["resampled"] += 1
            stats["resampled_pass"] += bool(rec.get("passed"))
        if atts and any("identical RTL" in (d.get("message") or "")
                        for s in atts[-1].get("stages", [])
                        for d in s.get("diagnostics", [])):
            stats["ident"] += 1
        for a in atts:
            ev = a.get("cone") or ""
            stats["ev_x"] += "is X (unknown)" in ev
            stats["ev_div"] += "First disagreement" in ev
            stats["ev_cex"] += "Input combinations your design" in ev
            stats["ev_unread"] += "declared but never read" in ev
            stats["ev_noop"] += "had no effect on behaviour" in ev
            stats["lint_fail"] += any(
                s.get("stage") == "lint" and not s.get("passed")
                for s in a.get("stages", []))
        rows.append(
            f"| {r['name']} | {'PASS' if rec.get('passed') else 'FAIL'} "
            f"| {rec.get('iterations')} | {rec.get('stop_reason', '–')} "
            f"| {r['elapsed']:.0f}s |")

    sf = stats["sim_fail"] or 1
    out += ["## Summary", "",
            f"- runs: {stats['runs']}, passed {stats['passed']}, "
            f"harness errors {stats['errors']}",
            f"- attempts: {stats['attempts']} "
            f"({stats['repairs']} repairs), RTL logged on {stats['rtl']}",
            f"- functional failures: {stats['sim_fail']}",
            f"  - mismatch hint present in feedback: "
            f"{stats['hint']}/{stats['sim_fail']} "
            f"({100 * stats['hint'] / sf:.0f}%)",
            f"  - waveform trace present in feedback: "
            f"{stats['trace']}/{stats['sim_fail']} "
            f"({100 * stats['trace'] / sf:.0f}%)",
            f"  - evidence built: {stats['cone']}/{stats['sim_fail']} "
            f"({100 * stats['cone'] / sf:.0f}%)  "
            "_(last attempt of a run gets none: no repair follows)_",
            f"  - of which: X-state {stats['ev_x']}, first-divergence "
            f"{stats['ev_div']}, counterexamples {stats['ev_cex']}, "
            f"unread input {stats['ev_unread']}, "
            f"no-effect {stats['ev_noop']}",
            f"- lint failures: {stats['lint_fail']}",
            f"- failed runs ending in identical regeneration: "
            f"{stats['ident']}/{stats['runs'] - stats['passed']}",
            f"- runs that redrew after a repeat: {stats['resampled']}, "
            f"of which passed: {stats['resampled_pass']}",
            "",
            "| problem | result | iters | stop reason | time |",
            "|---|---|---|---|---|", *rows, ""]

    # ---- detail
    out += ["## Detail", ""]
    refs_done: set[str] = set()
    for r in runs:
        out.append(f"### {r['name']}")
        if "error" in r:
            out += [f"**harness error:** {r['error']}", "",
                    fence(r.get("cli_output", "")), ""]
            continue
        rec = r["record"]
        out.append(f"{r['summary']}  \nlog: `{r['logdir']}`\n")
        if r["name"] not in refs_done:
            refs_done.add(r["name"])
            ref = os.path.join(PROBLEMS, r["name"], "ref.sv")
            if os.path.exists(ref):
                out += ["<details><summary>reference</summary>", "",
                        fence(open(ref).read(), "verilog"), "",
                        "</details>", ""]
        for a in rec.get("attempts", []):
            failed = [s for s in a.get("stages", []) if not s.get("passed")
                      and not s.get("skipped")]
            where = failed[0]["stage"] if failed else "—"
            out.append(f"#### attempt {a.get('index')} — "
                       f"{'pass' if a.get('passed') else 'fail at ' + where}")
            out += ["**rtl**", fence(a.get("rtl"), "verilog"), ""]
            if not a.get("passed"):
                fb = a.get("feedback")
                if not fb:
                    diags = [d.get("message", "") for s in failed
                             for d in s.get("diagnostics", [])]
                    fb = "(feedback not logged; diagnostics:)\n" + \
                        "\n".join(diags)
                out += ["**feedback sent to next prompt**", fence(fb), ""]
                out += ["**cone sent to next prompt**",
                        fence(a.get("cone")), ""]
        out.append("")
    out += render_appendix()
    return "\n".join(out)


# --------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--problems", nargs="*")
    ap.add_argument("--all", action="store_true",
                    help="every problem under problems/verilogeval")
    ap.add_argument("--trials", type=int, default=1)
    ap.add_argument("--model", default="qwen2.5-coder:32b")
    ap.add_argument("--level", default="full")
    ap.add_argument("--max-tokens", type=int, default=3072)
    ap.add_argument("--reasoning-effort", default=None,
                    help="low/medium/high for reasoning models, if the CLI supports it")
    ap.add_argument("--timeout", type=int, default=900,
                    help="per-run ceiling in seconds")
    ap.add_argument("--out", default=None)
    ap.add_argument("--evidence", choices=sorted(ARMS), default="on",
                    help="which evidence the repair prompt gets (ablation arm)")
    ap.add_argument("--preflight-only", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="run even if preflight reports a failure")
    args = ap.parse_args()

    started = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    out_path = args.out or os.path.join(
        ROOT, f"diag_{started}_evidence-{args.evidence}.md")
    os.environ["RTLFORGE_EVIDENCE"] = ARMS[args.evidence]

    pre, ok = preflight(args.model)
    if args.reasoning_effort:
        sup = cli_supports("--reasoning-effort")
        pre.insert(-1, f"- `{'ok  ' if sup else 'warn'}` **reasoning effort**: "
                   + (args.reasoning_effort if sup else
                      f"requested {args.reasoning_effort}, but rtlforge.cli run has "
                      "no --reasoning-effort flag: the model uses its default"))
    print("\n".join(pre))
    if args.preflight_only:
        return 0 if ok else 1
    if not ok and not args.force:
        write_report(out_path, [], pre, args, started)
        print(f"preflight failed -- not running. report: {out_path}")
        print("fix the FAIL lines above, or pass --force")
        return 1

    names = problem_list(args)
    plan = [n for n in names for _ in range(args.trials)]
    runs: list[dict] = []
    dead_streak = 0
    import traceback
    try:
        for i, name in enumerate(plan, 1):
            print(f"[{i}/{len(plan)}] {name} ...", flush=True)
            try:
                r = run_one(name, args)
            except Exception:                             # noqa: BLE001
                r = {"name": name, "elapsed": 0.0,
                     "error": "diagnose crashed on this run",
                     "cli_output": traceback.format_exc()}
            runs.append(r)
            status = r.get("error") or r.get("summary", "")
            print(f"    {status}", flush=True)
            # A dead model server makes every remaining run a harness error;
            # stop instead of spending the night recording them.
            dead = ("Connection refused" in r.get("cli_output", "")
                    or "Max retries exceeded" in r.get("cli_output", ""))
            dead_streak = dead_streak + 1 if dead else 0
            if dead_streak >= 3:
                write_report(out_path, runs, pre, args, started,
                             crash="aborted: model server unreachable for 3 "
                                   "consecutive runs (Connection refused)")
                print(f"\naborted: model server unreachable. partial report: {out_path}")
                return 4
            # Rewrite after every run, so a killed job still leaves
            # everything gathered so far.
            write_report(out_path, runs, pre, args, started)
    except KeyboardInterrupt:
        write_report(out_path, runs, pre, args, started,
                     crash="interrupted (Ctrl-C) -- partial results above")
        print(f"\ninterrupted. partial report: {out_path}")
        return 130

    print(f"\nreport: {out_path}")
    print("send it back with (from your laptop):")
    print(f"  scp -O jeh.19196@10.16.1.50:{out_path} ~/Downloads/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
