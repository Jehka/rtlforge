"""Produce a Verilator JSON AST for a design.

Kept separate from runners.py because this is not a pass/fail stage: a
failed AST dump must never fail a design. If Verilator cannot elaborate
the RTL, the cone is simply unavailable and the repair prompt falls back
to the diagnostics it already had.

Honours the same backend convention as runners.py:
``RTLFORGE_EDA=local|docker`` and ``RTLFORGE_EDA_WORKDIR``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass

EDA_BACKEND = os.environ.get("RTLFORGE_EDA", "local")
EDA_WORKDIR = os.environ.get("RTLFORGE_EDA_WORKDIR", "/work")
EDA_SERVICE = os.environ.get("RTLFORGE_EDA_SERVICE", "eda")

# Verilator gained --json-only in 5.0x; --xml-only was removed / never
# compiled in on the builds we use. Confirmed present in 5.052.
JSON_FLAG = "--json-only"


@dataclass
class AstResult:
    tree: dict | None
    available: bool
    note: str = ""
    raw: str = ""


def _run(cmd: list[str], cwd: str, timeout: int = 60
         ) -> tuple[int, str]:
    try:
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                              timeout=timeout)
    except FileNotFoundError:
        return 127, f"NOTFOUND: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return 124, f"TIMEOUT: {' '.join(cmd)}"
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def _infra_failure(rc: int, out: str) -> bool:
    """True when the tool never ran, as opposed to finding problems."""
    return rc in (126, 127) or out.startswith(("NOTFOUND:", "TIMEOUT:"))


def dump_ast(rtl: str, top: str | None = None, workdir: str | None = None
             ) -> AstResult:
    """Elaborate *rtl* (source text) and return its parsed JSON AST."""
    owned = workdir is None
    workdir = workdir or tempfile.mkdtemp(prefix="rtlforge-ast-")
    if owned:
        os.chmod(workdir, 0o777)
    try:
        name = f"{top or 'design'}.v"
        with open(os.path.join(workdir, name), "w") as fh:
            fh.write(rtl)
        out_json = "ast.json"

        args = [JSON_FLAG, "--json-only-output", out_json, "-Wno-fatal"]
        if top:
            args += ["--top-module", top]
        args.append(name)

        if EDA_BACKEND == "docker":
            cmd = ["docker", "compose", "run", "--rm",
                   "-v", f"{workdir}:{EDA_WORKDIR}",
                   "-w", EDA_WORKDIR, EDA_SERVICE, "verilator"] + args
        else:
            cmd = ["verilator"] + args

        rc, out = _run(cmd, cwd=workdir)

        if _infra_failure(rc, out):
            return AstResult(None, False,
                             note=f"verilator unavailable: {out.strip()[:200]}",
                             raw=out)

        path = os.path.join(workdir, out_json)
        if not os.path.exists(path):
            # Verilator writes the tree next to the obj dir in some builds.
            alt = os.path.join(workdir, "obj_dir", out_json)
            path = alt if os.path.exists(alt) else path
        if not os.path.exists(path):
            return AstResult(None, False,
                             note="verilator produced no JSON AST "
                                  f"(rc={rc})", raw=out)
        try:
            with open(path) as fh:
                tree = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            return AstResult(None, False,
                             note=f"unreadable JSON AST: {exc}", raw=out)
        return AstResult(tree, True, raw=out)
    finally:
        if owned:
            shutil.rmtree(workdir, ignore_errors=True)
