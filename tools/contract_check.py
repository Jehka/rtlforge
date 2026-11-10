#!/usr/bin/env python3
"""Score generated RTL against a synthesizability / PD-readiness contract.

Functional pass on VerilogEval only proves the outputs match. This checks
the designs a diagnostic run already produced against three levels:

  functional       the run passed its testbench (taken from the report)
  synth-clean      Yosys finds no latch, logic loop, multiple drivers or
                   undriven net, and Verilator reports none of the
                   synthesis-relevant lint codes below
  PD-ready (asic)  synth-clean, plus: no `initial` / declaration init on
                   state (ASIC synthesis ignores it), no tristates, no
                   clocks derived from logic

and reports reset coverage and area (cells) against the reference design
synthesised the same way. No LLM, no GPU: it re-checks logged RTL.

    python tools/contract_check.py diag_*_evidence-on.md diag_*_evidence-off.md
    python tools/contract_check.py REPORT.md --jobs 8 --out contract.md

Environment: YOSYS (default "yosys"), VERILATOR (default "verilator"),
RTLFORGE_YOSYS_NOABC=1 to skip ABC (cell counts become pre-mapping).
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
PROBLEMS = os.path.join(ROOT, "problems", "verilogeval")
YOSYS = os.environ.get("YOSYS", "yosys")
VERILATOR = os.environ.get("VERILATOR", "verilator")
NOABC = os.environ.get("RTLFORGE_YOSYS_NOABC") == "1"

#: Verilator codes that mean the design will not synthesise as written.
LINT_FAIL = {"LATCH", "UNOPTFLAT", "MULTIDRIVEN", "COMBDLY", "BLKSEQ",
             "BLKANDNBLK", "SYNCASYNCNET", "UNDRIVEN"}
#: Reported, but legal RTL: not a contract failure.
LINT_INFO = {"WIDTH", "WIDTHEXPAND", "WIDTHTRUNC", "CASEINCOMPLETE",
             "CASEOVERLAP", "UNUSEDSIGNAL"}

_FLOP = re.compile(r"^\$_(S?DFFS?R?C?E?|ALDFFE?|DFFSRE?)_")
_ASYNC = re.compile(r"^\$_(DFF_[PN][PN][01]_|DFFE_[PN][PN][01][PN]_|"
                    r"DFFSRE?_|ALDFFE?_)")
_SYNC = re.compile(r"^\$_SDFF")
_CELL_LINE = re.compile(r"^\s*(\d+)\s+(?:[\d.]+\s+)?(\$_\w+)\s*$")
_TOTAL_LINE = re.compile(r"^\s*(\d+)\s+(?:[\d.]+\s+)?cells\s*$")


# ------------------------------------------------------------ report parse

def _fenced_after(label: str, block: str) -> str | None:
    i = block.find(label)
    if i < 0:
        return None
    rest = block[i + len(label):].lstrip("\n")
    m = re.match(r"(`{3,})[^\n]*\n", rest)
    if not m:
        return "" if rest.startswith("_(empty)_") else None
    fence = m.group(1)
    body = rest[m.end():]
    end = re.search(rf"\n{fence}(?!`)\s*\n", "\n" + body)
    return body[:end.start()] if end else body


REPORT_REFS: dict[str, str] = {}


def parse_report(path: str) -> tuple[str, list[dict]]:
    text = open(path).read()
    arm = (re.search(r"evidence \*\*(\w+)\*\*", text) or [None, "?"])[1]
    detail = text.split("## Detail", 1)[1].split("## Appendix", 1)[0]
    parts = re.split(r"\n### (Prob\S+)\n", detail)[1:]
    runs, trial = [], Counter()
    for name, body in zip(parts[::2], parts[1::2]):
        trial[name] += 1
        ref = _fenced_after("<summary>reference</summary>", body)
        if ref and name not in REPORT_REFS:
            # Fallback when problems/ is not alongside: reports embed the
            # reference once per problem.
            REPORT_REFS[name] = ref
        atts = re.split(r"\n#### attempt (\d+) — ([^\n]+)\n", body)[1:]
        if not atts:
            continue
        last_status, last_body = atts[-2], atts[-1]
        attempts = [{"n": int(atts[i]), "status": atts[i + 1],
                     "rtl": _fenced_after("**rtl**", atts[i + 2]) or ""}
                    for i in range(0, len(atts) - 2, 3)]
        runs.append({"name": name, "trial": trial[name],
                     "passed": last_status == "pass",
                     "rtl": _fenced_after("**rtl**", last_body) or "",
                     "attempts": attempts})
    return arm, runs


# --------------------------------------------------------------- checks

@dataclass
class Check:
    ok: bool = True                    # tools ran
    error: str = ""
    latch: bool = False
    loop: bool = False
    multidriven: bool = False
    undriven: bool = False
    init: int = 0
    tristate: bool = False
    derived_clocks: list = field(default_factory=list)
    lint: Counter = field(default_factory=Counter)
    cells: int | None = None
    flops: int = 0
    flops_async: int = 0
    flops_sync: int = 0

    def synth_violations(self) -> list[str]:
        v = []
        if self.latch: v.append("latch")
        if self.loop: v.append("comb loop")
        if self.multidriven: v.append("multiple drivers")
        if self.undriven: v.append("undriven net")
        v += [f"lint:{c}" for c in sorted(self.lint) if c in LINT_FAIL]
        return v

    def pd_violations(self) -> list[str]:
        v = list(self.synth_violations())
        if self.init: v.append("initial/decl init")
        if self.tristate: v.append("tristate")
        if self.derived_clocks:
            v.append("derived clock " + ",".join(self.derived_clocks))
        return v


def _run(cmd: list[str], cwd: str, timeout: int = 120) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                           timeout=timeout)
        return p.returncode, p.stdout + p.stderr
    except FileNotFoundError:
        return 127, f"NOTFOUND: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return 124, "TIMEOUT"


def _parse_stat(log: str) -> tuple[int | None, Counter]:
    """First module's stat block: total cells and per-type counts."""
    block = log.split("Printing statistics", 1)[-1] if "Printing statistics" in log else ""
    total, cells, seen_total = None, Counter(), False
    for line in block.splitlines():
        t = _TOTAL_LINE.match(line)
        if t:
            if seen_total:
                break                  # second block is the design summary
            total, seen_total = int(t.group(1)), True
            continue
        c = _CELL_LINE.match(line)
        if c and seen_total:
            cells[c.group(2)] += int(c.group(1))
    return total, cells


def check_design(rtl: str, top: str = "TopModule") -> Check:
    chk = Check()
    if not rtl.strip():
        return Check(ok=False, error="no RTL logged")
    work = tempfile.mkdtemp(prefix="contract-")
    try:
        with open(os.path.join(work, "design.v"), "w") as fh:
            fh.write(rtl)

        # Markers delimit each query's output so parsing never depends on
        # which "N objects." line comes first. Derived clocks: wires on a
        # flop's CLK pin that are not module inputs -- a gated clock or a
        # flop-divided one. CLK only: a logic-driven clock *enable* is fine.
        script = (f"read_verilog -sv design.v; hierarchy -top {top}; proc; "
                  f"flatten; log @@INIT; select -count a:init; "
                  f"log @@CLK; select -list t:$*dff* %ci:+[CLK] w:* %i i:* %d; "
                  f"log @@END; tribuf; check; "
                  f"synth -top {top} -flatten{' -noabc' if NOABC else ''}; "
                  f"check; stat")
        rc, log = _run([YOSYS, "-p", script], work)
        if rc != 0 or "ERROR" in log:
            err = next((l for l in log.splitlines() if "ERROR" in l),
                       log.strip().splitlines()[-1] if log.strip() else "")
            return Check(ok=False, error=f"yosys: {err[:200]}")
        # Line-anchored: Yosys echoes the whole script, markers included,
        # at the top of the log, so a plain split lands inside the echo.
        def section(a: str, b: str) -> str:
            m = re.search(rf"^{a}\s*$(.*?)^{b}\s*$", log, re.M | re.S)
            return m.group(1) if m else ""
        init_part = section("@@INIT", "@@CLK")
        m = re.search(r"^(\d+) objects\.", init_part, re.M)
        chk.init = int(m.group(1)) if m else 0
        clk_part = section("@@CLK", "@@END")
        chk.derived_clocks = sorted({
            m.group(1) for m in re.finditer(r"^\S+/(\S+)\s*$", clk_part, re.M)})
        chk.latch = "Latch inferred" in log
        chk.loop = "found logic loop" in log
        chk.multidriven = "multiple conflicting drivers" in log
        chk.undriven = "is used but has no driver" in log
        chk.cells, cells = _parse_stat(log)
        chk.latch |= any("DLATCH" in c for c in cells)
        chk.tristate = any("TBUF" in c for c in cells)
        for name, n in cells.items():
            if _FLOP.match(name):
                chk.flops += n
                if _ASYNC.match(name):
                    chk.flops_async += n
                elif _SYNC.match(name):
                    chk.flops_sync += n

        rc, out = _run([VERILATOR, "--lint-only", "-Wall", "-Wno-DECLFILENAME",
                        "-Wno-EOFNEWLINE", "--top-module", top, "design.v"],
                       work)
        if not out.startswith("NOTFOUND"):
            for code in re.findall(r"^%(?:Warning|Error)-([A-Z0-9_]+)", out, re.M):
                chk.lint[code] += 1

        return chk
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _check_job(args: tuple[str, str]) -> Check:
    rtl, top = args
    return check_design(rtl, top)


# --------------------------------------------------------------- report

def pct(a: int, b: int) -> str:
    return f"{a}/{b} ({100 * a / b:.1f}%)" if b else f"{a}/0"


def render(arms: dict, refs: dict, started: str) -> str:
    out = [f"# RTLForge contract check — {started}", "",
           f"yosys `{YOSYS}`{' (no ABC)' if NOABC else ''}, "
           f"verilator `{VERILATOR}`", "",
           "Levels: **functional** = testbench passed; **synth-clean** = no "
           "latch, comb loop, multiple drivers, undriven net or synthesis-"
           "relevant lint; **PD-ready** = synth-clean + no initial/decl init "
           "+ no tristate + no logic-derived clock.", ""]

    out += ["## Summary", "",
            "| arm | runs | functional | synth-clean | PD-ready | PD-ready of functional |",
            "|---|---|---|---|---|---|"]
    for arm, rows in arms.items():
        n = len(rows)
        f = [r for r in rows if r["passed"]]
        sc = [r for r in f if r["chk"].ok and not r["chk"].synth_violations()]
        pd = [r for r in f if r["chk"].ok and not r["chk"].pd_violations()]
        out.append(f"| {arm} | {n} | {pct(len(f), n)} | {pct(len(sc), n)} | "
                   f"{pct(len(pd), n)} | {pct(len(pd), len(f))} |")
    out.append("")

    for arm, rows in arms.items():
        f = [r for r in rows if r["passed"]]
        out += [f"## Arm `{arm}`: violations among functional passes", ""]
        viol = Counter()
        tool_err = [r for r in f if not r["chk"].ok]
        for r in f:
            if r["chk"].ok:
                for v in r["chk"].pd_violations():
                    viol[re.sub(r" \S+$", "", v) if v.startswith("derived") else v] += 1
        if not viol and not tool_err:
            out += ["none", ""]
        for v, n in viol.most_common():
            out.append(f"- {v}: {n}")
        if tool_err:
            out.append(f"- tool errors (not counted as violations): {len(tool_err)}")
        out.append("")

        seq = [r["chk"] for r in f if r["chk"].ok and r["chk"].flops]
        if seq:
            total = sum(c.flops for c in seq)
            a = sum(c.flops_async for c in seq); s = sum(c.flops_sync for c in seq)
            none_all = sum(1 for c in seq if c.flops_async + c.flops_sync == 0)
            out += [f"Reset coverage over {len(seq)} sequential designs, "
                    f"{total} flops: async {pct(a, total)}, sync {pct(s, total)}, "
                    f"none {pct(total - a - s, total)}. Designs with no reset on "
                    f"any flop: {none_all}. (Many VerilogEval specs have no reset "
                    "port; reported, not gated.)", ""]

        ratios = []
        for r in f:
            ref = refs.get(r["name"])
            if r["chk"].ok and r["chk"].cells and ref and ref.ok and ref.cells:
                ratios.append(r["chk"].cells / ref.cells)
        if ratios:
            ratios.sort()
            q = lambda p: ratios[min(len(ratios) - 1, int(p * len(ratios)))]
            big = sum(x > 1.5 for x in ratios)
            out += [f"Area vs reference (cells, same flow), {len(ratios)} designs: "
                    f"median {statistics.median(ratios):.2f}×, IQR "
                    f"[{q(0.25):.2f}, {q(0.75):.2f}], >1.5× larger: {big}.", ""]

        bad = [(r, r["chk"].pd_violations()) for r in f
               if r["chk"].ok and r["chk"].pd_violations()]
        if bad:
            out += ["<details><summary>functional but not PD-ready "
                    f"({len(bad)})</summary>", "",
                    "| problem | trial | violations |", "|---|---|---|"]
            out += [f"| {r['name']} | {r['trial']} | {', '.join(v)} |"
                    for r, v in sorted(bad, key=lambda x: x[0]["name"])]
            out += ["", "</details>", ""]
        if tool_err:
            out += ["<details><summary>tool errors</summary>", ""]
            out += [f"- {r['name']} t{r['trial']}: {r['chk'].error}" for r in tool_err]
            out += ["", "</details>", ""]

    ref_bad = {n: c for n, c in refs.items() if c.ok and c.pd_violations()}
    out += ["## Reference designs", "",
            f"{len(refs)} references checked; {len(ref_bad)} are themselves not "
            "PD-ready by this contract (VerilogEval references use `initial` "
            "and reset-less state freely)."]
    if ref_bad:
        out += ["", "<details><summary>list</summary>", ""]
        out += [f"- {n}: {', '.join(c.pd_violations())}" for n, c in sorted(ref_bad.items())]
        out += ["", "</details>"]
    return "\n".join(out) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("reports", nargs="+")
    ap.add_argument("--jobs", type=int, default=os.cpu_count() or 4)
    ap.add_argument("--out", default=None)
    ap.add_argument("--all-runs", action="store_true",
                    help="also check runs that failed functionally")
    args = ap.parse_args()

    for tool in (YOSYS, VERILATOR):
        if not shutil.which(tool):
            print(f"{tool} not found on PATH (activate the rtlforge env)",
                  file=sys.stderr)
            return 1

    started = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    out_path = args.out or os.path.join(ROOT, f"contract_{started}.md")

    arms: dict[str, list[dict]] = {}
    for path in args.reports:
        arm, runs = parse_report(path)
        key, k = arm, 2
        while key in arms:
            key, k = f"{arm}-{k}", k + 1
        arms[key] = runs
        print(f"{path}: arm {key}, {len(runs)} runs, "
              f"{sum(r['passed'] for r in runs)} functional")

    todo = [(key, i) for key, runs in arms.items() for i, r in enumerate(runs)
            if r["passed"] or args.all_runs]
    names = sorted({r["name"] for runs in arms.values() for r in runs})
    ref_src = {}
    for n in names:
        p = os.path.join(PROBLEMS, n, "ref.sv")
        if os.path.exists(p):
            ref_src[n] = open(p).read()
        elif n in REPORT_REFS:
            ref_src[n] = REPORT_REFS[n]

    jobs = ([(arms[k][i]["rtl"], "TopModule") for k, i in todo]
            + [(src, "RefModule") for src in ref_src.values()])
    print(f"checking {len(todo)} designs + {len(ref_src)} references "
          f"with {args.jobs} workers ...", flush=True)
    with ProcessPoolExecutor(max_workers=args.jobs) as pool:
        results = list(pool.map(_check_job, jobs, chunksize=4))

    for (k, i), chk in zip(todo, results):
        arms[k][i]["chk"] = chk
    for runs in arms.values():
        for r in runs:
            r.setdefault("chk", Check(ok=False, error="not checked"))
    refs = dict(zip(ref_src, results[len(todo):]))

    tmp = out_path + ".tmp"
    with open(tmp, "w") as fh:
        fh.write(render(arms, refs, started))
    os.replace(tmp, out_path)
    print(f"report: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
