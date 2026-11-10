#!/usr/bin/env python3
"""M0: does formal disagreement agree with the testbench verdicts?

For every final design in one or more diagnostic reports, compare it with
its problem's reference using rtlforge.formal.distinguish, and cross-tab
the formal verdict against the testbench verdict already in the report:

  testbench pass + formal equivalent   agree
  testbench fail + formal difference   agree (formal recall)
  testbench pass + formal difference   candidate testbench false pass
  testbench fail + formal equivalent   formal miss (depth, init, harness)

No LLM and no GPU: it re-checks logged RTL. Identical designs are checked
once.

    python tools/formal_vs_testbench.py diag_*_evidence-*.md --jobs 8
    python tools/formal_vs_testbench.py REPORT.md --depth 40 --out m0.md
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import os
import sys
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

from contract_check import PROBLEMS, REPORT_REFS, parse_report   # noqa: E402
from rtlforge.formal import distinguish, render_question          # noqa: E402


def _job(args):
    ref, rtl, depth = args
    d = distinguish(ref, rtl, top_gold="RefModule", top_gate="TopModule",
                    depth=depth)
    fd = d.counterexample.first_difference() if d.counterexample else None
    return {"verdict": d.verdict, "error": d.error,
            "first_cycle": fd.t if fd else None,
            "outputs": d.differing_outputs(),
            "question": render_question(d, "reference", "design")}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("reports", nargs="+")
    ap.add_argument("--depth", type=int, default=20)
    ap.add_argument("--jobs", type=int, default=os.cpu_count() or 4)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    runs = []
    for path in args.reports:
        arm, rs = parse_report(path)
        for r in rs:
            r["arm"] = arm
        runs += rs
    refs = {}
    for name in {r["name"] for r in runs}:
        p = os.path.join(PROBLEMS, name, "ref.sv")
        refs[name] = open(p).read() if os.path.exists(p) else REPORT_REFS.get(name)

    key = lambda r: (r["name"], hashlib.sha256(r["rtl"].encode()).hexdigest())
    unique = {}
    for r in runs:
        if r["rtl"].strip() and refs.get(r["name"]):
            unique.setdefault(key(r), r)
    todo = list(unique)
    print(f"{len(runs)} runs, {len(todo)} unique designs, depth {args.depth}, "
          f"{args.jobs} workers ...", flush=True)
    with ProcessPoolExecutor(max_workers=args.jobs) as pool:
        res = dict(zip(todo, pool.map(
            _job, [(refs[k[0]], unique[k]["rtl"], args.depth) for k in todo],
            chunksize=4)))

    # cross-tab over runs (not unique designs): each run is a verdict
    tab = Counter()
    by_cell = defaultdict(list)
    for r in runs:
        k = key(r)
        if k not in res:
            cell = ("pass" if r["passed"] else "fail", "not checked")
        else:
            v = res[k]["verdict"]
            f = "equivalent" if v == "equivalent" else ("error" if v == "error" else "differs")
            cell = ("pass" if r["passed"] else "fail", f)
        tab[cell] += 1
        by_cell[cell].append((r, res.get(k)))

    started = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    out = [f"# M0 — formal disagreement vs testbench verdict ({started})", "",
           f"Reports: {', '.join(os.path.basename(p) for p in args.reports)}  ",
           f"Runs {len(runs)}, unique designs {len(todo)}, bounded depth "
           f"{args.depth}, reset asserted in cycle 1 where a reset input exists.", "",
           "| testbench \\ formal | equivalent | differs | error | not checked |",
           "|---|---|---|---|---|"]
    for tb in ("pass", "fail"):
        out.append(f"| **{tb}** | " + " | ".join(
            str(tab[(tb, f)]) for f in ("equivalent", "differs", "error", "not checked")) + " |")
    tf = tab[("fail", "differs")] + tab[("fail", "equivalent")]
    tp = tab[("pass", "equivalent")] + tab[("pass", "differs")]
    out += ["",
            f"- **Formal recall** on testbench failures (excluding errors): "
            f"{tab[('fail', 'differs')]}/{tf}"
            + (f" ({100 * tab[('fail', 'differs')] / tf:.1f}%)" if tf else ""),
            f"- **Agreement** on testbench passes (excluding errors): "
            f"{tab[('pass', 'equivalent')]}/{tp}"
            + (f" ({100 * tab[('pass', 'equivalent')] / tp:.1f}%)" if tp else ""),
            ""]

    verdicts = Counter(v["verdict"] for v in res.values())
    out += ["Formal verdicts over unique designs: "
            + ", ".join(f"{k} {n}" for k, n in verdicts.most_common()), ""]

    def listing(title, cell, limit=60):
        items = by_cell.get(cell, [])
        if not items:
            return []
        seen, rows = set(), []
        for r, v in items:
            k = key(r)
            if k in seen:
                continue
            seen.add(k)
            rows.append((r, v))
        lines = [f"## {title} ({len(rows)} unique designs)", ""]
        for r, v in rows[:limit]:
            lines.append(f"### {r['name']} ({r['arm']}, trial {r['trial']})")
            if v and v["error"]:
                lines.append(f"error: `{v['error']}`")
            if v and v["question"]:
                lines += ["```", v["question"], "```"]
            lines.append("")
        if len(rows) > limit:
            lines.append(f"... {len(rows) - limit} more")
        return lines

    out += listing("Testbench pass, formal differs — candidate testbench false passes",
                   ("pass", "differs"))
    out += listing("Testbench fail, formal equivalent — formal misses",
                   ("fail", "equivalent"))
    out += listing("Errors", ("pass", "error")) + listing("Errors (failing designs)",
                                                          ("fail", "error"))

    path = args.out or os.path.join(ROOT, f"m0_formal_{started}.md")
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        fh.write("\n".join(out) + "\n")
    os.replace(tmp, path)
    print(f"report: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
