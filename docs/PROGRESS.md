# RTLForge — progress report (as of 14 November 2026)

Jehkaran Singh · B.Tech ECE (microelectronics), UPES Dehradun

Compute: UPES HPC cluster (PBS Pro, NVIDIA H100 80 GB). Publications from this
work will credit *Centre of Excellence: Artificial Intelligence, School of
Computer Science, UPES*.

---

## 1. The question

LLMs can write Verilog, but every published loop that makes them better —
generate, simulate, repair — assumes a **golden testbench**: someone already
wrote a correct test for the design. In practice the testbench is written by
the same people, at the same time, from the same spec, and it is often wrong
or thin. We found this even in NVIDIA's CVDP benchmark (section 4.4).

**Can we establish trust in LLM-written RTL *without* a golden testbench?**

Our approach, in one line: generate several candidate designs, use **formal
equivalence checking** to find exactly where they disagree, turn each
disagreement into a concrete **spec question** ("in cycle 3, with these inputs,
must `empty` be 0 or 1?"), have **independent LLM judges from different model
families** answer it from the spec, and accept a design only when it wins
every question.

## 2. Closest prior work

| work | what it does | what it does not do |
|---|---|---|
| AutoChip, VerilogCoder, RTLFixer | repair loops driven by a golden testbench or compiler | work without a testbench |
| **NoTB** (arXiv 2608.21962, Aug 2026) | 4 model families × 5 samples, pairwise equivalence with Cadence JasperGold, accept a spec when the largest equivalence cluster spans ≥ τ families (CVDP cid003: 94.7% precision at 27% coverage, τ = 4) | resolve disagreements; deferred specs (73%) are left to "the existing verification flow"; reset handling and input assumptions are not described; commercial tool |
| **RTLForge (this work)** | turns each disagreement into a spec question answered by cross-family judges; open-source tools only (Yosys, Icarus, Verilator) | — |

## 3. What was built (all open source, all in this repository)

| component | file | purpose |
|---|---|---|
| repair loop with structured evidence | `rtlforge/loop.py`, `rtlforge/evidence.py`, `rtlforge/cone.py` | backward dependency cone, trace readings (X-state, first divergence), no-effect detection |
| formal distinguisher | `rtlforge/formal.py` `distinguish()` | Yosys miter + SAT: shortest counterexample between two designs, reset asserted in cycle 1 and excluded, reset/clock detected from usage |
| equivalence proof | `rtlforge/formal.py` `prove_equivalent()` | temporal induction (`sat -tempinduct`): unbounded proof, needed for deep-state designs |
| spec questions | `rtlforge/formal.py` `render_question()` | counterexample → plain-language A/B question |
| judge evaluation | `tools/spec_query_eval.py` | asks a model the questions, scores against a known answer |
| pipeline replay | `tools/replay_pipeline.py` | the full no-testbench pipeline on logged candidate designs; golden tests used only for scoring |
| CVDP without Docker | `tools/cvdp_run.py`, `tools/cvdp_generate.py` | NVIDIA CVDP harness and prompts reproduced for a cluster with no container runtime |
| cluster jobs | `tools/*.pbs`, `tools/gpu_up.sh` | PBS jobs; per-job Ollama server on a free port |
| tests | `tests/` | 185 unit tests |

## 4. Results

### 4.1 Repair evidence helps weak models, not strong ones
VerilogEval v2 (156 problems × 3 runs), golden-testbench repair loop.

| model | runs passed: no evidence → full evidence | repairs that succeed |
|---|---|---|
| qwen2.5-coder:32b | 280 → 304 of 468 | 23.9% → 33.6% |
| qwen3.8:27b | 439/467 → 441/468 (94.0% → 94.2%) | 86.3% → 86.4% |

The trace evidence (X-state readings) carries most of the gain for the weak
model. Contract check: 0 of 304 passing designs introduced physical-design
violations. *Honest reading: a useful engineering result, not the paper's
headline.*

### 4.2 Formal disagreement agrees with the golden testbench
Every final design vs. the reference, bounded formal check:
**recall 574/581 (98.8%)** on testbench failures, **agreement 1128/1134
(99.5%)** on testbench passes. Formal disagreement can stand in for the
testbench as a detector.

### 4.3 LLMs can answer spec questions — judging is easier than generating
263 questions built from qwen2.5's failed designs, each with a known answer
(the reference's behaviour).

| judge | correct among A/B picks |
|---|---|
| qwen2.5-coder:32b | 36% |
| gpt-oss:120b, reasoning low → medium | 73.7% → 86.3% |
| gemma4:31b | 86.3% (all questions) |
| qwen3.8:27b | 91.4–94.3% |

qwen3.8 writes a correct design on its first simulatable try 82% of the time,
yet judges a concrete counterexample correctly ~91–94% of the time.

Self-bias (a model judging its own mistakes worse) appeared for qwen3.8
(−14 points, p ≈ 0.04) but **did not replicate** with gpt-oss as generator
(p ≈ 0.77); pooled p ≈ 0.11. Reported as a non-replicated observation.

### 4.4 CVDP's own testbenches are weak
Mutants of the reference LFSR (5 runs each through CVDP's cocotb test):
dropping a feedback XOR passed 1/5, replacing the LFSR with a plain rotate
passed 1/5, **changing async to sync reset passed 5/5**. Unseeded random
stimulus; one assertion compares a signal handle to itself and cannot fail.
Our formal check flags all five mutants (the reset change with a 3-cycle
counterexample). This is the motivation for a no-testbench method in one
example.

### 4.5 The no-testbench pipeline on VerilogEval (main positive result)
155 problems; candidates = the first design of each run that compiles and
simulates (6 per problem from qwen3.8, 9 with gpt-oss added). Golden tests used
only to score.

| strategy | accepted | correct among accepted | overall correct* |
|---|---|---|---|
| first design (no pipeline) | — | — | 82.0% |
| majority vote over formal clusters | 100% | 85.8% | 85.8% |
| two judges agree | 86% | 91.7% | 88.4% |
| all 3 judges agree (mixed generators) | 79% | **92.7%** | **89.7%** |
| best selection can do (a correct cluster exists) | — | — | 90.3–91.6% |
| golden testbench + repair (reference) | — | — | 94.0% |

\*deferred problems fall back to the majority cluster.
Gain over the first design: **+6.3 points, 95% CI [+3.8, +9.1]** (qwen
candidates); +9.4 [+6.2, +12.8] with mixed generators. Most of the accuracy
gain comes from formal clustering; the judges add precision on what is
accepted and identify which problems to defer (majority is right on only 68%
of deferred problems).

### 4.6 CVDP (NVIDIA, harder) — work in progress
78 spec-to-RTL problems (the NoTB subset); 6 candidates (qwen3.8 ×3, gpt-oss
×3); pass@1 of the generators 44.4% and 34.6%.

Three rounds, each fixing a measurement problem found by auditing the previous:
1. Merging designs on bounded equivalence (20 cycles) was unsound: a 24-hour
   clock differs from a buggy one only at the hour rollover. **Fix:** merge
   only on an induction proof (as NoTB merges only on proven equivalence).
2. Reset and clock names (`i_rst_n`, `presetn`, `axi_aresetn`, `clk_in`,
   `pclk`) were not recognised, so power-up states were compared. **Fix:**
   detection by usage and name tokens. Designs Yosys accepts but Icarus
   rejects were merged with passing ones. **Fix:** compile gate with the
   harness's simulator.

Round 3:

| strategy | accepted | correct among accepted (95% CI) |
|---|---|---|
| first design | — | 39.5% overall |
| majority vote | 100% | 55.1% |
| cross-family consensus, no judge (NoTB's rule on our pool) | 27% | 81.0% [60, 92] |
| **all 3 judges agree** | **33%** | **84.6% [66, 94]** |
| best selection can do | — | 67.9% overall |
| NoTB published (different pool: 4 families × 5, JasperGold), τ ≥ 3 | 33% | 87% |

*Honest reading:* the judges are at least as precise as cross-family consensus
while accepting more problems, but the intervals overlap; not yet a
significant win. More problems are being generated (CVDP cid002, +94 specs)
and a third generator family (Gemma 4).

The audit also found a genuine open problem: some disagreements lie **outside
the spec's stated assumptions** (e.g. a clock divider whose spec says `sel`
stays stable after reset, while the counterexample changes it). The judges
answer sensibly, but no testbench exercises it. **Turning a spec's stated
assumptions into formal constraints** is the most original next step; NoTB
does not address it either.

## 5. Status of claims

| claim | status |
|---|---|
| formal disagreement can replace the testbench as a detector | supported (98.8% / 99.5%) |
| LLM judges answer formal counterexample questions reliably | supported on VerilogEval (86–94%); weaker on CVDP |
| no-testbench pipeline improves correctness and gives a trust signal | supported on VerilogEval (+6.3 to +9.4 points; ~92% precision) |
| better than NoTB-style consensus on CVDP | not yet significant |
| models judge their own mistakes worse | not replicated |
| repair evidence helps | weak models only |

## 6. Next steps

1. **CVDP at larger n** (running): cid002 + Gemma candidates → ~170 problems,
   confidence intervals roughly halved; head-to-head with NoTB's rule on the
   same candidate pool.
2. **Spec assumptions as formal constraints**: extract stated input
   assumptions (stability, one-hot, valid ranges) into SAT constraints so every
   question lies inside the spec's domain.
3. **Repair from judge answers**: when judges answer NEITHER or no cluster wins,
   regenerate with the answered questions as constraints — the only way past
   the selection ceiling (90% on VerilogEval).
4. **Paper**: draft once (1) is in. Target: a preprint, then an EDA / ML-for-EDA
   venue, depending on the CVDP outcome.

## 7. Where the details are

- `FINDINGS.md` — the September experiments (feedback levels, model size, timing repair).
- Raw reports in the repository root: `diag_*_evidence-*.md` (benchmarks), `m1a_*` (judge evaluations).
- Per-experiment write-ups (methods, tables, audits) are kept in the project notes and can be exported on request.
