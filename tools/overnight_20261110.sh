#!/bin/bash
# Overnight batch, 2026-11-10. Run on mgmt01 from ~/rtlforge:
#
#     bash tools/overnight_20261110.sh
#
# The spec-question (M1a) jobs run one after another: jobs that start
# together can pick the same GPU (that caused this morning's timeouts).
# The qwen3.8 evidence benchmark runs alongside them, starting once the
# first M1a job is running.
#
#  A1-A3  self vs cross arbitration on qwen3.8's OWN failed attempts
#         (73 questions): qwen3.8 judging itself, gpt-oss, qwen2.5
#  B      qwen3.8 on the original 263 questions with a 16k token budget
#         (removes the 20 truncations)
#  C      gpt-oss on the original 263 with reasoning effort medium
#  D      qwen3.8 design benchmark with all repair evidence (ARMS=on),
#         paired with this morning's evidence-off run
set -e
cd "$HOME/rtlforge"
grep -q -- "--attempts" tools/spec_query_eval.py || { echo "old code: git pull first"; exit 1; }

own="SOURCE_MODEL=qwen3.8:27b,ATTEMPTS=1,SKIP_NORESET=1"
A1=$(qsub -v "MODEL=qwen3.8:27b,REASONING=low,MAXTOK=16384,CTX=24576,NEED_MIB=30000,$own,TAG=own-self" tools/spec_query.pbs)
A2=$(qsub -W depend=afterany:$A1 -v "MODEL=gpt-oss:120b,REASONING=low,MAXTOK=16384,CTX=24576,NEED_MIB=72000,$own,TAG=own-cross" tools/spec_query.pbs)
A3=$(qsub -W depend=afterany:$A2 -v "MODEL=qwen2.5-coder:32b,MAXTOK=400,NEED_MIB=24000,$own,TAG=own-weak" tools/spec_query.pbs)
B=$(qsub -W depend=afterany:$A3 -v "MODEL=qwen3.8:27b,REASONING=low,MAXTOK=16384,CTX=24576,NEED_MIB=30000,TAG=tok16k" tools/spec_query.pbs)
C=$(qsub -W depend=afterany:$B -v "MODEL=gpt-oss:120b,REASONING=medium,MAXTOK=16384,CTX=24576,NEED_MIB=72000,TAG=medium" tools/spec_query.pbs)
D=$(qsub -W depend=after:$A1 -v "MODEL=qwen3.8:27b,REASONING=low,ARMS=on,TRIALS=3,MAXTOK=8192,CTX=16384,NEED_MIB=30000" tools/model_benchmark.pbs)

echo "A1 self  $A1"; echo "A2 cross $A2"; echo "A3 weak  $A3"
echo "B  tok16 $B";  echo "C  gpt-m $C";  echo "D  evid  $D"
qstat -u "$USER"
