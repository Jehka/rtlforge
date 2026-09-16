# RTLForge

A generate → verify → repair loop for LLM-written RTL. The model proposes and
repairs; Verilator, Icarus, and Yosys decide whether it is correct.

The experiment this is built to run: **how much of the gap between a small
local model and a large cloud one can be closed by giving the small model
deterministic tool feedback?** Every feedback level is a separate run
configuration so the ablation is one command.

**Backend architecture: [BACKEND.md](BACKEND.md)** — the spec-to-GDS plan, action-space rules, and cost analysis.

**First complete experiment: [FINDINGS.md](FINDINGS.md)** — 96 runs, 31% → 66%
on a discriminating subset, with the capability boundary and failure taxonomy.

## Status

Working: parsers, runners, repair loop, CLI, three validated problems,
oracle regression tests, container definitions. Not yet built: cocotb
regression, SymbiYosys, VerilogEval integration, result plotting.

## Problems

| Problem | Top | Tests |
|---|---|---|
| `counter` | `counter_en` | async reset, enable hold, 4-bit wrap |
| `seq_detect` | `seq_detect_1011` | overlapping detection, one-cycle pulse, reset mid-pattern, 400 random bits vs. reference |
| `adder32` | `adder32` | 32-bit registered add: corner cases, long carry chains, 500 random vectors. **The timing problem** -- two correct implementations (`adder32_ripple.v`, `adder32_fast.v`) with different critical paths, so repair has somewhere to go |
| `fifo` | `sync_fifo` | fill/drain ordering, overflow and underflow ignored, simultaneous r/w, pointer wrap, 600 random ops vs. reference |

Each has a known-good design and two to three known-bad ones in `examples/`.
`tests/test_oracles.py` asserts every testbench still passes the good design
and still fails each bug at the expected stage. Run it after touching any
testbench or upgrading a tool.

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
docker compose ps          # confirm the eda container's name

docker compose exec agent python -m rtlforge.cli check \
    --problem problems/counter --rtl examples/counter_good.v
```

The repo is bind-mounted at `/app`, so code and example edits take effect
without rebuilding. The agent dispatches tool commands into the `eda` container
via `RTLFORGE_EDA`, already set in the compose file. Both containers see the
same `work/` directory -- the agent through `/app/work`, the sandbox through
`/work` -- which is how the netlist written by one is read by the other.

If `docker compose ps` shows a different name than `rtlforge-eda-1` (Compose
derives it from the folder), override it:

```bash
docker compose exec -e RTLFORGE_EDA=docker:<actual-name> agent \
    python -m rtlforge.cli check --problem problems/counter \
    --rtl examples/counter_good.v
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

### VerilogEval

```bash
git clone --depth 1 https://github.com/NVlabs/verilog-eval.git ../verilog-eval
python -m rtlforge.cli import-verilogeval \
    --dataset ../verilog-eval/dataset_spec-to-rtl
python -m rtlforge.cli selftest --problems problems/verilogeval --write-manifest
```

156 problems import; 152 are usable. `selftest` applies two checks:

1. **Toolchain.** Run each problem's own reference through the pipeline.
   One problem fails to compile under Icarus 12.
2. **Spec sufficiency.** A reference always passes its own testbench, so
   that alone cannot tell whether the *prompt* is enough to reach it. Three
   problems have references that rely on an `initial` block for power-on
   state while the prompt never mentions an initial value, reset, or
   power-on behaviour. A spec-compliant design starts at `x` and mismatches
   immediately, so no correct answer can pass. `Prob034_dff8` is the clearest
   case: an 8-bit DFF that failed 6/6 in an early run purely for this reason.

Both classes are excluded from `usable.json`. Scoring a model on either
records a failure that belongs to the benchmark, not the model.

These testbenches compare against a golden RefModule, so they need none of the
hand-validation our own oracles required.

Before scaling up further: prefer these over hand-writing oracles. You get immutable testbenches for free and your numbers become
comparable to published work. Check which problems you trust first — published
work has found flawed cases in the existing RTL benchmarks.

## Running a benchmark

```bash
# one session's worth; stops cleanly when the quota runs out
python -m rtlforge.cli benchmark --problems problems/verilogeval \
    --levels none full --shuffle --limit 30 \
    --model openai/gpt-oss-120b --reasoning-effort low --max-tokens 3072

# next session: same command plus --out <that dir> --resume
```

Free-tier daily caps mean a full 155-problem benchmark spans several sessions.
Every completed run is written to disk immediately and `--resume` skips work
already done, so a 429 on the last problem costs nothing. Rate-limit failures
stop the run rather than being recorded as model failures -- a 429 says nothing
about the RTL.

`--shuffle` samples across the set. Without it a partial run only ever covers
alphabetically early problems, which are not a representative sample.

The summary reports pass rate per level and, more usefully, which problems
were **rescued** (failed single-shot, passed with the loop) and which
**regressed**. That contrast is the experiment; the aggregate percentage is
just its headline.

## Spending a limited token budget well

Free tiers give roughly 65 calls a day at 3K tokens each. Random sampling
wastes most of that: in one run, four problems passed 6/6 at both levels --
24 calls that could not have shown a feedback effect, because there was
nothing to repair.

`problems/discriminating.json` is a 16-problem subset built from observed
behaviour rather than guesswork:

- **10 discriminating** -- fail single-shot at least sometimes, and the
  repair loop sometimes recovers them. These carry the signal.
- **4 hard FSMs** -- multi-output sequential designs that failed every
  attempt at every level. These mark where feedback stops helping.
- **2 controls** -- pass reliably at both levels, confirming the pipeline
  is not the thing causing failures elsewhere.

```bash
python -m rtlforge.cli benchmark --problems problems/verilogeval \
    --manifest problems/discriminating.json --levels none full --trials 3 \
    --model openai/gpt-oss-120b --reasoning-effort low --max-tokens 3072
```

Rebuild the subset as you learn more: drop anything that passes 6/6, add
anything that fails interestingly. A problem that always passes and a problem
that always fails both cost the same tokens; only the ones that change
behaviour between levels are paying for themselves.

## Convergence controls

The first valid benchmark run showed the loop oscillating on multi-output
FSMs -- mismatch counts going 522 -> 398 -> 672 -> 504 -> 592, and on another
problem two byte-identical final attempts. Four controls address that:

- **Syntactic stall detection.** An attempt whose RTL hashes identically to an
  earlier one ends the run with `stop_reason: stalled`.
- **Behavioural stall detection.** A model can rewrite comments and signal
  names while producing functionally identical RTL -- distinct hash, identical
  mismatch counts. Observed on multi-output FSMs across three consecutive
  attempts. Each attempt is therefore also fingerprinted by its *observed
  failure*; three matches ends the run with
  `stop_reason: behaviourally stalled`.
- **Repair history.** Each repair prompt now lists prior attempts and their
  failure magnitudes. Without it the model re-proposes fixes it already made.
- **Rollback.** If an attempt regresses -- fails at an earlier stage, or
  mismatches more samples -- the next repair starts from the best attempt so
  far rather than the worst.
- **`rtl_hash` on every attempt**, so oscillation between repeated states is
  greppable rather than something you notice by eye.

`stop_reason` is reported per run: `passed`, `stalled`, `iteration budget`,
`no feedback signal for this stage`, `single-shot`.

### Reading the summary honestly

A problem that fails at `none` and passes at `full` on **attempt 1** saw no
feedback at all -- same prompt, different sample. That is temperature noise,
not repair. The benchmark summary separates these: `repaired by` counts only
runs with `iterations > 1`, and apparent rescues that passed first try are
reported separately as noise. In the first 30-problem run this was the
difference between a claimed +7 and a real +5.

## Timing repair

With `--through sta`, a timing violation feeds the critical path back and the
agent restructures logic to close it.

```bash
python -m rtlforge.cli run --problem problems/counter --level full \
    --through sta --liberty /path/to/cells.lib
```

Two things make this an optimisation loop rather than a second repair loop:

- **A separate prompt.** "Your design is wrong" and "your design is too slow"
  are different tasks. The functional prompt invites changing behaviour, which
  is exactly what must not happen when the design is already correct. The
  timing prompt states the module's behaviour and ports are fixed and asks
  only for delay reduction on the reported path.
- **The constraint is out of reach.** `run_sta` regenerates the SDC on every
  call, so the clock period is a property of the problem, not something the
  agent can relax. A design that meets timing because the clock slowed has not
  improved. Set it in `problem.json` via `clock_period_ns`.

### Rescuing a restructuring

A timing repair that introduces a functional bug is **not** rolled back on its
first failure. By stage rank a simulation failure scores far below the
correct-but-slow design it replaced, so plain rollback discards it -- which is
how a complete carry-lookahead adder with a one-line error was thrown away in
favour of the ripple adder it was meant to improve on, after which the loop
reproduced the slow design three times.

The new structure gets one attempt to be corrected, with a prompt that says
to keep it and fix only what is wrong. If the correction also fails, normal
rollback resumes -- the rescue is once per run, so a structure that will not
come good cannot consume the whole iteration budget.

`from_timing_repair` on each attempt records which path produced it.

Violated timing is recorded even on a failing run -- `final_wns_ns` is the
measurement you most want when the loop does not converge.

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
