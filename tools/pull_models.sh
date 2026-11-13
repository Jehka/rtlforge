#!/bin/bash
# Download Ollama models on mgmt01 (the only node with internet) into the
# shared home, where GPU jobs find them. Starts a temporary private server
# on a free port, pulls, stops it.
#
#     bash tools/pull_models.sh devstral-small-2 gemma4:31b glm-4.7-flash laguna-xs-2.1
OLLAMA="$HOME/.local/bin/ollama"
[ "$#" -gt 0 ] || { echo "usage: $0 model [model ...]"; exit 1; }
echo "ollama $("$OLLAMA" --version 2>/dev/null | tail -1)"
df -h "$HOME" | tail -1
du -sh "$HOME/.ollama/models" 2>/dev/null
port=$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')
export OLLAMA_HOST="127.0.0.1:$port"
"$OLLAMA" serve > "$HOME/ollama-pull-$port.log" 2>&1 &
pid=$!
trap 'kill $pid 2>/dev/null' EXIT
for i in $(seq 30); do curl -sf "http://$OLLAMA_HOST/api/tags" >/dev/null && break; sleep 1; done
ok=() bad=()
for m in "$@"; do
  echo "=== pulling $m"
  if "$OLLAMA" pull "$m"; then ok+=("$m"); else bad+=("$m"); fi
done
"$OLLAMA" list
echo "pulled: ${ok[*]:-none}"
[ ${#bad[@]} -eq 0 ] || echo "FAILED: ${bad[*]}  (wrong tag, or this Ollama is too old for it -- see message above)"
