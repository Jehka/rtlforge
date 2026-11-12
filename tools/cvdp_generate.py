#!/usr/bin/env python3
"""Generate CVDP non-agentic code-generation answers with a local model.

Reproduces the upstream CVDP prompt and response parsing
(NVlabs/cvdp_benchmark, Apache-2.0: src/dataset_processor.py
create_context, src/model_helpers.py create_system_prompt /
determine_schema / parse_model_response), so pass rates are comparable
with published CVDP numbers. Writes one candidates file per sample,
in the format tools/cvdp_run.py --candidates reads.

    python tools/cvdp_generate.py data/cvdp/cvdp_v1.1.0_nonagentic_code_generation_no_commercial.jsonl \
        --model qwen3.8:27b --reasoning-effort low --samples 3 --out-dir cvdp_gen/qwen38
    python tools/cvdp_run.py DATA.jsonl --candidates cvdp_gen/qwen38/cand_s0.jsonl --out res_s0.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

# ---- verbatim from cvdp_benchmark src/model_helpers.py --------------------
FOLDERS = """You are a helpful assistance.
Consider that you have a folder structure like the following:\n
    - rtl/*   : Contains files which are RTL code.
    - verif/* : Contains files which are used to verify the correctness of the RTL code.
    - docs/*  : Contains files used to document the project, like Block Guides, RTL Plans and Verification Plans.

When generating files, return the file name in the correct place at the folder structure.
"""
SCHEMA = ['{ "code": [{ "<name>" : "<code>"}] }', '{ "response": "<response>" }']
GUIDANCE = {
    2: "You are solving an 'RTL Code Completion' problem. To solve this problem correctly, you should only respond with the RTL code generated according to the requirements.",
    3: "You are solving a 'Specification to RTL Translation' problem. To solve this problem correctly, you should only respond with the RTL code translated from the specification.",
    4: "You are solving an 'RTL Code Modification' problem. To solve this problem correctly, you should only respond with the modified RTL code according to the requirements.",
    5: "You are solving a 'Specification to RTL Translation: Module Instantiation and Component Reuse' problem. To solve this problem correctly, you should only respond with the RTL code translated from the specification and with proper module instantiation and component reuse.",
    7: "You are solving an 'RTL Lint Improvement or Power-Performance Optimization' problem. To solve this problem correctly, you should only respond with improved RTL code to address lint issues or optimize for power/performance.",
    16: "You are solving an 'RTL Debugging and Bug Fixing' problem. To solve this problem correctly, you should only respond with the RTL code that is debugged and fixed to address the bug.",
}
CODE_GEN = (2, 3, 4, 5, 7, 12, 13, 14, 16)
# ---------------------------------------------------------------------------


def system_prompt(category: int, schema) -> str:
    s = FOLDERS + f"\n{GUIDANCE[category]}\n"
    if schema is not None:
        s += "\nProvide the response in one of the following JSON schemas: \n"
        s += "\nor\n".join(schema)
        s += ("\nThe response should be in JSON format, including double-quotes around keys "
              "and values, and proper escaping of quotes within values, and escaping of newlines.")
    return s


def user_prompt(dp: dict) -> tuple[str, list]:
    prompt = ""
    for f, cxt in (dp["input"].get("context") or {}).items():
        prompt += f"\nConsider the following content for the file {f}:\n```\n{cxt}\n```"
    prompt += f"\nProvide me one answer for this request: {dp['input']['prompt']}\n"
    files = list((dp["output"].get("context") or {}).keys())
    if len(files) == 1:
        prompt += (f"Please provide your response as plain text without any JSON formatting. "
                   f"Your response will be saved directly to: {files[0]}.\n")
    elif len(files) > 1:
        prompt += f"Name the files as: {files}.\n"
    else:
        prompt += "Provide your response below.\n"
    return prompt, files


def _blocks(text: str) -> list:
    out, pos = [], 0
    pat = re.compile(r"```(?:(\w+)\n)?(.*?)```", re.S)
    while True:
        m = pat.search(text, pos)
        if not m:
            return out
        out.append(m.group(2))
        pos = m.end()


def parse_response(res: str, files: list) -> dict | None:
    """Upstream parse_model_response, reduced to code generation. Returns
    {file: text}, or None when a multi-file JSON answer does not parse."""
    if len(files) == 1:
        b = _blocks(res)
        return {files[0]: b[0] if b else res.strip()}
    text = re.sub(r"[\x00-\x1F\x7F]", "", res.strip())
    start, end = text.find("{"), text[::-1].find("}")
    try:
        out = json.loads(text[start:-end if end else None])
    except (json.JSONDecodeError, ValueError):
        return None
    result = {}
    for item in out.get("code", []) if isinstance(out, dict) else []:
        for name, code in item.items():
            b = _blocks(code) if isinstance(code, str) else []
            result[name] = b[0] if b else code
    return result or None


def strip_think(text: str) -> str:
    """Reasoning models served by Ollama may inline <think>...</think>."""
    return re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dataset")
    ap.add_argument("--model", required=True)
    ap.add_argument("--provider", default="ollama")
    ap.add_argument("--reasoning-effort", default=None)
    ap.add_argument("--max-tokens", type=int, default=16384)
    ap.add_argument("--temperature", type=float, default=0.2)
    ap.add_argument("--samples", type=int, default=1)
    ap.add_argument("--category", nargs="*", default=None, help="e.g. cid002 cid003 (default: all code generation)")
    ap.add_argument("--ids", nargs="*")
    ap.add_argument("--retries", type=int, default=3, help="re-ask when a multi-file answer does not parse (upstream: 3)")
    ap.add_argument("--out-dir", required=True)
    args = ap.parse_args()

    from spec_query_eval import MAX_CONSECUTIVE_ERRORS, server_up
    if args.provider == "ollama" and not server_up(os.environ.get("OLLAMA_BASE_URL")):
        print("no Ollama server -- source tools/gpu_up.sh first", file=sys.stderr)
        return 2
    from rtlforge.providers import LLMClient
    client = LLMClient(provider=args.provider, model=args.model, temperature=args.temperature,
                       max_tokens=args.max_tokens, reasoning_effort=args.reasoning_effort)

    data = [json.loads(l) for l in open(args.dataset) if l.strip()]
    cat = lambda d: int(d["categories"][0][3:])
    data = [d for d in data if cat(d) in CODE_GEN and cat(d) in GUIDANCE]
    if args.category:
        data = [d for d in data if d["categories"][0] in set(args.category)]
    if args.ids:
        data = [d for d in data if d["id"] in set(args.ids)]
    os.makedirs(args.out_dir, exist_ok=True)
    print(f"{len(data)} datapoints x {args.samples} samples -> {args.out_dir}", flush=True)

    streak = 0
    for s in range(args.samples):
        path = os.path.join(args.out_dir, f"cand_s{s}.jsonl")
        done = set()
        if os.path.exists(path):                       # resume
            done = {json.loads(l)["id"] for l in open(path) if l.strip()}
        with open(path, "a") as fh:
            for n, dp in enumerate(data, 1):
                if dp["id"] in done:
                    continue
                prompt, files = user_prompt(dp)
                schema = None if len(files) == 1 else SCHEMA
                msgs = [{"role": "system", "content": system_prompt(cat(dp), schema)},
                        {"role": "user", "content": prompt}]
                parsed, raw, err = None, "", ""
                for _ in range(1 + args.retries):
                    try:
                        raw = strip_think(client.chat(msgs))
                        err = ""
                    except Exception as exc:              # noqa: BLE001
                        raw, err = "", str(exc)
                        break
                    parsed = parse_response(raw, files)
                    if parsed is not None:
                        break
                streak = streak + 1 if err else 0
                if streak >= MAX_CONSECUTIVE_ERRORS:
                    print(f"aborted after {streak} consecutive errors: {err[:200]}", file=sys.stderr)
                    return 3
                fh.write(json.dumps({"id": dp["id"], "sample": s, "files": parsed or {},
                                     "parse_ok": parsed is not None, "error": err,
                                     "finish_reason": getattr(client, "last_finish_reason", None),
                                     "raw": raw}) + "\n")
                fh.flush()
                if n % 10 == 0:
                    print(f"  sample {s}: {n}/{len(data)}", flush=True)
        print(f"sample {s}: {path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
