# RTLForge — Backend Architecture

Design notes for extending the RTL loop through synthesis, timing, and physical
design. Written to be shared with collaborators, so it states the reasoning and
not just the plan.

## Goal

One command takes a natural-language spec to a GDS, with every stage feeding
failures back to the agent:

```
spec.md → RTL → lint → sim → map → STA → floorplan → place → CTS → route → GDS
```

The deliverable is a **working, reproducible pipeline**, not a benchmark
result. Depth of pipeline beats breadth of benchmark: one design taken all the
way down, runnable by a stranger with `docker compose up`, demonstrates more
than a large study of one stage.

## Prior art — read before building

This space is active. Not reading it means reimplementing it by accident.

| System | What it does |
|---|---|
| ASIC-Agent (ICLAD 2025) | Autonomous multi-agent ASIC design, end to end |
| ORFS-Agent | Tool-using agent driving OpenROAD-flow-scripts |
| AgenticPD | Stage-aware PD QoR optimisation as tree search with a judge agent |
| OpenROAD Agent / OpenROAD-Assistant | Self-correcting script generation |
| ChatEDA | LLM-driven EDA flow control |

The contribution to aim for is not "first agentic PD flow". It is a clean,
open, reproducible one that a reader can actually run.

## Two things change at the backend boundary

### 1. The metric becomes continuous

Simulation returns pass/fail. Timing returns **slack**, a number. That makes
"better" meaningful for the first time and turns the loop from repair into
optimisation. It also introduces a failure mode that does not exist at the
frontend: the agent can improve the metric by moving the target.

**Rule: the SDC is immutable during a run.** A design that meets timing because
the clock was slowed has not been improved. `TimingResult` records the clock
period it was measured against, and `fmax_mhz` returns `None` when the
constraint is unknown, so a number can never be quoted without the constraint
that produced it. Enforced in `backend.py`; see `SDC_IS_IMMUTABLE`.

### 2. The action space forks per stage

The frontend loop has one action: rewrite the module. That assumption breaks at
STA. Each stage needs its own action space, and the agent must not be allowed
to reach outside it.

| Stage | Failure | Permitted action |
|---|---|---|
| lint / compile / sim | wrong behaviour | edit RTL |
| map | unsynthesizable, or absurd cell count | edit RTL |
| STA | negative slack | edit RTL (pipeline, restructure). **Not** the SDC |
| floorplan / place | congestion, utilisation | floorplan params: utilisation, aspect ratio |
| CTS | skew, insertion delay | clock spec, buffer settings |
| route | DRC violations | placement density, layer constraints |

This is why AgenticPD is stage-aware rather than a single loop. Mixing the
action spaces produces a system that appears to converge and is actually
loosening its own constraints.

## Cost: the backend inverts the economics

PDAgent-Bench reports ~5.84M input tokens for a full flow across ten designs —
roughly 580K input tokens per design — and notes that cost is overwhelmingly
dominated by *input* context: each step ingests large synthesis logs, timing
reports and netlist summaries while emitting only short commands.

The frontend loop is output-dominated (the model writes a module). The backend
is input-dominated (the model reads reports and emits a parameter). At 200K
tokens/day free tier, one design is three days.

**Consequence: report summarisation is the core engineering problem, not a
nicety.** A raw timing report is tens of thousands of tokens. The useful
content is one path:

```
setup violation: WNS -0.34ns against a 2.0ns clock.
Critical path r2 -> r3: r2/Q (DFF_X1) +0.23ns -> u1/Z (BUF_X1) +0.08ns
```

That is what `parse_sta` produces. Every backend stage needs the same
treatment before it is wired into a loop.

## Status

**Built and tested:**

- `map` stage — Yosys `dfflibmap` + `abc -liberty` to a real standard-cell
  library, producing `mapped.v` plus area in um^2, cell mix, and flop count.
  Verified against Nangate45: the 4-bit counter maps to 31.122 um^2, 11 cells,
  4 DFFR_X1.
- `sta` stage — OpenSTA runner and `report_checks` parser, extracting WNS, TNS,
  and the critical path with per-cell delay contributions. Parsers tested
  against real OpenSTA output; the runner skips cleanly when OpenSTA is absent.
- `write_sdc` — minimal constraints including input/output delays. Without
  them, port paths get a full clock period and the reported timing is fiction.

**Not built:** OpenROAD floorplan/place/CTS/route, DRC parsing, the stage-aware
action-space router, GDS output.

## Next steps, in order

1. **Get OpenSTA and a PDK into the EDA container.** The runners exist and skip
   gracefully; they need the tools. Nangate45 is the easiest start (small,
   ships with OpenSTA examples); Sky130 is the realistic target.
2. **Close the STA loop with RTL-only actions.** Set an aggressive clock, let
   the agent pipeline its way to positive slack. This is the smallest complete
   demonstration that the loop optimises rather than merely repairs.
3. **Benchmark problem**: VerilogEval designs are too small for timing to be
   interesting — a 3-cell FSM has no critical path worth reporting. Use blocks
   from the RISC-V work, or PicoRV32. **This is a benchmark-construction
   problem and deserves thought before code.**
4. **ORFS through CTS.** Runtime moves from seconds to minutes, which is when
   parallel execution stops being optional.
5. **Full P&R with DRC feedback**, action space expanding to floorplan
   parameters.

## Division of work

The natural seam is the netlist plus the SDC:

- **Frontend** — RTL generation, verification, the repair loop, waveform
  feedback, benchmark curation. Exists and works.
- **Backend** — ORFS integration, timing and PD parsers, report summarisation,
  the stage-aware action router.

They share one interface (`mapped.v` + `constraints.sdc`) and can be developed
against fixtures independently.

## Packaging, since the artifact is the point

- **Docker is mandatory, not optional.** If a reader must install Yosys,
  OpenROAD, OpenSTA and a PDK before seeing anything, they close the tab. ORFS
  publishes prebuilt images.
- **Ship a picture.** PD produces layout renders, timing histograms, clock
  trees. The frontend never had a visual; use this one.
- **One command to reproduce.** `docker compose run flow --spec spec.md`.
