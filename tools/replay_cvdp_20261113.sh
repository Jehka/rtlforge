#!/bin/bash
# CVDP replay, 2026-11-13: the no-testbench pipeline on NoTB's 78 cid003
# specs, with 6 candidates per spec (qwen3.8 x 3, gpt-oss x 3). Run on mgmt01:
#
#     bash tools/replay_cvdp_20261113.sh
#
#  Q   cluster by formal equivalence, write spec questions      CPU
#  J1  qwen3.8 answers them                                     GPU
#  J2  gpt-oss (reasoning medium) answers them                  GPU, after J1
set -e
cd "$HOME/rtlforge"
grep -q "questions-cvdp" tools/replay_pipeline.py || { echo "old code: git pull first"; exit 1; }
for d in cvdp_gen/qwen38_cid003 cvdp_gen/gptoss_cid003; do
  ls $d/cand_s*_result.json >/dev/null 2>&1 || { echo "no scored candidates in $d"; exit 1; }
done
mkdir -p replay
QF=replay/replay_q_cvdp.jsonl
Q=$(qsub -v "CVDP_DIRS=cvdp_gen/qwen38_cid003:cvdp_gen/gptoss_cid003,OUT=$QF" tools/replay_questions.pbs)
J1=$(qsub -W depend=afterok:$Q -v "QFILE=$QF,MODEL=qwen3.8:27b,REASONING=low,NEED_MIB=30000,CTX=32768,TAG=cvdp" tools/replay_judge.pbs)
J2=$(qsub -W depend=afterok:$Q,afterany:$J1 -v "QFILE=$QF,MODEL=gpt-oss:120b,REASONING=medium,NEED_MIB=70000,CTX=24576,TAG=cvdp" tools/replay_judge.pbs)
printf '%-3s %s\n' Q "$Q" J1 "$J1" J2 "$J2"
qstat -u "$USER"
