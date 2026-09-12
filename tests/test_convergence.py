"""Tests for the repair loop's convergence controls.

These exist because the first valid benchmark run showed the loop
oscillating on multi-output FSMs: mismatch counts going down, then up, then
returning to an exact earlier value, with attempts 4 and 5 byte-identical.
Each control below targets one of those observed behaviours.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from rtlforge.loop import (  # noqa: E402
    Attempt,
    Problem,
    _history_block,
    _progress,
    run_problem,
)
from rtlforge.providers import Usage  # noqa: E402


class FixedClient:
    """Always returns the same RTL, so the loop must detect a stall."""

    provider = "stub"
    model = "stub"
    last_finish_reason = "stop"

    def __init__(self, rtl: str):
        self._rtl = rtl
        self.usage = Usage()
        self.calls = 0

    def chat(self, messages, max_tokens=None):
        self.calls += 1
        self.last_messages = messages
        return "```verilog\n" + self._rtl + "\n```"


def test_identical_regeneration_stops_the_loop(tmp_path):
    problem = Problem(ROOT / "problems" / "fifo")
    rtl = (ROOT / "examples" / "fifo_overflow_bug.v").read_text()
    client = FixedClient(rtl)

    result = run_problem(problem, client, tmp_path,
                         feedback_level="full", max_iterations=5)

    assert not result.passed
    assert result.stop_reason == "stalled"
    # Stops at 2 rather than burning the full budget on identical output.
    assert result.iterations == 2
    assert client.calls == 2


def test_attempts_record_a_content_hash(tmp_path):
    problem = Problem(ROOT / "problems" / "fifo")
    rtl = (ROOT / "examples" / "fifo_overflow_bug.v").read_text()
    result = run_problem(problem, FixedClient(rtl), tmp_path,
                         feedback_level="full", max_iterations=3)
    hashes = [a.rtl_hash for a in result.attempts]
    assert all(hashes), "every attempt needs a hash for oscillation analysis"
    assert hashes[0] == hashes[1]


def test_progress_prefers_later_stage_then_fewer_mismatches():
    sim_good = Attempt(1, "", failed_stage="simulate", magnitude=0.10)
    sim_worse = Attempt(2, "", failed_stage="simulate", magnitude=0.13)
    lint_fail = Attempt(3, "", failed_stage="lint")
    passing = Attempt(4, "", failed_stage=None)

    assert _progress(sim_good) > _progress(sim_worse)
    assert _progress(sim_good) > _progress(lint_fail)
    assert _progress(passing) > _progress(sim_good)


def test_history_block_lists_prior_attempts():
    attempts = [
        Attempt(1, "", failed_stage="simulate", magnitude=0.10),
        Attempt(2, "", failed_stage="simulate", magnitude=0.13),
    ]
    text = _history_block(attempts)
    assert "attempt 1" in text and "attempt 2" in text
    assert "10.0%" in text and "13.0%" in text
    assert "Do not repeat" in text


def test_history_block_empty_until_there_is_history():
    assert _history_block([]) == ""
    assert _history_block([Attempt(1, "", failed_stage="lint")]) == ""


def test_history_reaches_the_repair_prompt(tmp_path):
    """The model must actually be told what it already tried."""
    problem = Problem(ROOT / "problems" / "fifo")
    rtl = (ROOT / "examples" / "fifo_overflow_bug.v").read_text()

    class Varying(FixedClient):
        def chat(self, messages, max_tokens=None):
            self.calls += 1
            self.last_messages = messages
            # change a comment each time so no stall triggers
            return "```verilog\n" + self._rtl + f"\n// v{self.calls}\n```"

    client = Varying(rtl)
    run_problem(problem, client, tmp_path,
                feedback_level="full", max_iterations=4)
    final_prompt = client.last_messages[-1]["content"]
    assert "Attempts so far:" in final_prompt


def test_waveform_window_renders_ref_and_dut(tmp_path):
    """The trace must show what the design did, not just that it failed."""
    from rtlforge.waveform import first_mismatch_time, window_at

    vcd = tmp_path / "wave.vcd"
    vcd.write_text(
        "$timescale 1ps $end\n"
        "$var wire 1 ! clk $end\n"
        "$var wire 1 # q_ref $end\n"
        "$var wire 1 $ q_dut $end\n"
        "$enddefinitions $end\n"
        "#0\n0!\n0#\n0$\n"
        "#5\n1!\n1#\n0$\n"
        "#10\n0!\n1#\n0$\n"
    )
    assert first_mismatch_time("First mismatch occurred at time 5") == 5
    text = window_at(vcd, 5)
    assert "q_ref" in text and "q_dut" in text
    assert "first mismatch" in text
    # ref/dut pairs must sit next to each other to be readable
    assert text.index("q_ref") < text.index("q_dut")


def test_waveform_missing_file_is_not_fatal(tmp_path):
    from rtlforge.waveform import window_at
    assert window_at(tmp_path / "nope.vcd", 10) == ""
