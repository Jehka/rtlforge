#!/usr/bin/env python3
"""Run CVDP harnesses without Docker.

The CVDP harness (NVlabs/cvdp_benchmark) runs each datapoint's
docker-compose services in the nvidia/cvdp-sim image, with the issue
directory mounted at /code/{docs,rtl,verif,src,rundir}, src/ also at
/src, and working directory /code/rundir. A datapoint passes when every
service exits 0. This script reproduces that layout in a temp directory,
rewrites the absolute container paths to it, and runs each service's
command directly, so it works on a cluster with no Docker.

The environment must match the sim image: Icarus 13 (v13_0),
cocotb 2.0.1, pytest 8.3.2, cocotb-bus 0.3.0, cocotbext-axi 0.1.28.
Datapoints that need a commercial simulator image are skipped.

    # reference solutions must pass (harness self-test)
    python tools/cvdp_run.py DATA.jsonl --solutions --jobs 8

    # candidates: JSONL lines {"id": ..., "files": {"rtl/x.sv": "..."}}
    python tools/cvdp_run.py DATA.jsonl --candidates cand.jsonl --out res.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ISSUE_DIRS = ("docs", "rtl", "verif", "src", "rundir")
OSS_IMAGES = ("__OSS_SIM_IMAGE__", "nvidia/cvdp-sim")
TEXT_SUFFIXES = (".py", ".env", ".sh", ".tcl", ".mk", ".json", ".yml", ".yaml",
                 ".txt", ".cfg", ".ini", "Makefile")


@dataclass
class Result:
    id: str
    status: str                      # pass | fail | error | skipped | timeout
    categories: list = field(default_factory=list)
    services: list = field(default_factory=list)
    tests: dict = field(default_factory=dict)
    seconds: float = 0.0
    note: str = ""
    workdir: str = ""


# ---------------------------------------------------------------- datapoints

def harness_files(dp: dict) -> dict:
    h = dp.get("harness") or {}
    return h.get("files", h) if isinstance(h, dict) else {}


def context_files(dp: dict) -> dict:
    """Files the model is given (agentic: context; non-agentic: input.context)."""
    if "input" in dp:
        return dict(dp["input"].get("context") or {})
    return dict(dp.get("context") or {})


def solution_files(dp: dict, workdir: str) -> dict | str:
    """Reference files. Non-agentic: output.context. Agentic: a patch,
    applied to the context in workdir; returns an error string on failure."""
    if "output" in dp:
        return dict(dp["output"].get("context") or {})
    patch = dp.get("patch") or {}
    if not isinstance(patch, dict):
        return "unsupported patch format"
    diff = "".join(f"--- a/{p}\n+++ b/{p}\n{h if h.endswith(chr(10)) else h + chr(10)}"
                   for p, h in patch.items())
    for p in patch:
        os.makedirs(os.path.dirname(os.path.join(workdir, p)) or workdir, exist_ok=True)
    r = subprocess.run(["patch", "-p1", "-s", "-N", "-d", workdir],
                       input=diff, text=True, capture_output=True)
    if r.returncode:
        return f"patch failed: {(r.stdout + r.stderr).strip()[:300]}"
    return {}


# ------------------------------------------------------------ path rewriting

def rewriter(issue: str):
    src = os.path.join(issue, "src")
    rundir = os.path.join(issue, "rundir")
    pat = re.compile(r"(?<![\w.\-/])/(code|src|rundir)(?=/|\b)")

    def sub(text: str) -> str:
        return pat.sub(lambda m: {"code": issue, "src": src,
                                  "rundir": rundir}[m.group(1)], text)
    return sub


def parse_env(text: str) -> dict:
    env = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
            v = v[1:-1]
        env[k.strip()] = v
    return env


def service_env(svc: dict, issue: str, sub) -> dict:
    env = {}
    files = svc.get("env_file") or []
    for f in [files] if isinstance(files, str) else files:
        f = f["path"] if isinstance(f, dict) else f
        p = os.path.join(issue, f)
        if os.path.exists(p):
            env.update(parse_env(open(p).read()))
    extra = svc.get("environment") or {}
    if isinstance(extra, list):
        extra = dict(e.split("=", 1) if "=" in e else (e, "") for e in extra)
    env.update({k: str(v) for k, v in extra.items()})
    return {k: sub(v) for k, v in env.items()}


def service_command(svc: dict) -> str | None:
    cmd = svc.get("command")
    if cmd is None:
        return None
    if isinstance(cmd, list):
        import shlex
        return " ".join(shlex.quote(c) for c in cmd)
    return str(cmd)


# --------------------------------------------------------------------- run

_SUMMARY = re.compile(r"=+ (.*?) in [\d.]+s")


def pytest_counts(log: str) -> dict:
    counts = {}
    for m in _SUMMARY.finditer(log):
        for part in m.group(1).split(","):
            bits = part.strip().split(" ", 1)
            if len(bits) == 2 and bits[0].isdigit():
                counts[bits[1]] = counts.get(bits[1], 0) + int(bits[0])
    return counts


def run_datapoint(dp: dict, files: dict | None, timeout: int, keep: bool,
                  base: str | None) -> Result:
    res = Result(id=dp["id"], status="error", categories=dp.get("categories", []))
    hfiles = harness_files(dp)
    if not hfiles:
        res.note = "no harness"
        res.status = "skipped"
        return res
    compose_txt = hfiles.get("docker-compose.yml", "")
    try:
        compose = yaml.safe_load(compose_txt) or {}
    except yaml.YAMLError as e:
        res.note = f"bad compose: {e}"
        return res
    services = compose.get("services") or {}
    if not services:
        res.status, res.note = "skipped", "no services (LLM-judged datapoint)"
        return res
    for name, svc in services.items():
        image = str(svc.get("image", ""))
        if "build" in svc:
            image = hfiles.get("Dockerfile", "")
        if image and not any(i in image for i in OSS_IMAGES):
            res.status, res.note = "skipped", f"service {name} needs image {image.split()[-1] if image else '?'}"
            return res

    if base:
        os.makedirs(base, exist_ok=True)
    issue = tempfile.mkdtemp(prefix=f"cvdp_{dp['id']}_", dir=base)
    res.workdir = issue
    t0 = time.time()
    try:
        for d in ISSUE_DIRS:
            os.makedirs(os.path.join(issue, d), exist_ok=True)
        os.makedirs(os.path.join(issue, "rundir", "harness"), exist_ok=True)
        sub = rewriter(issue)

        def put(rel, text, rewrite=False):
            p = os.path.join(issue, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            if rewrite and (rel.endswith(TEXT_SUFFIXES) or "/." in rel):
                text = sub(text)
            with open(p, "w") as fh:
                fh.write(text)

        for rel, text in context_files(dp).items():
            put(rel, text)
        if files is None:                       # --solutions
            sol = solution_files(dp, issue)
            if isinstance(sol, str):
                res.note = sol
                return res
            files = sol
        for rel, text in files.items():
            put(rel, text)
        for rel, text in hfiles.items():
            if rel in ("docker-compose.yml", "Dockerfile"):
                continue
            put(rel, text, rewrite=rel.startswith("src/"))

        base_env = {k: v for k, v in os.environ.items()}
        base_env["PATH"] = os.path.dirname(sys.executable) + os.pathsep + base_env.get("PATH", "")
        for name, svc in services.items():
            cmd = service_command(svc)
            if cmd is None:
                res.services.append({"name": name, "rc": None, "note": "no command"})
                continue
            env = dict(base_env)
            env.update(service_env(svc, issue, sub))
            log_path = os.path.join(issue, f"service_{name}.log")
            left = max(1, timeout - int(time.time() - t0))
            try:
                with open(log_path, "w") as log:
                    p = subprocess.run(["bash", "-c", sub(cmd)], cwd=os.path.join(issue, "rundir"),
                                       env=env, stdout=log, stderr=subprocess.STDOUT,
                                       timeout=left, start_new_session=True)
                rc = p.returncode
            except subprocess.TimeoutExpired:
                rc = "timeout"
            text = open(log_path, errors="replace").read()
            counts = pytest_counts(text)
            for k, v in counts.items():
                res.tests[k] = res.tests.get(k, 0) + v
            tail = ""
            if rc != 0:
                tail = text[-1500:]
                # cocotb sends compiler output to build.log/sim.log, not stdout
                for dirpath, _, fnames in os.walk(os.path.join(issue, "rundir")):
                    for fn in fnames:
                        if fn in ("build.log", "sim.log"):
                            blog = open(os.path.join(dirpath, fn), errors="replace").read()
                            tail += f"\n--- {fn} ---\n" + blog[-1500:]
            res.services.append({"name": name, "rc": rc, "tests": counts, "tail": tail})
            if rc == "timeout":
                break
        rcs = [s["rc"] for s in res.services if s["rc"] is not None]
        if "timeout" in rcs:
            res.status = "timeout"
        elif rcs and all(rc == 0 for rc in rcs):
            res.status = "pass"
        elif rcs:
            res.status = "fail"
        else:
            res.note = "no runnable services"
        return res
    except Exception as e:                      # noqa: BLE001
        res.note = f"{type(e).__name__}: {e}"
        return res
    finally:
        res.seconds = round(time.time() - t0, 1)
        if not keep and res.status == "pass":
            shutil.rmtree(issue, ignore_errors=True)
            res.workdir = ""


# --------------------------------------------------------------------- cli

def load_jsonl(path: str) -> list:
    with open(path) as fh:
        return [json.loads(l) for l in fh if l.strip()]


def preflight() -> list:
    problems = []
    try:
        v = subprocess.run(["iverilog", "-V"], capture_output=True, text=True).stdout.splitlines()[0]
        m = re.search(r"version (\d+)", v)
        if not m or int(m.group(1)) != 13:
            problems.append(f"Icarus should be 13 (the CVDP sim image); found: {v}")
    except (FileNotFoundError, IndexError):
        problems.append("iverilog not on PATH")
    with tempfile.TemporaryDirectory() as d:     # does it actually compile and run?
        src = os.path.join(d, "t.sv")
        with open(src, "w") as fh:
            fh.write("module t; initial begin $display(\"ivl-ok\"); $finish; end endmodule\n")
        try:
            c = subprocess.run(["iverilog", "-g2012", "-o", os.path.join(d, "t.vvp"), src],
                               capture_output=True, text=True, timeout=60)
            r = subprocess.run(["vvp", os.path.join(d, "t.vvp")], capture_output=True,
                               text=True, timeout=60) if c.returncode == 0 else None
            if c.returncode or r is None or "ivl-ok" not in r.stdout:
                problems.append("iverilog cannot compile a trivial design: "
                                + (c.stdout + c.stderr + (r.stdout + r.stderr if r else "")).strip()[:600])
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            problems.append(f"iverilog cannot compile a trivial design: {e}")
    for mod, want in (("cocotb", "2.0.1"), ("pytest", "8.3.2")):
        try:
            got = __import__(mod).__version__
            if got != want:
                problems.append(f"{mod} {got}, the CVDP image has {want}")
        except ImportError:
            problems.append(f"{mod} not installed")
    for mod in ("cocotb_bus", "cocotbext.axi"):
        try:
            __import__(mod)
        except ImportError:
            problems.append(f"{mod} not installed (some datapoints import it)")
    return problems


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dataset")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--solutions", action="store_true", help="run the reference solutions")
    g.add_argument("--candidates", help='JSONL of {"id", "files"}')
    g.add_argument("--skeleton", action="store_true",
                   help="submit the given input.context files unchanged (do-nothing baseline; "
                        "datapoints whose expected files are not in the input are skipped)")
    ap.add_argument("--ids", nargs="*", help="only these ids")
    ap.add_argument("--category", nargs="*", help="only these categories, e.g. cid002 cid003")
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--timeout", type=int, default=600, help="seconds per datapoint")
    ap.add_argument("--keep", action="store_true", help="keep work dirs of passing runs too")
    ap.add_argument("--workdir", default=None, help="where to create work dirs")
    ap.add_argument("--out", default=None, help="results JSON")
    ap.add_argument("--no-preflight", action="store_true")
    args = ap.parse_args()

    if not args.no_preflight:
        issues = preflight()
        for p in issues:
            print("preflight:", p, file=sys.stderr)
        fatal = ("iverilog not on PATH", "cocotb not installed", "pytest not installed",
                 "iverilog cannot compile")
        if any(p.startswith(fatal) for p in issues):
            return 2

    data = load_jsonl(args.dataset)
    if args.ids:
        data = [d for d in data if d["id"] in set(args.ids)]
    if args.category:
        data = [d for d in data if set(args.category) & set(d.get("categories", []))]
    if args.ids:
        unknown = set(args.ids) - {d["id"] for d in data}
        if unknown:
            print(f"ids not in {os.path.basename(args.dataset)}: {sorted(unknown)}", file=sys.stderr)
    print(f"{len(data)} datapoints selected from {os.path.basename(args.dataset)}", flush=True)
    if not data:
        return 1
    cands = {}
    if args.candidates:
        cands = {c["id"]: c["files"] for c in load_jsonl(args.candidates)}
        missing = [d["id"] for d in data if d["id"] not in cands]
        if missing:
            print(f"{len(missing)} datapoints have no candidate; recorded as fail", file=sys.stderr)

    if args.skeleton:
        for dp in data:
            out = list((dp.get("output") or {}).get("context") or {})
            ctx = context_files(dp)
            if out and all(f in ctx for f in out):
                cands[dp["id"]] = {f: ctx[f] for f in out}

    def one(dp):
        if args.skeleton and dp["id"] not in cands:
            return Result(id=dp["id"], status="skipped", categories=dp.get("categories", []),
                          note="no skeleton in input.context")
        if args.candidates and dp["id"] not in cands:
            return Result(id=dp["id"], status="fail", categories=dp.get("categories", []),
                          note="no candidate")
        r = run_datapoint(dp, None if args.solutions else cands[dp["id"]],
                          args.timeout, args.keep, args.workdir)
        print(f"{r.status:8s} {r.seconds:6.1f}s  {r.id}  {r.note}", flush=True)
        return r

    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        results = list(pool.map(one, data))

    from collections import Counter
    c = Counter(r.status for r in results)
    print("\n" + ", ".join(f"{k} {v}" for k, v in c.most_common()))
    ran = c["pass"] + c["fail"] + c["timeout"] + c["error"]
    if ran:
        print(f"pass rate {c['pass']}/{ran} = {100 * c['pass'] / ran:.1f}% (skipped excluded)")
    if args.out:
        tmp = args.out + ".tmp"
        with open(tmp, "w") as fh:
            json.dump([asdict(r) for r in results], fh, indent=1)
        os.replace(tmp, args.out)
        print("results:", args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
