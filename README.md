# RTLForge

A generate → verify → repair loop for LLM-written RTL. The model proposes and
repairs; Verilator, Icarus, and Yosys decide whether it is correct.

The experiment this is built to run: **how much of the gap between a small
local model and a large cloud one can be closed by giving the small model
deterministic tool feedback?** Every feedback level is a separate run
configuration so the ablation is one command.

## Status

Working: parsers, runners, repair loop, CLI, one validated problem, container
definitions. Not yet built: cocotb regression, SymbiYosys, additional problems,
result plotting.

## Quick start

```bash
pip install -r requirements.txt

# 1. Harness self-test -- no LLM, no API key. Do this first.
python -m rtlforge.cli check --problem problems/counter --rtl examples/counter_good.v
python -m rtlforge.cli check --problem problems/counter --rtl examples/counter_sync_reset_bug.v

# 2. Add a key, then one real run
cp .env.example .env    # fill in GROQ_API_KEY or GEMINI_API_KEY
python -m rtlforge.cli run --problem problems/counter --level full

# 3. The experiment
python -m rtlforge.cli sweep --problem problems/counter --trials 5
```

Requires `verilator`, `iverilog`, `yosys` on PATH, or use the containers.\n\n**New machine? Follow [SETUP.md](SETUP.md)** — WSL2, Docker, VS Code, and the\nlab-GPU migration, with a checkpoint after every step.

## Feedback levels

| Level     | Signals returned to the model                    |
|-----------|--------------------------------------------------|
| `none`    | nothing — single-shot baseline                   |
| `lint`    | Verilator                                        |
| `compile` | + Icarus                                         |
| `sim`     | + trusted testbench simulation                   |
| `full`    | + Yosys synthesis                                |

`none` is scored against the *same* full stage set as the others, so the
baseline and the treatment are measured identically. Only the repair signal
differs.

## Providers

All three speak the OpenAI chat format, so switching is a flag.

```bash
--provider groq     # gpt-oss-120b, published limits, no card
--provider gemini   # current-gen Flash, no card
--provider ollama   # local; qwen2.5-coder:7b by default
```

Free-tier arithmetic: 30 problems × 5 iterations ≈ 150 calls, inside a single
day on either free tier. The **token** ceiling binds before the request count,
which is why `parsers.py` returns structured diagnostics instead of raw logs —
raw Verilator output is roughly 4× the tokens for the same information.

Note: Gemini's free tier uses submitted content to improve Google's products;
the paid tier does not. Fine for open benchmark problems, worth knowing before
you point it at your own RTL.

## Containers

```bash
docker compose build
docker compose up -d
docker compose exec agent python -m rtlforge.cli run --problem problems/counter
```

The `eda` service runs with `network_mode: none`. Generated RTL is untrusted
input and is only ever executed there.

If your site does not grant the `docker` group, the same images run under
Apptainer, or skip containers entirely — `apt install verilator iverilog yosys`
plus a venv is all the harness needs.

## Trusted testbenches

Testbenches live beside each problem, are copied read-only into the work
directory, and are never shown to the design model. A design and a testbench
generated from the same misunderstanding pass together and prove nothing.

Each testbench prints `MISMATCH:` lines and ends with `TB_PASS` or `TB_FAIL`.
A run with neither marker is scored as a failure — a testbench that printed no
verdict verified nothing.

**Validate every new testbench against a known-good design and at least two
known-bad ones before trusting it.** `problems/counter/tb.v` was written wrong
twice: 128-bit label strings silently truncated the messages, and `rst_n`
initialised to `0` never produced a negedge, so a correct async-reset design
read `x` and failed. Both bugs would have shown up as model failures.
`examples/` holds the bug designs used for that validation.

## Adding a problem

```
problems/<name>/
  problem.json   {"top": "<module_name>", "testbench": "tb.v"}
  spec.md        natural-language spec given to the model
  tb.v           trusted oracle, never shown to the model
```

Before scaling up: pull VerilogEval v2 or RTLLM rather than hand-writing 30
oracles. You get immutable testbenches for free and your numbers become
comparable to published work. Check which problems you trust first — published
work has found flawed cases in the existing RTL benchmarks.

## Design notes

- **Stages are gated.** Simulation output for RTL that does not compile is
  wasted tokens.
- **Lint warnings are advisory by default.** `DECLFILENAME` would otherwise
  send the model into repair cycles on functionally correct designs. Use
  `--strict-lint` to test whether warning-level feedback helps or hurts — that
  ablation is interesting on its own.
- **Every attempt is written to disk before the next begins**, so a run that
  dies at iteration 4 still yields analysable data.
- **Yosys `stat` prints per-module blocks**; the parser takes the last cell
  count, not the first.

## Prior work

This is a reimplementation of a well-covered idea, not a novel one. AutoChip
(Blocklove et al., arXiv 2411.11856) is the closest reference and is open
source. RTLFixer covers syntax repair, VerilogCoder the simulation loop,
SpecLoop the formal loop. Read AutoChip before claiming anything here is new.

The open question worth chasing: AutoChip found tool feedback consistently
helped only their *strongest* model. Where that threshold sits for small
open-weight coders is not well characterised, and it is what this harness is
shaped to measure.

## Next

1. More problems — FSM, ALU, FIFO, then VerilogEval v2.
2. Failure taxonomy: log why repair loops stall (oscillation between two wrong
   fixes, error misattribution, iteration-count distributions).
3. Feedback-representation ablation: raw log vs. structured, held constant.
4. Ollama on local hardware, then the small-model-vs-feedback comparison.
