#!/bin/bash
# CVDP replay, third run (2026-11-14). Two fixes from the audit of the
# proof-based run:
#  - reset and clock detection by usage and token, not VerilogEval names:
#    CVDP's i_rst_n / presetn / axi_aresetn / rst_in / pclk / clk_in /
#    axi_aclk were missed, so no reset was applied and power-up states were
#    compared -- meaningless questions;
#  - compile gate with the harness's Icarus 13: designs Yosys accepts but
#    Icarus rejects were merged with passing designs (6 mixed clusters).
# Judges reuse their answers to identical questions from the proof-based run.
#
#     bash tools/replay_cvdp_v3_20261114.sh
set -e
cd "$HOME/rtlforge"
grep -q "def is_clock_name" rtlforge/formal.py && grep -q "compiles_in_simulator" tools/replay_pipeline.py \
  || { echo "old code: git pull first"; exit 1; }
QF=replay/replay_q_cvdp_v3.jsonl
Q=$(qsub -v "CVDP_DIRS=cvdp_gen/qwen38_cid003:cvdp_gen/gptoss_cid003,OUT=$QF" tools/replay_questions.pbs)
common="QFILE=$QF,REUSE_Q=replay/replay_q_cvdp_proved.jsonl,REUSE_TAG=cvdpproved,TAG=cvdpv3,CTX=32768"
J1=$(qsub -W depend=afterok:$Q -v "$common,MODEL=qwen3.8:27b,REASONING=low,NEED_MIB=30000" tools/replay_judge.pbs)
J2=$(qsub -W depend=afterok:$Q,afterany:$J1 -v "$common,MODEL=gpt-oss:120b,REASONING=medium,NEED_MIB=70000" tools/replay_judge.pbs)
J3=$(qsub -W depend=afterok:$Q,afterany:$J2 -v "$common,MODEL=gemma4:31b,NEED_MIB=40000" tools/replay_judge.pbs)
printf '%-3s %s\n' Q "$Q" J1 "$J1" J2 "$J2" J3 "$J3"
qstat -u "$USER"
