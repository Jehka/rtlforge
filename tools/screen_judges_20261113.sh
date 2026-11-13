#!/bin/bash
# Screen new model families as spec-question judges on the standard 263
# questions (qwen2.5's mistakes; same set as every earlier M1a run).
# Jobs run one after another. Run on mgmt01 from ~/rtlforge after
# tools/pull_models.sh; models that are not pulled are skipped.
#
#     bash tools/screen_judges_20261113.sh                 # the four below
#     bash tools/screen_judges_20261113.sh gemma4:31b      # or name them
set -e
cd "$HOME/rtlforge"
MODELS=("$@"); [ ${#MODELS[@]} -gt 0 ] || MODELS=(devstral-small-2 gemma4:31b glm-4.7-flash laguna-xs-2.1)
have=$(ls "$HOME/.ollama/models/manifests/registry.ollama.ai/library" 2>/dev/null)
prev=""
for m in "${MODELS[@]}"; do
  name="${m%%:*}"; tag="${m#*:}"; [ "$tag" = "$m" ] && tag=latest
  if [ ! -f "$HOME/.ollama/models/manifests/registry.ollama.ai/library/$name/$tag" ]; then
    echo "skip $m: not pulled"; continue
  fi
  dep=${prev:+-W depend=afterany:$prev}
  prev=$(qsub $dep -v "MODEL=$m,MAXTOK=16384,CTX=24576,NEED_MIB=40000,TAG=screen" tools/spec_query.pbs)
  echo "$m -> $prev"
done
qstat -u "$USER"
