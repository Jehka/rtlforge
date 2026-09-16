# RTLForge — Findings

First complete experiment. 16 problems × 2 feedback levels × 3 trials = 96 runs,
`openai/gpt-oss-120b` via Groq, reasoning effort low, September 2026.

## Headline

| Level | Runs passed | Problems solved | Mean iterations |
|---|---|---|---|
| `none` (single-shot) | 15/48 (31%) | 6/16 | 1.00 |
| `full` (lint → compile → sim → synth feedback) | 31/47 (66%) | 13/16 | 2.94 |

Eight problems failed single-shot and were recovered by the repair loop. Every
one of them took `iterations ≥ 2`, so each is an actual repair rather than a
lucky resample.

## The caveat that has to come first

**The problem set was selected for discriminating.** It was built from problems
already observed to fail single-shot at least sometimes, plus four known-hard
FSMs and two controls. So 31% → 66% is not a VerilogEval pass rate and must
never be reported as one.

The defensible claim is narrower: *on problems where single-shot generation
fails, deterministic EDA feedback recovers a substantial fraction.* An
unbiased pass rate needs the full 152-problem set.

## Where feedback stops helping

Three problems failed all three trials at both levels:

- `Prob142_lemmings2`
- `Prob151_review2015_fsm`
- `Prob156_review2015_fancytimer`

The obvious reading is "multi-output FSMs are hard", but that is too coarse:
`Prob140_fsm_hdlc` is also a four-output FSM and the loop recovered it 2/3.
`Prob127_lemmings1` is a multi-output FSM and passes 3/3.

The sharper distinction is **FSMs coupled to multi-cycle counters**. All three
failures require state that persists across many cycles and interacts with a
count: `fancytimer` must count exactly 2000 then 15000 cycles;
`review2015_fsm` must sequence shift-enable, counting, and done across
thousands of samples; `lemmings2` must track fall duration. `lemmings1`, which
the loop solves reliably, is the same game without the counter.

That is a capability boundary with a mechanism, not just a difficulty label.

## Two distinct failure modes, not one

Aggregate "failed" hides a real distinction visible only per-attempt.

**Behavioural stall.** `Prob156` trial 1, attempts 3–5: mismatch magnitude
`0.819975`, `0.819975`, `0.819975` — identical observable behaviour, three
times, with three *different* `rtl_hash` values. The model rewrote comments and
signal names while producing functionally identical RTL. `Prob142` trial 3
does the same at 223/441 for attempts 3, 4 and 5.

This defeated the original hash-based stall detection, which is syntactic. The
loop now also fingerprints the *observed failure* (failing stage plus
diagnostic messages) and stops after three attempts that share one, reported as
`stop_reason: behaviourally stalled`. This is a cheap and, as far as we have
found, unreported control.

**Slow convergence.** `Prob151` trial 2 went 722 → 722 → 672 → 592 → 336
mismatches. Monotonic improvement that simply ran out of budget. That is a
different phenomenon from a stall and a larger `--max-iterations` might resolve
it. Conflating the two would be an error.

Failure modes across the run: `iteration budget` 13, `single-shot` 33,
`stalled` 3.

## Noise, and why trials matter

`Prob142_lemmings2` shows as "regressed under full" — it passed 1/3 at `none`
and 0/3 at `full`. That single `none` pass came at `iterations=1`, meaning no
feedback was ever delivered. It is sampling variance, not a regression caused
by the loop.

An earlier single-trial run reported a +7 problem improvement; separating
first-attempt passes from genuine repairs cut it to +5. Any result from this
harness at `--trials 1` should be treated as provisional.

## Benchmark curation

Of 156 imported VerilogEval spec-to-rtl problems, 152 are usable:

- 1 fails to compile under Icarus 12 (`Prob099_m2014_q6c`).
- 3 have references that depend on an `initial` block for power-on state that
  the prompt never specifies (`Prob031_dff`, `Prob034_dff8`,
  `Prob104_mt2015_muxdff`). No spec-compliant design can pass them: the
  reference starts at 0, a correct design starts at `x`, and the first samples
  mismatch.

The second class is worth emphasising because a reference always passes its
own testbench, so the standard self-check cannot detect it. `Prob034_dff8` — an
8-bit D flip-flop — failed 6/6 in an early run purely for this reason, while
the strictly harder `Prob073_dff16e` passed 6/6. Both classes are excluded from
`usable.json`.

## Area is not optimised

`Prob030_popcount255` is recovered by the loop but synthesises to 1351 cells.
The loop optimises for passing verification, not for area. Any PPA claim would
need a separate objective.

## Timing repair: what the model actually attempts

A second experiment ran the loop through static timing analysis on a 32-bit
registered adder, constrained to a 1.2ns clock against Nangate45. Reference
points on the same constraint:

| Implementation | WNS @ 1.2ns | Area |
|---|---|---|
| Hand-written ripple carry | -0.021ns | 371.34 um^2 |
| Hand-written carry-select | +0.065ns | 404.32 um^2 |
| Model's best, after repair | -0.043ns | 370.80 um^2 |

The loop plateaued: attempts 1, 3 and 5 landed on exactly -0.043ns, with
attempts 2 and 4 failing at lint and compile in between and being rolled
back. More iterations do not move it.

### The transformation was not the problem -- the style was

Measuring the model's carry-lookahead after correcting its bug, against three
references at the same 1.2ns constraint:

| Implementation | Style | WNS @ 1.2ns |
|---|---|---|
| Plain `a + b + cin` (ripple) | arithmetic | -0.021ns |
| Model's carry-lookahead, bug fixed | explicit gate equations | -0.136ns |
| Same 4-bit-group carry algorithm | arithmetic | -0.021ns |
| Carry-select | arithmetic | +0.065ns |

Two results, both against expectation.

**Writing the carry logic as explicit AND/OR equations costs 115ps** versus
expressing the identical algorithm arithmetically. Yosys's ABC restructures
arithmetic freely but must take gate-level equations more literally, so a
textbook-optimal structure written the textbook way blocks the optimisation
that would have made it fast. The model's *choice* of carry-lookahead was
sound; writing it at gate level is what lost.

**Expressing the carry structure at RTL buys nothing at all when written
arithmetically.** The 4-bit-group version and plain `a + b + cin` produce
identical slack, to the picosecond -- ABC flattens both to the same
implementation. Only carry-select improves on them, and it does so because it
is a different dataflow (speculative computation plus a select), not a
different way of writing the same carry chain.

So the actionable finding for an agentic RTL tool is about *how* to write, not
*what* to write: prompt for intent expressed arithmetically and leave
structure to synthesis, except where the change is genuinely a different
dataflow. An agent that reasons in textbook gate-level structures -- which is
what an LLM reaches for when asked to speed up an adder -- will reliably make
things worse.

**The discarded attempts are the interesting part.** One of them is a
complete 4-bit-group carry-lookahead adder -- propagate/generate per bit,
group propagate/generate, group carry chain, per-bit carries. That is the
correct transformation for the problem. It fails on one line:

```verilog
wire [31:0] sum_comb = p ^ c;        // c[j] is the carry OUT of bit j
wire [31:0] sum_comb = p ^ {c[30:0], cin};   // the sum needs the carry IN
```

With that single character-level correction it passes every test. Both
versions are preserved as `examples/adder32_cla_offbyone_bug.v` and
`examples/adder32_cla_fixed.v`, and both are in the oracle regression suite.

So the failure is not "the model does not know how to speed up an adder". It
selects the right structure and mis-implements one detail, then -- because the
broken version fails simulation rather than timing -- gets rolled back to its
slower but correct design and never revisits the idea. The loop's own
correctness guarantee is what discards the better approach.

That suggested a concrete fix, since implemented: when a timing repair
regresses to a *functional* failure, the loop no longer reverts on the first
attempt. The new structure gets one chance to be corrected, with a prompt
that says to keep it and repair only what is wrong. Rollback had been
treating "wrong" and "slow" as points on one scale, and here they are not --
a correct-but-slow design outranks a nearly-correct fast one, so the better
idea is discarded precisely when it is closest to working.

The rescue is gated to once per run: a structure that does not come good
after one correction reverts normally rather than consuming the whole
iteration budget. Whether this recovers the carry-lookahead case in practice
is the next thing to measure.

## A synthesis caveat worth stating

The critical path of the hand-written ripple adder maps to
`OR2 -> AOI21 -> OAI21 -> AOI21 -> OAI21 -> AOI21`. Yosys's ABC has already
flattened the explicit ripple structure before timing runs. Hand-written
carry-select beats it by only 86ps on a ~1.2ns path.

Much of what RTL-level restructuring would buy is therefore already captured
by synthesis, and the room an agent has to help at this level is narrower than
textbook complexity arguments suggest. Any claim about agentic timing
optimisation needs this baseline stated, or the gains attributed to the agent
belong to ABC.

## What would strengthen this

1. Run the full 152-problem set for an unbiased pass rate.
2. Intermediate feedback levels (`lint`, `compile`, `sim`) to attribute the
   effect to a specific signal rather than the whole pipeline.
3. A weaker model. The open question from AutoChip is whether feedback helps
   models that are weak enough to need it; a 120B is near the top of the range.
4. Larger iteration budget on the slow-converging cases specifically, now that
   behavioural stalls exit early and no longer consume it.

## Reproducing

```bash
python -m rtlforge.cli selftest --problems problems/verilogeval --write-manifest
python -m rtlforge.cli benchmark --problems problems/verilogeval \
    --manifest problems/discriminating.json --levels none full --trials 3 \
    --model openai/gpt-oss-120b --reasoning-effort low --max-tokens 3072
```

Free-tier daily token caps mean this spans sessions; `--resume` continues from
`raw.json`.
