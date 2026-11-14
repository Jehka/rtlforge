#!/usr/bin/env python3
"""Replay the no-testbench pipeline on designs already logged in reports.

For each problem, the candidates are the first design of each run that
compiled and reached simulation (lint/compile feedback needs no golden
testbench). The pipeline never sees the testbench verdicts; they are used
only to score it.

  questions  (CPU)  cluster candidates by formal equivalence; one spec
                    question per pair of cluster representatives
  judge      (GPU)  one model answers every question
  score      (any)  accept a cluster only if it wins every comparison,
                    under one judge or under two judges agreeing; report
                    coverage, precision and accuracy against baselines

    python tools/replay_pipeline.py questions diag_A.md diag_B.md --out replay_q.jsonl --jobs 16
    python tools/replay_pipeline.py judge replay_q.jsonl --model qwen3.8:27b --reasoning-effort low --max-tokens 16384
    python tools/replay_pipeline.py score replay_q.jsonl --answers replay_a_qwen*.jsonl replay_a_gpt*.jsonl
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import random
import re
import sys
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

from contract_check import PROBLEMS, parse_report                    # noqa: E402

SIMULATED = ("pass", "fail at simulate")


# ------------------------------------------------------------ candidates

def _norm(rtl: str) -> str:
    rtl = re.sub(r"//[^\n]*|/\*.*?\*/", "", rtl, flags=re.S)
    return re.sub(r"\s+", " ", rtl).strip()


def candidates(reports):
    """{problem: [{"hash", "rtl", "passed", "runs": [(arm, trial)]}]}"""
    out = defaultdict(dict)
    for path in reports:
        arm, runs = parse_report(path)
        for r in runs:
            first = next((a for a in r.get("attempts", []) if a["status"] in SIMULATED
                          and a["rtl"].strip()), None)
            if first is None:
                continue
            h = hashlib.sha256(_norm(first["rtl"]).encode()).hexdigest()[:16]
            c = out[r["name"]].setdefault(h, {"hash": h, "rtl": first["rtl"], "runs": [],
                                              "passed": []})
            c["runs"].append([arm, r["trial"]])
            c["passed"].append(first["status"] == "pass")
    return {p: list(v.values()) for p, v in out.items()}


# ------------------------------------------------------------ clustering

def _same(d) -> bool:
    """Formally the same behaviour for the pipeline's purposes."""
    return d.verdict == "equivalent" or (d.verdict == "init-only" and not d.resets)


def compiles_in_simulator(rtl: str, top: str) -> bool | None:
    """True/False if the target simulator elaborates the design; None if no
    simulator is configured. Yosys accepts some designs Icarus rejects (an
    output wire assigned in an always block); the harness then fails them
    without running a test, so formally equivalent members of one cluster
    got different testbench verdicts (6 clusters, CVDP, 2026-11-14). A compile
    check needs no testbench. RTLFORGE_IVERILOG points at the harness's Icarus."""
    exe = os.environ.get("RTLFORGE_IVERILOG")
    if not exe:
        return None
    import subprocess
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        src = os.path.join(d, "design.sv")
        with open(src, "w") as fh:
            fh.write(rtl)
        try:
            r = subprocess.run([exe, "-g2012", "-s", top, "-o", os.path.join(d, "a.vvp"), src],
                               capture_output=True, text=True, timeout=120)
        except (OSError, subprocess.TimeoutExpired):
            return False
        return r.returncode == 0


def cluster_problem(args):
    """Insert designs one by one: join the first equivalent cluster, else
    become a new representative. Every pair of representatives is then
    compared exactly once, so all rep-pair counterexamples are on hand."""
    from rtlforge.formal import distinguish, prove_equivalent, render_ab_question
    name, cands, depth, seed = args[:4]
    top = args[4] if len(args) > 4 else "TopModule"
    gate = args[5] if len(args) > 5 else False
    prove = args[6] if len(args) > 6 else False
    cands = sorted(cands, key=lambda c: -len(c["runs"]))     # biggest first
    dropped = []
    if gate:
        # No-testbench compile gate: a design the formal front end cannot
        # read on its own cannot be clustered or asked about. (Without this
        # one unreadable design blocks every other cluster from sweeping.)
        usable = []
        for c in cands:
            ok = (bool(c["rtl"].strip())
                  and compiles_in_simulator(c["rtl"], top) is not False
                  and distinguish(c["rtl"], c["rtl"], top_gold=top, top_gate=top, depth=1,
                                  shortest=False).verdict != "error")
            (usable if ok else dropped).append(c)
        cands = usable
    reps, members, pairs, errors, inconclusive = [], [], {}, 0, []
    pending = []                     # (rep index, new design) bounded-same, unproven
    for c in cands:
        joined, ds, unproven = None, {}, []
        for i, r in enumerate(reps):
            d = distinguish(r["rtl"], c["rtl"], top_gold=top,
                            top_gate=top, depth=depth)
            if d.verdict == "error":
                errors += 1
            if _same(d):
                # Bounded "no difference in `depth` cycles" is not equivalence:
                # with prove, merge only on an induction proof (NoTB merges on
                # proven equivalence only).
                if not prove or d.verdict == "init-only" or prove_equivalent(
                        r["rtl"], c["rtl"], top_gold=top, top_gate=top) == "proven":
                    joined = i
                    break
                unproven.append(i)
                continue
            ds[i] = d
        if joined is not None:
            members[joined].append(c)
            continue
        j = len(reps)
        reps.append(c)
        members.append([c])
        for i, d in ds.items():
            pairs[(i, j)] = d
        inconclusive += [[i, j] for i in unproven]
    rng = random.Random(f"{seed}:{name}")
    clusters = []
    for i, ms in enumerate(members):
        labels = [p for m in ms for p in m["passed"]]
        clusters.append({"id": i, "weight": len(labels), "designs": len(ms),
                         "n_passed": sum(labels),
                         "sources": sorted({r[0] for m in ms for r in m["runs"]}),
                         "passed": sum(labels) * 2 > len(labels),
                         "mixed": 0 < sum(labels) < len(labels),
                         "hashes": [m["hash"] for m in ms]})
    questions = []
    for (i, j), d in sorted(pairs.items()):
        if d.verdict not in ("logic", "init-only") or d.counterexample is None:
            continue
        i_is_a = rng.random() < 0.5
        q, _ = render_ab_question(d, gold_is_a=i_is_a)      # rep i plays "gold"
        questions.append({"qid": f"{name}:{i}-{j}", "ci": i, "cj": j,
                          "label_i": "A" if i_is_a else "B", "question": q,
                          "verdict": d.verdict,
                          "cycles": len(d.counterexample.trace_until_difference())})
    untestable = sum(1 for (i, j), d in pairs.items() if d.verdict == "error")
    return {"problem": name, "clusters": clusters, "questions": questions,
            "errors": errors, "untestable_pairs": untestable,
            "inconclusive_pairs": inconclusive,
            "dropped": sum(len(c["runs"]) for c in dropped),
            "dropped_passed": sum(sum(c["passed"]) for c in dropped),
            "candidates": len(cands), "runs": sum(len(c["runs"]) for c in cands)}


def cmd_questions(args):
    cands = candidates(args.reports)
    if args.problems:
        cands = {p: c for p, c in cands.items() if p in set(args.problems)}
    todo = [(p, cs, args.depth, args.seed, "TopModule", False, args.prove)
            for p, cs in sorted(cands.items())]
    specs = {}
    for p in cands:
        sp = os.path.join(PROBLEMS, p, "spec.md")
        specs[p] = open(sp).read() if os.path.exists(sp) else ""
    return _run_questions(args, todo, specs)


def cvdp_spec(dp: dict) -> str:
    """What the generator was given: context files, then the request."""
    parts = [f"File {f}:\n```\n{c}\n```" for f, c in (dp["input"].get("context") or {}).items()]
    return "\n\n".join(parts + [dp["input"]["prompt"]])


def cvdp_top(dp: dict) -> str:
    h = dp.get("harness") or {}
    env = (h.get("files", h) or {}).get("src/.env", "")
    m = re.search(r"^\s*TOPLEVEL\s*=\s*(\S+)", env, re.M)
    return m.group(1) if m else ""


def cvdp_candidates(dataset, cand_dirs, categories):
    """{id: [cand]} from cvdp_generate.py output dirs (cand_sN.jsonl with
    cand_sN_result.json beside it); source = directory name."""
    data = {}
    for l in open(dataset):
        if l.strip():
            dp = json.loads(l)
            if not categories or dp["categories"][0] in categories:
                data[dp["id"]] = dp
    out = defaultdict(dict)
    for d in cand_dirs:
        source = os.path.basename(os.path.normpath(d))
        for f in sorted(os.listdir(d)):
            m = re.fullmatch(r"cand_s(\d+)\.jsonl", f)
            rf = os.path.join(d, f"cand_s{m.group(1)}_result.json") if m else ""
            if not m or not os.path.exists(rf):
                continue
            verdict = {r["id"]: r["status"] == "pass" for r in json.load(open(rf))}
            for l in open(os.path.join(d, f)):
                row = json.loads(l)
                if row["id"] not in data or row["id"] not in verdict:
                    continue
                rtl = "\n".join(t for n, t in sorted(row["files"].items())
                                if n.endswith((".sv", ".v")) and t.strip())
                h = hashlib.sha256(_norm(rtl).encode()).hexdigest()[:16]
                c = out[row["id"]].setdefault(h, {"hash": h, "rtl": rtl, "runs": [], "passed": []})
                c["runs"].append([source, int(m.group(1))])
                c["passed"].append(verdict[row["id"]])
    return data, {k: list(v.values()) for k, v in out.items()}


def cmd_questions_cvdp(args):
    data, cands = cvdp_candidates(args.dataset, args.candidates, set(args.category or []))
    if args.problems:
        cands = {p: c for p, c in cands.items() if p in set(args.problems)}
    todo, specs = [], {}
    for p, cs in sorted(cands.items()):
        top = cvdp_top(data[p])
        if not top:
            print(f"no TOPLEVEL for {p}; skipped", file=sys.stderr)
            continue
        todo.append((p, cs, args.depth, args.seed, top, True, not args.no_prove))
        specs[p] = cvdp_spec(data[p])
    return _run_questions(args, todo, specs)


def _run_questions(args, todo, specs):
    print(f"{len(todo)} problems, {sum(len(t[1]) for t in todo)} unique designs, "
          f"{args.jobs} workers", flush=True)
    out = []
    with ProcessPoolExecutor(max_workers=args.jobs) as pool:
        for i, res in enumerate(pool.map(cluster_problem, todo), 1):
            res["spec"] = specs.get(res["problem"], "")
            out.append(res)
            if i % 20 == 0:
                print(f"  {i}/{len(todo)}", flush=True)
    path = args.out or os.path.join(ROOT, f"replay_q_{dt.datetime.now():%Y%m%d-%H%M%S}.jsonl")
    with open(path + ".tmp", "w") as fh:
        for r in out:
            fh.write(json.dumps(r) + "\n")
    os.replace(path + ".tmp", path)
    k = Counter(len(r["clusters"]) for r in out)
    print(f"clusters per problem: {dict(sorted(k.items()))}; "
          f"questions {sum(len(r['questions']) for r in out)}; "
          f"formal errors {sum(r['errors'] for r in out)}; "
          f"runs dropped at the compile gate {sum(r.get('dropped', 0) for r in out)} "
          f"(of which testbench-passing {sum(r.get('dropped_passed', 0) for r in out)}); "
          f"problems without a spec {sum(not r['spec'] for r in out)}")
    print("questions:", path)
    return 0


# ----------------------------------------------------------------- judge

def cmd_judge(args):
    from spec_query_eval import MAX_CONSECUTIVE_ERRORS, build_prompt, classify, server_up
    if args.provider == "ollama" and not server_up(os.environ.get("OLLAMA_BASE_URL")):
        print("no Ollama server -- source tools/gpu_up.sh first", file=sys.stderr)
        return 2
    from rtlforge.providers import LLMClient
    client = LLMClient(provider=args.provider, model=args.model, temperature=0.0,
                       max_tokens=args.max_tokens, reasoning_effort=args.reasoning_effort)
    probs = [json.loads(l) for l in open(args.questions)]
    items = [(p, q) for p in probs for q in p["questions"] if p["spec"]]
    reuse = {}
    for path in args.reuse or []:
        old_q = {}
        qpath = path.split("::")[0]
        for line in open(qpath):
            pp = json.loads(line)
            for qq in pp["questions"]:
                old_q[qq["qid"]] = (pp["spec"], qq["question"])
        for line in open(path.split("::")[1]):
            row = json.loads(line)
            if row.get("model") == args.model and row["qid"] in old_q and row["answer"] in ("A", "B", "NEITHER"):
                reuse[old_q[row["qid"]]] = row
    tag = args.model.replace(":", "_").replace("/", "_")
    path = args.out or os.path.join(ROOT, f"replay_a_{tag}_{dt.datetime.now():%Y%m%d-%H%M%S}.jsonl")
    print(f"{len(items)} questions -> {path}", flush=True)
    streak = 0
    with open(path, "w") as fh:
        reused = 0
        for n, (p, q) in enumerate(items, 1):
            old = reuse.get((p["spec"], q["question"]))
            if old is not None:
                # same spec and same question text, answered before by this model
                text, ans = old["answer_text"], old["answer"]
                reused += 1
                fh.write(json.dumps({"qid": q["qid"], "model": args.model, "answer": ans,
                                     "answer_text": text, "reused": True}) + "\n")
                continue
            try:
                text = client.chat(build_prompt(p["spec"], q["question"]))
            except Exception as exc:                          # noqa: BLE001
                text = f"ERROR {exc}"
            ans = classify(text, getattr(client, "last_finish_reason", None))
            fh.write(json.dumps({"qid": q["qid"], "model": args.model, "answer": ans,
                                 "answer_text": text}) + "\n")
            fh.flush()
            streak = streak + 1 if ans == "ERROR" else 0
            if streak >= MAX_CONSECUTIVE_ERRORS:
                print(f"aborted after {streak} consecutive errors at {n}: {text[:200]}",
                      file=sys.stderr)
                return 3
            if n % 20 == 0:
                print(f"  {n}/{len(items)}", flush=True)
    print(f"answers: {path} ({reused} reused)")
    return 0


# ----------------------------------------------------------------- score

def _winner(q, answer):
    """Map an A/B/NEITHER answer to the winning cluster id, -1 for NEITHER
    (both lose), None for no usable answer."""
    if answer == "NEITHER":
        return -1
    if answer not in ("A", "B"):
        return None
    return q["ci"] if answer == q["label_i"] else q["cj"]


def decide(prob, outcome):
    """outcome(q) -> winner id / -1 / None / "split". Returns accepted
    cluster id, or None (defer). A cluster is accepted when it wins every
    question it takes part in; exactly one such cluster must exist."""
    cl = prob["clusters"]
    if not cl:
        return None
    if len(cl) == 1:
        return 0
    if not prob["questions"]:
        return None
    wins = {c["id"]: True for c in cl}
    asked = Counter()
    for q in prob["questions"]:
        w = outcome(q)
        asked[q["ci"]] += 1
        asked[q["cj"]] += 1
        for c in (q["ci"], q["cj"]):
            if w != c:
                wins[c] = False
    ok = [c for c, v in wins.items() if v and asked[c] == len(cl) - 1]
    return ok[0] if len(ok) == 1 else None


def cmd_score(args):
    probs = [json.loads(l) for l in open(args.questions)]
    judges = {}
    for path in args.answers:
        rows = [json.loads(l) for l in open(path)]
        if rows:
            judges[rows[0]["model"]] = {r["qid"]: r["answer"] for r in rows}
    names = list(judges)
    strategies = {}
    for n in names:
        strategies[f"judge {n}"] = (lambda n: lambda q: _winner(q, judges[n].get(q["qid"])))(n)
    if len(names) >= 2:
        def agree(q, ns=names[:2]):
            ws = [_winner(q, judges[n].get(q["qid"])) for n in ns]
            return ws[0] if ws[0] == ws[1] and ws[0] is not None else "split"
        strategies[f"two judges agree ({names[0]} + {names[1]})"] = agree
    if len(names) >= 3:
        def unanimous(q, ns=names):
            ws = [_winner(q, judges[n].get(q["qid"])) for n in ns]
            return ws[0] if ws[0] is not None and all(w == ws[0] for w in ws) else "split"
        def majority_vote(q, ns=names):
            ws = Counter(w for w in (_winner(q, judges[n].get(q["qid"])) for n in ns)
                         if w is not None)
            if not ws:
                return "split"
            (w, k), = ws.most_common(1)
            return w if k * 2 > len(ns) else "split"
        strategies[f"all {len(names)} judges agree"] = unanimous
        strategies[f"majority of {len(names)} judges"] = majority_vote

    P = len(probs)
    def correct(p, cid):
        return cid is not None and bool(p["clusters"]) and p["clusters"][cid]["passed"]
    majority = lambda p: (max(p["clusters"], key=lambda c: c["weight"])["id"]
                          if p["clusters"] else None)
    first_try = sum((sum(c.get("n_passed", c["passed"] * c["weight"]) for c in p["clusters"])
                     + p.get("dropped_passed", 0)) /
                    max(1, sum(c["weight"] for c in p["clusters"]) + p.get("dropped", 0))
                    for p in probs)
    single = [p for p in probs if len(p["clusters"]) == 1]
    lines = [f"# Replay of the no-testbench pipeline ({dt.datetime.now():%Y-%m-%d %H:%M})", "",
             f"Questions file `{os.path.basename(args.questions)}`; {P} problems; "
             f"judges: {', '.join(names) or 'none'}.", "",
             "| problems by clusters | count | correct |", "|---|---|---|"]
    k = defaultdict(list)
    for p in probs:
        k[min(len(p["clusters"]), 4)].append(p)       # 0 = no usable candidate
    for n in sorted(k):
        lab = f"{n}" if n < 4 else "4+"
        lines.append(f"| {lab} | {len(k[n])} | "
                     + ("no usable candidate" if n == 0 else
                        f"{sum(correct(p, 0) for p in k[n])} of the single cluster" if n == 1
                        else f"a correct cluster exists in {sum(any(c['passed'] for c in p['clusters']) for p in k[n])}")
                     + " |")
    lines += ["", "| strategy | accepted | precision of accepted | deferred | accuracy (defer → majority) |",
              "|---|---|---|---|---|",
              f"| first design (no pipeline) | — | — | — | {100 * first_try / P:.1f}% (expected) |",
              f"| largest formal cluster (majority vote, no judge) | {P} | {100 * sum(correct(p, majority(p)) for p in probs) / P:.1f}% | 0 | {100 * sum(correct(p, majority(p)) for p in probs) / P:.1f}% |",
              f"| oracle: a correct cluster exists | — | — | — | {100 * sum(any(c['passed'] for c in p['clusters']) for p in probs) / P:.1f}% (ceiling) |"]
    if any(len(c.get("sources", [])) > 1 for p in probs for c in p["clusters"]):
        # NoTB-style consensus: accept the largest cluster only when it spans
        # every generator family present; no questions asked.
        fam = max(len({s for c in p["clusters"] for s in c.get("sources", [])}) for p in probs)
        cons = [(p, majority(p) if p["clusters"] and len(p["clusters"][majority(p)].get("sources", [])) >= fam
                 else None) for p in probs]
        acc_c = [(p, c) for p, c in cons if c is not None]
        lines.append(f"| cross-family consensus (largest cluster spans all {fam} generators, no judge) | "
                     f"{len(acc_c)} ({100 * len(acc_c) / P:.0f}%) | "
                     f"{sum(correct(p, c) for p, c in acc_c)}/{len(acc_c)} "
                     f"({100 * sum(correct(p, c) for p, c in acc_c) / max(1, len(acc_c)):.1f}%) | "
                     f"{P - len(acc_c)} | {100 * sum(correct(p, majority(p)) for p in probs) / P:.1f}% |")
    for sname, fn in strategies.items():
        acc = [(p, decide(p, fn)) for p in probs]
        accepted = [(p, c) for p, c in acc if c is not None]
        prec = sum(correct(p, c) for p, c in accepted)
        overall = sum(correct(p, c if c is not None else majority(p)) for p, c in acc)
        lines.append(f"| {sname} | {len(accepted)} ({100 * len(accepted) / P:.0f}%) | "
                     f"{prec}/{len(accepted)} ({100 * prec / max(1, len(accepted)):.1f}%) | "
                     f"{P - len(accepted)} | {100 * overall / P:.1f}% |")
        multi = [(p, c) for p, c in accepted if len(p["clusters"]) > 1]
        lines.append(f"|  ↳ of which decided by questions | {len(multi)} | "
                     f"{sum(correct(p, c) for p, c in multi)}/{len(multi)} | | |")
    mixed = sum(c["mixed"] for p in probs for c in p["clusters"])
    lines += ["", f"Single-cluster problems: {len(single)}, wrong single clusters (all candidates "
              f"agree and are wrong): {sum(not correct(p, 0) for p in single)}.  ",
              f"Clusters whose members disagree with the testbench verdict (formal says "
              f"equivalent, testbench says not): {mixed}.  ",
              f"Formal errors: {sum(p['errors'] for p in probs)}.  ",
              f"Runs dropped at the compile gate: {sum(p.get('dropped', 0) for p in probs)} "
              f"(testbench-passing among them: {sum(p.get('dropped_passed', 0) for p in probs)}); "
              f"problems with no usable candidate: {sum(not p['clusters'] for p in probs)}; "
              f"pairs with no proof and no bounded difference (deferred): "
              f"{sum(len(p.get('inconclusive_pairs', [])) for p in probs)} in "
              f"{sum(bool(p.get('inconclusive_pairs')) for p in probs)} problems."]
    text = "\n".join(lines) + "\n"
    print(text)
    if args.out:
        open(args.out, "w").write(text)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    q = sub.add_parser("questions")
    q.add_argument("reports", nargs="+")
    q.add_argument("--depth", type=int, default=20)
    q.add_argument("--jobs", type=int, default=os.cpu_count() or 4)
    q.add_argument("--seed", type=int, default=7)
    q.add_argument("--problems", nargs="*", help="only these problems (testing)")
    q.add_argument("--prove", action="store_true",
                   help="merge designs only on an induction proof (default off for VerilogEval reproducibility)")
    q.add_argument("--out")
    qc = sub.add_parser("questions-cvdp")
    qc.add_argument("dataset")
    qc.add_argument("--candidates", nargs="+", required=True,
                    help="cvdp_generate.py output dirs (with cand_sN_result.json)")
    qc.add_argument("--category", nargs="*", default=["cid003"])
    qc.add_argument("--depth", type=int, default=20)
    qc.add_argument("--jobs", type=int, default=os.cpu_count() or 4)
    qc.add_argument("--seed", type=int, default=7)
    qc.add_argument("--problems", nargs="*")
    qc.add_argument("--no-prove", action="store_true",
                    help="merge on bounded equivalence alone (the 2026-11-13 first run)")
    qc.add_argument("--out")
    j = sub.add_parser("judge")
    j.add_argument("questions")
    j.add_argument("--model", required=True)
    j.add_argument("--provider", default="ollama")
    j.add_argument("--max-tokens", type=int, default=16384)
    j.add_argument("--reasoning-effort", default=None)
    j.add_argument("--reuse", nargs="*",
                   help="QFILE::ANSWERS pairs from earlier runs; identical questions are not re-asked")
    j.add_argument("--out")
    s = sub.add_parser("score")
    s.add_argument("questions")
    s.add_argument("--answers", nargs="*", default=[])
    s.add_argument("--out")
    args = ap.parse_args()
    return {"questions": cmd_questions, "questions-cvdp": cmd_questions_cvdp,
            "judge": cmd_judge, "score": cmd_score}[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
