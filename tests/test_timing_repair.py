"""Timing-repair tests.

A timing failure is a different task from a functional one: the design is
already correct and must stay that way. These tests assert the loop knows the
difference and that the clock constraint stays out of the agent's reach.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import rtlforge.backend as B  # noqa: E402
import rtlforge.loop as L  # noqa: E402
import rtlforge.runners as R  # noqa: E402
from rtlforge.loop import Problem, run_problem  # noqa: E402
from rtlforge.parsers import Diagnostic  # noqa: E402
from rtlforge.providers import Usage  # noqa: E402

GOOD_RTL = (ROOT / "examples" / "counter_good.v").read_text()


class RecordingClient:
    provider = "stub"
    model = "stub"
    last_finish_reason = "stop"

    def __init__(self):
        self.usage = Usage()
        self.prompts = []
        self.n = 0

    def chat(self, messages, max_tokens=None):
        self.n += 1
        self.prompts.append(messages)
        return "```verilog\n" + GOOD_RTL + f"\n// rev{self.n}\n```"


def _stub_backend(monkeypatch, wns=-0.123):
    """map always passes; sta always violates."""
    def fake_map(design, workdir, top, liberty):
        sr = R.StageResult("map", True, [], "")

        class M:
            area_um2, cells, flops, seq_area_um2 = 31.12, 11, 4, 21.28
        sr.map_metrics = M()
        (workdir / "mapped.v").write_text("// netlist\n")
        return sr

    def fake_sta(workdir, top, liberty, port, period, timeout=60):
        sr = R.StageResult("sta", False, [Diagnostic(
            "error", None, "TIMING",
            f"setup violation: WNS {wns}ns against a {period}ns clock. "
            f"Critical path r1 -> r2: r1/Q (DFFR_X1) +0.108ns")], "")

        class T:
            pass
        T.wns, T.clock_period_ns = wns, period
        sr.timing = T()
        return sr

    monkeypatch.setattr(B, "techmap", fake_map)
    monkeypatch.setattr(B, "run_sta", fake_sta)
    monkeypatch.setattr(L, "backend", B)


def test_timing_failure_uses_the_timing_prompt(tmp_path, monkeypatch):
    """The functional prompt invites rewriting behaviour. Timing repair
    must not: the design is already correct."""
    _stub_backend(monkeypatch)
    client = RecordingClient()
    run_problem(Problem(ROOT / "problems" / "counter"), client, tmp_path,
                feedback_level="full", max_iterations=2, through="sta")

    system, user = client.prompts[-1][0], client.prompts[-1][1]
    assert "closing timing" in system["content"]
    assert "observable behaviour" in system["content"]
    assert "cannot be changed" in user["content"], (
        "the prompt must state the clock is fixed, or the model proposes "
        "relaxing it as the fix"
    )


def test_functional_failure_still_uses_the_functional_prompt(tmp_path):
    """Routing must not send every failure to the timing prompt."""
    client = RecordingClient()
    problem = Problem(ROOT / "problems" / "counter")
    # sync-reset bug: fails simulation, never reaches timing
    bad = (ROOT / "examples" / "counter_sync_reset_bug.v").read_text()

    class BadClient(RecordingClient):
        def chat(self, messages, max_tokens=None):
            self.n += 1
            self.prompts.append(messages)
            return "```verilog\n" + bad + f"\n// rev{self.n}\n```"

    client = BadClient()
    run_problem(problem, client, tmp_path, feedback_level="full",
                max_iterations=2, through="synth")
    system = client.prompts[-1][0]["content"]
    assert "closing timing" not in system
    assert "RTL design engineer" in system


def test_violated_timing_is_still_recorded(tmp_path, monkeypatch):
    """A failing WNS is the measurement you most want in the log."""
    _stub_backend(monkeypatch, wns=-0.5)
    result = run_problem(Problem(ROOT / "problems" / "counter"),
                         RecordingClient(), tmp_path,
                         feedback_level="full", max_iterations=2,
                         through="sta")
    assert not result.passed
    assert result.final_wns_ns == -0.5
    assert result.final_area_um2 == 31.12


def test_sdc_is_written_by_the_runner_not_the_agent(tmp_path):
    """The agent has no path to the constraint: run_sta rewrites the SDC on
    every call, so any edit a model might make is overwritten before it is
    read."""
    from rtlforge.backend import write_sdc

    sdc = tmp_path / "constraints.sdc"
    write_sdc(sdc, "clk", 2.0)
    first = sdc.read_text()

    sdc.write_text("create_clock -name clk -period 999 [get_ports clk]\n")
    write_sdc(sdc, "clk", 2.0)
    assert sdc.read_text() == first, "a tampered SDC must be regenerated"
    assert "999" not in sdc.read_text()


# --- ranking: the bug that made rollback discard the better design ---

def test_every_pipeline_stage_is_ranked():
    """A stage missing from STAGE_RANK defaults to 0 and ranks below lint.

    Real consequence: a design 21ps from closing timing was discarded in
    favour of one that did not parse.
    """
    from rtlforge.loop import FLOW_TIERS, STAGE_RANK
    for tier in FLOW_TIERS.values():
        for stage in tier:
            assert stage in STAGE_RANK, f"{stage!r} is unranked"


def test_timing_failure_outranks_earlier_failures():
    from rtlforge.loop import Attempt, _progress
    sta = Attempt(1, "", failed_stage="sta", magnitude=0.02)
    sim = Attempt(2, "", failed_stage="simulate", magnitude=0.5)
    lint = Attempt(3, "", failed_stage="lint")
    passing = Attempt(4, "", failed_stage=None)
    assert _progress(sta) > _progress(sim) > _progress(lint)
    assert _progress(passing) > _progress(sta)


def test_timing_violations_rank_against_each_other():
    """Timing is the one stage with a continuous metric. Without using it,
    every violation scores the same and rollback reverts on ties."""
    from rtlforge.loop import Attempt, _magnitude_from, _progress

    near = _magnitude_from(
        {"stage": "sta", "diagnostics": [],
         "timing": {"wns": -0.021, "clock_period_ns": 1.2}})
    far = _magnitude_from(
        {"stage": "sta", "diagnostics": [],
         "timing": {"wns": -0.400, "clock_period_ns": 1.2}})
    assert near is not None and far is not None
    assert near < far

    a = Attempt(1, "", failed_stage="sta", magnitude=near)
    b = Attempt(2, "", failed_stage="sta", magnitude=far)
    assert _progress(a) > _progress(b)


def test_timing_magnitude_is_clamped():
    """A hopeless timing result must not outrank a functional failure."""
    from rtlforge.loop import _magnitude_from
    hopeless = _magnitude_from(
        {"stage": "sta", "diagnostics": [],
         "timing": {"wns": -50.0, "clock_period_ns": 1.0}})
    assert hopeless == 1.0


def test_best_slack_is_reported_not_the_last(tmp_path, monkeypatch):
    """The loop keeps its best attempt; the report must match.

    Observed: a run whose best attempt reached -0.322ns reported -0.343ns
    because the final attempt happened to be worse.
    """
    slacks = iter([-0.343, -0.376, -0.322, -0.343])

    def fake_map(design, workdir, top, liberty):
        sr = R.StageResult("map", True, [], "")

        class M:
            area_um2, cells, flops, seq_area_um2 = 370.8, 271, 33, 21.28
        sr.map_metrics = M()
        (workdir / "mapped.v").write_text("// netlist\n")
        return sr

    def fake_sta(workdir, top, liberty, port, period, timeout=60,
                 io_delay_ns=0.1):
        wns = next(slacks, -0.343)
        sr = R.StageResult("sta", False, [Diagnostic(
            "error", None, "TIMING", f"setup violation: WNS {wns}ns")], "")

        class T:
            pass
        T.wns, T.clock_period_ns = wns, period
        sr.timing = T()
        return sr

    monkeypatch.setattr(B, "techmap", fake_map)
    monkeypatch.setattr(B, "run_sta", fake_sta)
    monkeypatch.setattr(L, "backend", B)

    result = run_problem(Problem(ROOT / "problems" / "counter"),
                         RecordingClient(), tmp_path,
                         feedback_level="full", max_iterations=4,
                         through="sta")
    assert result.final_wns_ns == -0.322, (
        f"reported {result.final_wns_ns}, should be the best of the run"
    )


def test_every_attempt_is_written_to_disk(tmp_path, monkeypatch):
    """Failed attempts are the useful artifact when a loop plateaus."""
    _stub_backend(monkeypatch)
    run_problem(Problem(ROOT / "problems" / "counter"), RecordingClient(),
                tmp_path, feedback_level="full", max_iterations=3,
                through="sta")
    saved = sorted(p.name for p in tmp_path.glob("attempt_*.v"))
    assert len(saved) >= 2, f"attempts not preserved: {saved}"
    assert saved[0] == "attempt_01.v"
