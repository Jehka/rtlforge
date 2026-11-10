#!/usr/bin/env python3
"""M1a: can the model answer a narrow spec question it got wrong as a design?

For each testbench-failing design in the reports, formal comparison with
the reference gives a concrete input sequence where the two disagree. That
disagreement is put to the model as an A/B question about the spec, with
the reference's side randomised. Accuracy is measured against chance (50%)
-- the model's design-level accuracy on these same problems was 0%: every
design here failed its testbench.

    python tools/spec_query_eval.py diag_*_evidence-*.md --model qwen2.5-coder:32b
    python tools/spec_query_eval.py REPORTS... --per-problem 5 --limit 40   # smoke test

Requires problems/verilogeval/<name>/spec.md (the prompt needs the spec),
a running Ollama (OLLAMA_BASE_URL), and Yosys.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import os
import random
import re
import sys
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

from contract_check import PROBLEMS, REPORT_REFS, parse_report   # noqa: E402
from rtlforge.formal import distinguish, render_ab_question        # noqa: E402

SYSTEM = ("You are a hardware verification engineer. You decide what a "
          "specification requires. You do not write code.")
INSTRUCTION = ("Which candidate's output does the specification require at "
               "that cycle? Reply with exactly one word on the first line -- "
               "A, B, or NEITHER -- then one sentence of justification.")

_ANSWER = re.compile(r"^\W*(A|B|NEITHER)\b", re.I)


def parse_answer(text: str) -> str:
    """First-line A / B / NEITHER; anything else is UNPARSED."""
    for line in (text or "").splitlines():
        if line.strip():
            m = _ANSWER.match(line.strip())
            return m.group(1).upper() if m else "UNPARSED"
    return "UNPARSED"


def classify(text: str, finish_reason: str | None) -> str:
    """A / B / NEITHER, or why there is no answer.

    qwen3.8 at reasoning effort low returned empty content on 36 of 263
    questions (2026-11-09): the thinking used the whole token budget. Those
    are truncations, not wrong answers, and are reported separately.
    """
    if (text or "").startswith("ERROR"):
        return "ERROR"
    if not (text or "").strip():
        return "TRUNCATED" if finish_reason == "length" else "EMPTY"
    return parse_answer(text)


def server_up(base_url: str | None) -> bool:
    import urllib.request
    url = (base_url or "http://localhost:11434/v1").rsplit("/v1", 1)[0] + "/api/tags"
    try:
        with urllib.request.urlopen(url, timeout=5):
            return True
    except Exception:                                      # noqa: BLE001
        return False


#: Consecutive failed calls before the run aborts instead of recording
#: hundreds of errors as answers (the gpt-oss run of 2026-11-09).
MAX_CONSECUTIVE_ERRORS = 3


def build_prompt(spec: str, question: str) -> list[dict]:
    return [{"role": "system", "content": SYSTEM},
            {"role": "user", "content":
             f"Specification:\n\n{spec.strip()}\n\n{question}\n\n{INSTRUCTION}"}]


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    r = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return ((c - r) / d, (c + r) / d)


def _formal(args):
    ref, rtl, depth = args
    return distinguish(ref, rtl, top_gold="RefModule", depth=depth)


def collect(reports, per_problem, depth, jobs, seed, attempts=False, skip_noreset_init=False):
    """Questions from failed designs.

    attempts=False: the final design of each failed run (the original M1a).
    attempts=True: every attempt that compiled but failed simulation,
    including ones later repaired -- a strong generator rarely ends a run
    failed, but its intermediate designs still hold its own mistakes.
    """
    runs = []
    for p in reports:
        arm, rs = parse_report(p)
        if attempts:
            runs += [dict(r, arm=arm, rtl=a["rtl"], attempt=a["n"]) for r in rs
                     for a in r.get("attempts", [])
                     if a["status"] == "fail at simulate" and a["rtl"].strip()]
        else:
            runs += [dict(r, arm=arm) for r in rs if not r["passed"] and r["rtl"].strip()]
    refs, specs = {}, {}
    for name in {r["name"] for r in runs}:
        rp = os.path.join(PROBLEMS, name, "ref.sv")
        refs[name] = open(rp).read() if os.path.exists(rp) else REPORT_REFS.get(name)
        sp = os.path.join(PROBLEMS, name, "spec.md")
        specs[name] = open(sp).read() if os.path.exists(sp) else None

    unique, rng = {}, random.Random(seed)
    for r in runs:
        if refs.get(r["name"]) and specs.get(r["name"]):
            k = (r["name"], hashlib.sha256(r["rtl"].encode()).hexdigest())
            unique.setdefault(k, r)
    # cap per problem so a few hard problems with many failing variants
    # cannot dominate the accuracy figure
    by_prob = defaultdict(list)
    for k in unique:
        by_prob[k[0]].append(k)
    keys = []
    for name, ks in sorted(by_prob.items()):
        rng.shuffle(ks)
        keys += ks[:per_problem]

    with ProcessPoolExecutor(max_workers=jobs) as pool:
        ds = list(pool.map(_formal, [(refs[k[0]], unique[k]["rtl"], depth) for k in keys],
                           chunksize=2))
    items, skipped_noreset = [], 0
    for k, d in zip(keys, ds):
        if d.verdict not in ("logic", "init-only"):
            continue
        if skip_noreset_init and d.verdict == "init-only" and not d.resets:
            # power-up value of a design with no reset: the spec cannot decide
            skipped_noreset += 1
            continue
        q, gold = render_ab_question(d, gold_is_a=rng.random() < 0.5)
        fd = d.counterexample.first_difference()
        items.append({"problem": k[0], "arm": unique[k]["arm"],
                      "attempt": unique[k].get("attempt"),
                      "verdict": d.verdict, "question": q, "gold": gold,
                      "cycles": fd.t if fd else None, "spec": specs[k[0]]})
    skipped = {"no spec": sum(1 for n in {r["name"] for r in runs} if not specs.get(n)),
               "formal not a difference": len(keys) - len(items) - skipped_noreset,
               "no-reset power-up (skipped)": skipped_noreset}
    return items, skipped


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("reports", nargs="+")
    ap.add_argument("--model", default="qwen2.5-coder:32b")
    ap.add_argument("--provider", default="ollama")
    ap.add_argument("--max-tokens", type=int, default=400)
    ap.add_argument("--reasoning-effort", default=None,
                    help="low/medium/high for reasoning models (gpt-oss, qwen3.8)")
    ap.add_argument("--per-problem", type=int, default=5)
    ap.add_argument("--limit", type=int, default=0, help="cap questions (smoke test)")
    ap.add_argument("--depth", type=int, default=20)
    ap.add_argument("--jobs", type=int, default=os.cpu_count() or 4)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", default=None)
    ap.add_argument("--attempts", action="store_true",
                    help="questions from every attempt that failed simulation, not just final designs")
    ap.add_argument("--skip-noreset-init", action="store_true",
                    help="drop init-only questions on designs with no reset (spec cannot decide)")
    args = ap.parse_args()

    if args.provider == "ollama" and not server_up(os.environ.get("OLLAMA_BASE_URL")):
        print(f"no Ollama server at {os.environ.get('OLLAMA_BASE_URL')} -- "
              "source tools/gpu_up.sh first", file=sys.stderr)
        return 2
    from rtlforge.providers import LLMClient
    client = LLMClient(provider=args.provider, model=args.model,
                       temperature=0.0, max_tokens=args.max_tokens,
                       reasoning_effort=args.reasoning_effort)

    items, skipped = collect(args.reports, args.per_problem, args.depth,
                             args.jobs, args.seed, attempts=args.attempts,
                             skip_noreset_init=args.skip_noreset_init)
    if args.limit:
        items = items[:args.limit]
    print(f"{len(items)} questions ({skipped})", flush=True)

    started = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    base = args.out or os.path.join(ROOT, f"m1a_{args.model.replace(':', '_').replace('/', '_')}_{started}")
    raw = open(base + ".jsonl", "w")
    streak, aborted = 0, ""
    for i, it in enumerate(items, 1):
        try:
            text = client.chat(build_prompt(it["spec"], it["question"]))
        except Exception as exc:                          # noqa: BLE001
            text = f"ERROR {exc}"
        finish = getattr(client, "last_finish_reason", None)
        it["answer_text"] = text
        it["finish_reason"] = finish
        it["answer"] = classify(text, finish)
        it["correct"] = it["answer"] == it["gold"]
        streak = streak + 1 if it["answer"] == "ERROR" else 0
        if streak >= MAX_CONSECUTIVE_ERRORS:
            aborted = (f"aborted after {streak} consecutive failed calls at "
                       f"question {i}: {text[:200]}")
            print(aborted, file=sys.stderr)
            items = items[:i - streak]          # keep only real answers
            break
        rec = {k: v for k, v in it.items() if k != "spec"}
        raw.write(json.dumps(rec) + "\n"); raw.flush()
        if not aborted and (i % 10 == 0 or i == len(items)):
            done = items[:i]
            acc = sum(x["correct"] for x in done) / len(done)
            print(f"[{i}/{len(items)}] accuracy so far {100 * acc:.1f}%", flush=True)
    raw.close()

    def line(label, xs):
        n = len(xs); k = sum(x["correct"] for x in xs)
        if not n:
            return f"| {label} | 0 | | | | | | |"
        lo, hi = wilson(k, n)
        cnt = lambda a: sum(x["answer"] == a for x in xs)
        nei, trunc = cnt("NEITHER"), cnt("TRUNCATED") + cnt("EMPTY")
        unp = cnt("UNPARSED")
        decided = cnt("A") + cnt("B")
        return (f"| {label} | {n} | {k} ({100 * k / n:.1f}%) | "
                f"[{100 * lo:.1f}, {100 * hi:.1f}] | {nei} | {trunc} | {unp} | "
                + (f"{k}/{decided} ({100 * k / decided:.1f}%)" if decided else "–")
                + " |")

    out = [f"# M1a — spec-question answer accuracy ({started})", "",
           f"model `{args.model}`"
           + (f" (reasoning effort {args.reasoning_effort})" if args.reasoning_effort else "")
           + f", temperature 0, depth {args.depth}, "
           f"≤{args.per_problem} questions per problem, seed {args.seed}  ",
           f"source designs: {'every attempt that failed simulation' if args.attempts else 'final designs of failed runs'}"
           f"{'; no-reset power-up questions dropped' if args.skip_noreset_init else ''}  ",
           f"reports: {', '.join(os.path.basename(r) for r in args.reports)}  ",
           f"skipped: {skipped}", "",
           "Every question comes from a design that **failed** its testbench, "
           "so design-level accuracy on these is 0%. Chance on A/B is 50%.", "",
           "| subset | questions | correct | 95% CI | NEITHER | no answer (truncated/empty) | unparsed | correct among A/B |",
           "|---|---|---|---|---|---|---|---|", line("all", items)]
    for v in ("logic", "init-only"):
        out.append(line(v, [x for x in items if x["verdict"] == v]))
    gold_a = [x for x in items if x["gold"] == "A"]
    out.append(line("reference was A", gold_a))
    out.append(line("reference was B", [x for x in items if x["gold"] == "B"]))
    for lab, lo, hi in (("difference at cycle 1", 1, 1), ("cycles 2-4", 2, 4),
                        ("cycles 5-8", 5, 8), ("cycles 9+", 9, 10 ** 6)):
        out.append(line(lab, [x for x in items if x["cycles"] and lo <= x["cycles"] <= hi]))
    out += ["", "## Per problem", "", "| problem | questions | correct |", "|---|---|---|"]
    byp = defaultdict(list)
    for x in items:
        byp[x["problem"]].append(x)
    for name, xs in sorted(byp.items()):
        out.append(f"| {name} | {len(xs)} | {sum(x['correct'] for x in xs)} |")
    probs_any = sum(1 for xs in byp.values() if any(x["correct"] for x in xs))
    if aborted:
        out = [f"> **RUN ABORTED** — {aborted}", ""] + out
    out += ["", f"Problems where at least one question was answered correctly: "
            f"{probs_any}/{len(byp)}", "",
            f"Raw prompts and answers: `{os.path.basename(base)}.jsonl`"]
    with open(base + ".md", "w") as fh:
        fh.write("\n".join(out) + "\n")
    print(f"report: {base}.md")
    return 3 if aborted else 0


if __name__ == "__main__":
    raise SystemExit(main())
