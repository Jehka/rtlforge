#!/bin/bash
# Extra overnight jobs, 2026-11-12, for the CVDP head-to-head with NoTB.
# Run on mgmt01 from ~/rtlforge, after (or instead of waiting for)
# tools/overnight_20261112.sh -- the two do not depend on each other:
#
#     bash tools/overnight_20261112_extra.sh
#
#  E1  gpt-oss answers NoTB's 78 cid003 specs x 3 samples (second model
#      family: cross-family agreement on CVDP needs it)            GPU
#  S1  harness scoring of E1                                       CPU
#  E2  qwen3.8 answers the other four categories x 1 sample
#      (cid002, cid004, cid007, cid016: full CVDP score table)     GPU
#  S2  harness scoring of E2                                       CPU
set -e
cd "$HOME/rtlforge"
grep -q START_DELAY tools/cvdp_gen.pbs || { echo "old code: git pull first"; exit 1; }
export MAMBA_ROOT_PREFIX="$HOME/micromamba"
eval "$("$HOME/bin/micromamba" shell hook -s bash)"
micromamba run -n cvdp sh -c 'command -v verilator && command -v yosys' >/dev/null \
  || micromamba install -y -q -n cvdp -c conda-forge verilator yosys
mkdir -p cvdp_gen

E1=$(qsub -v "MODEL=gpt-oss:120b,REASONING=low,SAMPLES=3,CATS=cid003,OUTDIR=cvdp_gen/gptoss_cid003,NEED_MIB=72000,CTX=24576,START_DELAY=240" tools/cvdp_gen.pbs)
S1=$(qsub -W depend=afterany:$E1 -v "OUTDIR=cvdp_gen/gptoss_cid003" tools/cvdp_score.pbs)
E2=$(qsub -v "MODEL=qwen3.8:27b,REASONING=low,SAMPLES=1,CATS=cid002:cid004:cid007:cid016,OUTDIR=cvdp_gen/qwen38_other,NEED_MIB=30000,START_DELAY=480" tools/cvdp_gen.pbs)
S2=$(qsub -W depend=afterany:$E2 -v "OUTDIR=cvdp_gen/qwen38_other" tools/cvdp_score.pbs)
printf '%-3s %s\n' E1 "$E1" S1 "$S1" E2 "$E2" S2 "$S2"
qstat -u "$USER"
