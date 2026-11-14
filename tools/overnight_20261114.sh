#!/bin/bash
# Overnight batch, 2026-11-14: more CVDP problems and a third generator family.
# Run 3 of the CVDP replay (proofs + reset/clock fixes + Icarus gate) put all
# three judges agreeing at 84.6% precision (22/26, 95% CI 66-94%) vs 81.0%
# (17/21, CI 60-92%) for cross-family consensus -- too few problems to tell
# apart. cid002 (code completion, 94 specs) more than doubles n.
#
#     bash tools/overnight_20261114.sh
#
#  G1  gemma4 finishes cid003 sample 0 (resumes; 8k tokens)        GPU
#  G2  qwen3.8 cid002 x 3                                          GPU
#  G3  gpt-oss cid002 x 3                                          GPU
#  S1-S3 harness scoring after each                                CPU
set -e
cd "$HOME/rtlforge"
mkdir -p cvdp_gen
G1=$(qsub -v "MODEL=gemma4:31b,SAMPLES=1,CATS=cid003,OUTDIR=cvdp_gen/gemma4_cid003,MAXTOK=8192,NEED_MIB=40000" tools/cvdp_gen.pbs)
S1=$(qsub -W depend=afterany:$G1 -v "OUTDIR=cvdp_gen/gemma4_cid003" tools/cvdp_score.pbs)
G2=$(qsub -v "MODEL=qwen3.8:27b,REASONING=low,SAMPLES=3,CATS=cid002,OUTDIR=cvdp_gen/qwen38_cid002,NEED_MIB=30000,START_DELAY=300" tools/cvdp_gen.pbs)
S2=$(qsub -W depend=afterany:$G2 -v "OUTDIR=cvdp_gen/qwen38_cid002" tools/cvdp_score.pbs)
G3=$(qsub -v "MODEL=gpt-oss:120b,REASONING=low,SAMPLES=3,CATS=cid002,OUTDIR=cvdp_gen/gptoss_cid002,NEED_MIB=70000,CTX=24576,START_DELAY=600" tools/cvdp_gen.pbs)
S3=$(qsub -W depend=afterany:$G3 -v "OUTDIR=cvdp_gen/gptoss_cid002" tools/cvdp_score.pbs)
printf '%-3s %s\n' G1 "$G1" S1 "$S1" G2 "$G2" S2 "$S2" G3 "$G3" S3 "$S3"
qstat -u "$USER"
