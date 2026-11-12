#!/bin/bash
# Overnight batch, 2026-11-12. Run on mgmt01 from ~/rtlforge:
#
#     bash tools/overnight_20261112.sh
#
# 1. (here, on mgmt01, needs internet) add Verilator + Yosys to the cvdp env;
#    ~40 CVDP datapoints run lint/synth checks that need them.
# 2. Replay of the no-testbench pipeline on logged designs:
#      Q1  questions, qwen3.8 candidates (6 per problem)            CPU
#      Q2  questions, qwen3.8 + gpt-oss candidates (9 per problem)  CPU
#      J1-J4  qwen3.8 and gpt-oss-medium judge both question sets   GPU, in sequence
# 3. CVDP, NoTB's cid003 subset (78 specs) x 3 samples by qwen3.8:
#      G  generation (upstream CVDP prompt)                         GPU, alongside J1-J4
#      S  harness scoring of all 3 samples                          CPU, after G
set -e
cd "$HOME/rtlforge"
for f in tools/replay_pipeline.py tools/cvdp_generate.py tools/replay_judge.pbs; do
  [ -f "$f" ] || { echo "missing $f: git pull first"; exit 1; }
done
QO=diag_20261110-061608_evidence-off.md; QN=diag_20261110-201520_evidence-on.md; GO=diag_20261111-181116_evidence-off.md
for f in $QO $QN $GO; do [ -f "$f" ] || { echo "missing report $f"; exit 1; }; done

export MAMBA_ROOT_PREFIX="$HOME/micromamba"
eval "$("$HOME/bin/micromamba" shell hook -s bash)"
if ! micromamba run -n cvdp sh -c 'command -v verilator && command -v yosys' >/dev/null; then
  echo "installing verilator + yosys into the cvdp env ..."
  micromamba install -y -q -n cvdp -c conda-forge verilator yosys
fi
micromamba run -n cvdp sh -c 'verilator --version; yosys -V'
mkdir -p replay cvdp_gen

Q1=$(qsub -v "REPORTS=$QO:$QN,OUT=replay/replay_q_qwen.jsonl" tools/replay_questions.pbs)
Q2=$(qsub -v "REPORTS=$QO:$QN:$GO,OUT=replay/replay_q_mixed.jsonl" tools/replay_questions.pbs)
qwen="MODEL=qwen3.8:27b,REASONING=low,NEED_MIB=30000"
gpt="MODEL=gpt-oss:120b,REASONING=medium,NEED_MIB=72000"
J1=$(qsub -W depend=afterok:$Q1 -v "QFILE=replay/replay_q_qwen.jsonl,$qwen,TAG=qwen" tools/replay_judge.pbs)
J2=$(qsub -W depend=afterok:$Q1,afterany:$J1 -v "QFILE=replay/replay_q_qwen.jsonl,$gpt,TAG=qwen" tools/replay_judge.pbs)
J3=$(qsub -W depend=afterok:$Q2,afterany:$J2 -v "QFILE=replay/replay_q_mixed.jsonl,$qwen,TAG=mixed" tools/replay_judge.pbs)
J4=$(qsub -W depend=afterok:$Q2,afterany:$J3 -v "QFILE=replay/replay_q_mixed.jsonl,$gpt,TAG=mixed" tools/replay_judge.pbs)
G=$(qsub -v "MODEL=qwen3.8:27b,REASONING=low,SAMPLES=3,CATS=cid003,OUTDIR=cvdp_gen/qwen38_cid003,NEED_MIB=30000" tools/cvdp_gen.pbs)
S=$(qsub -W depend=afterany:$G -v "OUTDIR=cvdp_gen/qwen38_cid003" tools/cvdp_score.pbs)

printf '%-4s %s\n' Q1 "$Q1" Q2 "$Q2" J1 "$J1" J2 "$J2" J3 "$J3" J4 "$J4" G "$G" S "$S"
qstat -u "$USER"
