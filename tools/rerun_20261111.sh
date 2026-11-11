#!/bin/bash
# Rerun of the 2026-11-10 overnight spec-question jobs. Those picked
# smoke-test reports (git checkout gave all reports one mtime, and selection
# sorted by mtime) and the 16k run hit a fixed 180 s read timeout. Fixed in
# spec_query.pbs (largest report per arm) and providers.py (timeout scales
# with max tokens). Run on mgmt01 from ~/rtlforge:
#
#     bash tools/rerun_20261111.sh
#
# Jobs run one after another so they do not pick the same GPU.
#  A1-A3  qwen3.8's OWN failed attempts (~101 questions, both qwen3.8 runs)
#         judged by qwen3.8 itself, gpt-oss, qwen2.5
#  B      qwen3.8 on the original 263 at 16k tokens
#  C      gpt-oss on the original 263 at reasoning effort medium
set -e
cd "$HOME/rtlforge"
grep -q "MOST runs" tools/spec_query.pbs && grep -q "def read_timeout" rtlforge/providers.py \
  || { echo "old code: git pull first"; exit 1; }

own="SOURCE_MODEL=qwen3.8:27b,ATTEMPTS=1,SKIP_NORESET=1"
A1=$(qsub -v "MODEL=qwen3.8:27b,REASONING=low,MAXTOK=16384,CTX=24576,NEED_MIB=30000,$own,TAG=own-self" tools/spec_query.pbs)
A2=$(qsub -W depend=afterany:$A1 -v "MODEL=gpt-oss:120b,REASONING=low,MAXTOK=16384,CTX=24576,NEED_MIB=72000,$own,TAG=own-cross" tools/spec_query.pbs)
A3=$(qsub -W depend=afterany:$A2 -v "MODEL=qwen2.5-coder:32b,MAXTOK=400,NEED_MIB=24000,$own,TAG=own-weak" tools/spec_query.pbs)
B=$(qsub -W depend=afterany:$A3 -v "MODEL=qwen3.8:27b,REASONING=low,MAXTOK=16384,CTX=24576,NEED_MIB=30000,TAG=tok16k" tools/spec_query.pbs)
C=$(qsub -W depend=afterany:$B -v "MODEL=gpt-oss:120b,REASONING=medium,MAXTOK=16384,CTX=24576,NEED_MIB=72000,TAG=medium" tools/spec_query.pbs)
echo "A1 self  $A1"; echo "A2 cross $A2"; echo "A3 weak  $A3"; echo "B  tok16 $B"; echo "C  gpt-m $C"
qstat -u "$USER"
