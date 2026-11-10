#!/usr/bin/env python3
"""Wire cone_hint() into loop.py's repair path.

Idempotent and fail-loud: every edit is checked before anything is
written, so a loop.py that does not look as expected is left untouched
and the script says which edit did not match. Keeps a timestamped
backup and byte-compiles the result before replacing the original.

    python tools/patch_cone_into_loop.py            # apply
    python tools/patch_cone_into_loop.py --dry-run  # show, change nothing
    python tools/patch_cone_into_loop.py --revert   # restore newest backup
"""

from __future__ import annotations

import argparse
import datetime as dt
import glob
import os
import py_compile
import re
import shutil
import sys
import tempfile

TARGET = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "rtlforge", "loop.py")


class Unmatched(Exception):
    pass


def _sub_once(text: str, pattern: str, repl: str, what: str,
              flags: int = 0) -> str:
    new, n = re.subn(pattern, repl, text, count=1, flags=flags)
    if n != 1:
        raise Unmatched(what)
    return new


# ---------------------------------------------------------------- edits

def add_import(text: str) -> str:
    if re.search(r"^from \.cone import cone_hint$", text, re.M):
        return text
    return _sub_once(
        text,
        r"^(from \.parsers import [^\n]*\n)",
        r"\1from .cone import cone_hint\n",
        "import line (expected 'from .parsers import ...')",
        re.M)


def _repair_prompt_slice(text: str) -> tuple[int, int]:
    start = text.find("def _repair_prompt(")
    if start < 0:
        raise Unmatched("def _repair_prompt(")
    nxt = re.search(r"^def ", text[start + 1:], re.M)
    end = start + 1 + nxt.start() if nxt else len(text)
    return start, end


def extend_signature(text: str) -> str:
    start, end = _repair_prompt_slice(text)
    body = text[start:end]
    if "cone: str" in body:
        return text

    # Every replacement below goes through a lambda: re.sub processes
    # escape sequences in a plain replacement string, which turns the \n
    # inside the generated f-string into a real newline and produces an
    # unterminated literal.
    body = _sub_once(
        body,
        r'(history: str = "")(\s*\)\s*->\s*List\[dict\]:)',
        lambda m: (m.group(1) + ',\n                   cone: str = ""'
                   + m.group(2)),
        "_repair_prompt signature (expected 'history: str = \"\"')")

    evidence = ('    evidence = f"\\nLine-level evidence:\\n{cone}\\n"'
                ' if cone else ""')
    body = _sub_once(
        body,
        r"(\n)(\s*return \[)",
        lambda m: m.group(1) + evidence + "\n" + m.group(2),
        "_repair_prompt body (expected 'return [')")

    body = _sub_once(
        body,
        r'(f"Tool diagnostics:\\n\{feedback\}\\n"\n)',
        lambda m: m.group(1) + '                f"{evidence}"\n',
        "_repair_prompt content (expected the 'Tool diagnostics' line)")

    return text[:start] + body + text[end:]


def patch_call_site(text: str) -> str:
    # Either form means this edit already ran: a later edit renames
    # cone_hint( to repair_evidence(, and re-running the patcher must
    # stay a no-op rather than hunt for the original call site.
    if "cone = cone_hint(" in text or "cone = repair_evidence(" in text:
        return text
    return _sub_once(
        text,
        r"( *)prompt = _repair_prompt\(problem, repair_from, failed_stage,\n"
        r" *feedback, history\)",
        lambda m: (
            f"{m.group(1)}cone = \"\"\n"
            f"{m.group(1)}if failed_stage == \"simulate\":\n"
            f"{m.group(1)}    cone = cone_hint(repair_from, problem.top,\n"
            f"{m.group(1)}                     feedback)\n"
            f"{m.group(1)}prompt = _repair_prompt(problem, repair_from, "
            f"failed_stage,\n"
            f"{m.group(1)}                        feedback, history, cone)"),
        "_repair_prompt call site")


def add_attempt_field(text: str) -> str:
    """Record the cone on the attempt whose failure it explains.

    Without this the cone never reaches the per-attempt JSON, and the
    only way to know what evidence a repair actually received is to
    infer it -- which is how every harness bug so far survived as long
    as it did.
    """
    if re.search(r"^    cone: str = \"\"", text, re.M):
        return text
    return _sub_once(
        text,
        r"^(    from_timing_repair: bool = False[^\n]*\n)",
        lambda m: (m.group(1)
                   + '    cone: str = ""             '
                     '# line-level evidence sent to the repair prompt\n'),
        "Attempt field (expected 'from_timing_repair: bool = False')",
        re.M)


def add_serialisation(text: str) -> str:
    if '"cone": a.cone' in text:
        return text
    return _sub_once(
        text,
        r'"stages": a\.stages,',
        lambda m: '"stages": a.stages,\n                 "cone": a.cone,',
        "attempt serialisation (expected '\"stages\": a.stages,')")


def record_cone_on_attempt(text: str) -> str:
    if "attempt.cone = cone" in text:
        return text
    return _sub_once(
        text,
        r"( *)(prompt = _repair_prompt\(problem, repair_from, failed_stage,)",
        lambda m: f"{m.group(1)}attempt.cone = cone\n{m.group(1)}{m.group(2)}",
        "cone recording point (run the earlier edits first)")


def add_feedback_field(text: str) -> str:
    """Log the full diagnostics text, waveform trace included.

    The JSON held structured diagnostics but not the assembled feedback,
    so whether a repair prompt carried the signal trace was unknowable
    from the logs -- grepping for it returned 0 whether it was sent or not.
    """
    if re.search(r"^    feedback: str = \"\"", text, re.M):
        return text
    return _sub_once(
        text,
        r"^(    cone: str = \"\"[^\n]*\n)",
        lambda m: (m.group(1)
                   + '    feedback: str = ""         '
                     '# full diagnostics text sent to the repair prompt\n'),
        "feedback field (run the attempt-field edit first)",
        re.M)


def add_feedback_serialisation(text: str) -> str:
    if '"feedback": a.feedback' in text:
        return text
    return _sub_once(
        text,
        r'"cone": a\.cone,',
        lambda m: '"cone": a.cone,\n                 "feedback": a.feedback,',
        "feedback serialisation (run the cone serialisation edit first)")


def record_feedback_on_attempt(text: str) -> str:
    if "attempt.feedback = feedback" in text:
        return text
    return _sub_once(
        text,
        r"( *)attempt\.cone = cone\n",
        lambda m: (f"{m.group(1)}attempt.cone = cone\n"
                   f"{m.group(1)}attempt.feedback = feedback\n"),
        "feedback recording point (run the cone recording edit first)")


def add_rtl_serialisation(text: str) -> str:
    """Log what the model actually wrote on every attempt.

    Attempt.rtl existed but was never serialised, so a failing attempt's
    design could only be inferred from the cone's suspect lines.
    """
    if '"rtl": a.rtl' in text:
        return text
    return _sub_once(
        text,
        r'"feedback": a\.feedback,',
        lambda m: '"feedback": a.feedback,\n                 "rtl": a.rtl,',
        "rtl serialisation (run the feedback serialisation edit first)")


def add_evidence_import(text: str) -> str:
    if "from .evidence import" in text:
        return text
    return _sub_once(
        text,
        r"^(from \.cone import cone_hint\n)",
        lambda m: (m.group(1) + "from .evidence import "
                   "prioritise_diagnostics, repair_evidence\n"),
        "evidence import (run the cone import edit first)",
        re.M)


def prioritise_feedback(text: str) -> str:
    """Errors before warnings, harness noise dropped, for every stage.

    The 2026-11-07 diagnostic showed lint feedback cut at 12 warnings
    with the actual error omitted -- five-attempt loops fixing the
    wrong thing.
    """
    if "prioritise_diagnostics(sr.diagnostics)" in text:
        return text
    return _sub_once(
        text,
        r"feedback = render_feedback\(sr\.diagnostics\)",
        lambda m: ("feedback = render_feedback("
                   "prioritise_diagnostics(sr.diagnostics))"),
        "feedback rendering (expected "
        "'feedback = render_feedback(sr.diagnostics)')")


def use_repair_evidence(text: str) -> str:
    # Not "repair_evidence(repair_from": the no-effect edit splits that
    # call across lines, and re-running must still be a no-op.
    if "cone = repair_evidence(" in text:
        return text
    return _sub_once(
        text,
        r"cone = cone_hint\(repair_from, problem\.top,",
        lambda m: "cone = repair_evidence(repair_from, problem.top,",
        "cone_hint call (run the call-site edit first)")


def retitle_evidence(text: str) -> str:
    if "Evidence from simulation and the design:" in text:
        return text
    return _sub_once(
        text,
        r"Line-level evidence:",
        lambda m: "Evidence from simulation and the design:",
        "evidence heading (run the signature edit first)")


def pass_previous_feedback(text: str) -> str:
    """Give repair_evidence the previous attempt's feedback.

    The current attempt is already appended when the repair prompt is
    built, so the previous one is attempts[-2].
    """
    if "previous=(" in text:
        return text
    return _sub_once(
        text,
        r"( *)cone = repair_evidence\(repair_from, problem\.top,\s*feedback\)",
        lambda m: (
            f"{m.group(1)}cone = repair_evidence(\n"
            f"{m.group(1)}    repair_from, problem.top, feedback,\n"
            f"{m.group(1)}    previous=(getattr(result.attempts[-2], "
            f"\"feedback\", \"\")\n"
            f"{m.group(1)}              if len(result.attempts) > 1 "
            f"else \"\"))"),
        "repair_evidence call (run the trace + cone edit first)")


def add_resampled_field(text: str) -> str:
    if re.search(r"^    resampled: bool = False", text, re.M):
        return text
    return _sub_once(
        text,
        r"^(    feedback: str = \"\"[^\n]*\n)",
        lambda m: (m.group(1)
                   + "    resampled: bool = False    "
                     "# redrawn after an identical regeneration\n"),
        "resampled field (run the feedback field edit first)",
        re.M)


def add_resampled_serialisation(text: str) -> str:
    if '"resampled": a.resampled' in text:
        return text
    return _sub_once(
        text,
        r'"rtl": a\.rtl,',
        lambda m: '"rtl": a.rtl,\n                 "resampled": a.resampled,',
        "resampled serialisation (run the rtl serialisation edit first)")


def init_resample_flag(text: str) -> str:
    if "resampled_identical = False" in text:
        return text
    return _sub_once(
        text,
        r"^( *)seen_hashes: set = set\(\)\n",
        lambda m: (f"{m.group(1)}seen_hashes: set = set()\n"
                   f"{m.group(1)}resampled_identical = False   "
                   f"# one redraw per run\n"),
        "seen_hashes initialisation (expected 'seen_hashes: set = set()')",
        re.M)


RESAMPLE_MSG = ("That is exactly the code you returned before, and it failed "
                "as described. Return a corrected module whose behaviour "
                "differs from that code.")


def resample_on_identical(text: str) -> str:
    """Redraw once at higher temperature instead of stopping.

    160-run diagnostic, 2026-11-07: ~70% of failed runs in both arms ended
    on the first identical regeneration, and a prompt line asking the
    model not to repeat itself changed nothing (72% vs 70%). Repair runs
    at temperature 0.2, so the same prompt gives nearly the same answer.
    The redraw shows the model its repeated answer, says so, and samples
    at 0.8. If the redraw is also a repeat, the loop stalls as before.
    """
    if "resampled_identical = True" in text:
        return text

    def build(m):
        i8, i12 = m.group(1), m.group(2)
        i16 = i12 + "    "
        return (
            f"{i8}_last_prompt = locals().get(\"prompt\")\n"
            f"{i8}if (rtl_hash in seen_hashes and not resampled_identical\n"
            f"{i8}        and i > 1 and _last_prompt):\n"
            f"{i12}resampled_identical = True\n"
            f"{i12}_retry = list(_last_prompt) + [\n"
            f"{i16}{{\"role\": \"assistant\", \"content\": rtl}},\n"
            f"{i16}{{\"role\": \"user\", \"content\": {RESAMPLE_MSG!r}}}]\n"
            f"{i12}_saved_temp = client.temperature\n"
            f"{i12}client.temperature = max(_saved_temp, 0.8)\n"
            f"{i12}try:\n"
            f"{i16}rtl = _generate(client, _retry, result)\n"
            f"{i12}finally:\n"
            f"{i16}client.temperature = _saved_temp\n"
            f"{i12}design.write_text(rtl)\n"
            f"{i12}(workdir / f\"attempt_{{i:02d}}.v\").write_text(rtl)\n"
            f"{i12}rtl_hash = hashlib.sha256(rtl.encode()).hexdigest()[:12]\n"
            f"{i12}attempt = Attempt(index=i, rtl=rtl, rtl_hash=rtl_hash,\n"
            f"{i12}                  from_timing_repair=last_was_timing_repair)\n"
            f"{i12}attempt.resampled = True\n"
            f"{i8}if rtl_hash in seen_hashes:\n"
            f"{i12}attempt.failed_stage = \"generate\"")
    return _sub_once(
        text,
        r"( *)if rtl_hash in seen_hashes:\n( *)attempt\.failed_stage = \"generate\"",
        build,
        "identical-regeneration check (expected 'if rtl_hash in seen_hashes:')")


EDITS = [
    ("import", add_import),
    ("signature + body", extend_signature),
    ("call site", patch_call_site),
    ("attempt field", add_attempt_field),
    ("attempt serialisation", add_serialisation),
    ("cone recording", record_cone_on_attempt),
    ("feedback field", add_feedback_field),
    ("feedback serialisation", add_feedback_serialisation),
    ("feedback recording", record_feedback_on_attempt),
    ("rtl serialisation", add_rtl_serialisation),
    ("evidence import", add_evidence_import),
    ("errors-first feedback", prioritise_feedback),
    ("trace + cone evidence", use_repair_evidence),
    ("evidence heading", retitle_evidence),
    ("no-effect detection", pass_previous_feedback),
    ("resampled field", add_resampled_field),
    ("resampled serialisation", add_resampled_serialisation),
    ("resample flag", init_resample_flag),
    ("resample on identical", resample_on_identical),
]


# ----------------------------------------------------------------- main

def revert() -> int:
    backups = sorted(glob.glob(TARGET + ".bak.*"))
    if not backups:
        print("no backup found", file=sys.stderr)
        return 1
    newest = backups[-1]
    shutil.copy2(newest, TARGET)
    print(f"restored {TARGET} from {os.path.basename(newest)}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--revert", action="store_true")
    args = ap.parse_args()

    if args.revert:
        return revert()

    if not os.path.exists(TARGET):
        print(f"not found: {TARGET}", file=sys.stderr)
        return 1

    with open(TARGET) as fh:
        original = fh.read()

    text = original
    applied: list[str] = []
    for name, fn in EDITS:
        try:
            after = fn(text)
        except Unmatched as exc:
            print(f"FAILED on {name}: could not find {exc}", file=sys.stderr)
            print("nothing was changed. send me the surrounding lines and "
                  "I will adjust the patch.", file=sys.stderr)
            return 2
        applied.append(name if after != text else f"{name} (already present)")
        text = after

    if text == original:
        print("loop.py already patched; nothing to do")
        return 0

    # Byte-compile the candidate before it goes anywhere near the original.
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as tmp:
        tmp.write(text)
        probe = tmp.name
    try:
        py_compile.compile(probe, doraise=True, cfile=probe + "c")
    except py_compile.PyCompileError as exc:
        print(f"patched file does not compile, aborting:\n{exc}",
              file=sys.stderr)
        return 3
    finally:
        for path in (probe, probe + "c"):
            if os.path.exists(path):
                os.unlink(path)

    for name in applied:
        print(f"  ok  {name}")

    if args.dry_run:
        print("\n--dry-run: no files written")
        return 0

    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = f"{TARGET}.bak.{stamp}"
    shutil.copy2(TARGET, backup)
    with open(TARGET, "w") as fh:
        fh.write(text)
    print(f"\npatched {TARGET}")
    print(f"backup  {backup}")
    print("revert with: python tools/patch_cone_into_loop.py --revert")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
