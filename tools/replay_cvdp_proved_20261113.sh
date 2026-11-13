#!/bin/bash
# CVDP replay, second run (2026-11-13 night): merge candidates only on an
# induction proof of equivalence. The first run merged on "no difference in
# 20 cycles", which put deep-state designs the testbench tells apart (a
# 24-hour clock differing at the hours rollover) into one cluster -- 10
# clusters had mixed testbench verdicts. Judges reuse their earlier answers
# for identical questions; only new questions are asked.
#
#     bash tools/replay_cvdp_proved_20261113.sh
#
#  Q   cluster with proofs, write questions                 CPU
#  J1  qwen3.8   (reuses its cvdp answers)                  GPU
#  J2  gpt-oss   (reuses its cvdp answers)                  GPU, after J1
#  J3  gemma4    (reuses its cvdp answers if that run has finished)  GPU, after J2
set -e
cd "$HOME/rtlforge"
grep -q "def prove_equivalent" rtlforge/formal.py && grep -q -- "--reuse" tools/replay_pipeline.py \
  || { echo "old code: git pull first"; exit 1; }
QF=replay/replay_q_cvdp_proved.jsonl
Q=$(qsub -v "CVDP_DIRS=cvdp_gen/qwen38_cid003:cvdp_gen/gptoss_cid003,OUT=$QF" tools/replay_questions.pbs)
common="QFILE=$QF,REUSE_Q=replay/replay_q_cvdp.jsonl,REUSE_TAG=cvdp,TAG=cvdpproved,CTX=32768"
J1=$(qsub -W depend=afterok:$Q -v "$common,MODEL=qwen3.8:27b,REASONING=low,NEED_MIB=30000" tools/replay_judge.pbs)
J2=$(qsub -W depend=afterok:$Q,afterany:$J1 -v "$common,MODEL=gpt-oss:120b,REASONING=medium,NEED_MIB=70000" tools/replay_judge.pbs)
J3=$(qsub -W depend=afterok:$Q,afterany:$J2 -v "$common,MODEL=gemma4:31b,NEED_MIB=40000" tools/replay_judge.pbs)
printf '%-3s %s\n' Q "$Q" J1 "$J1" J2 "$J2" J3 "$J3"
qstat -u "$USER"
