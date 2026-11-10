# Bring up a GPU node for RTLForge. SOURCE this, do not execute it:
#
#     source tools/gpu_up.sh
#
# Run it on the node you got from:
#     qsub -I -l select=1:ncpus=8:ngpus=1 -q gpu
#
# It activates the env, pins the GPU with the most free memory, caps
# Ollama's context (it otherwise sizes it to all eight cards' VRAM), and
# starts Ollama on 127.0.0.1:11435 (11434 is held by another user).
# No `set -e`: a sourced script that exits would close your shell.

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    echo "gpu_up.sh must be sourced:  source tools/gpu_up.sh" >&2
    exit 1
fi

_gpu_up() {
    # Overridable per model: NEED_MIB (free GPU memory required) and
    # RTLFORGE_CTX (context window). Defaults suit qwen2.5-coder:32b.
    local need_mib="${NEED_MIB:-21000}" best free waited=0

    export MAMBA_ROOT_PREFIX="$HOME/micromamba"
    eval "$("$HOME/bin/micromamba" shell hook -s bash)" || return 1
    micromamba activate rtlforge || return 1

    if ! command -v nvidia-smi >/dev/null; then
        echo "no nvidia-smi: this is not a GPU node (did you qsub -q gpu?)" >&2
        return 1
    fi

    best=$(nvidia-smi --query-gpu=index,memory.free --format=csv,noheader,nounits \
           | sort -t, -k2 -nr | head -1)
    export CUDA_VISIBLE_DEVICES="${best%%,*}"
    free="${best##*, }"
    echo "GPU $CUDA_VISIBLE_DEVICES: ${free} MiB free"
    if [ "${free:-0}" -lt "$need_mib" ]; then
        echo "under ${need_mib} MiB free on every GPU -- node is full." >&2
        echo "exit, and qsub again for a different node." >&2
        return 1
    fi

    # One server per job, on a free port, stopped by PID. The old fixed
    # port plus `pkill -f 'ollama serve'` let concurrent jobs on one node
    # kill each other's servers: on 2026-11-09 the gpt-oss M1a job lost
    # its server and recorded 264 connection errors as answers.
    if [ -n "${OLLAMA_PID:-}" ] && kill -0 "$OLLAMA_PID" 2>/dev/null; then
        kill "$OLLAMA_PID" 2>/dev/null; sleep 1      # our own, from a previous source
    fi
    local port="${OLLAMA_PORT:-$(python -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')}"
    export OLLAMA_HOST="127.0.0.1:${port}"
    export OLLAMA_BASE_URL="http://127.0.0.1:${port}/v1"
    export OLLAMA_CONTEXT_LENGTH="${RTLFORGE_CTX:-8192}"
    export OLLAMA_MAX_LOADED_MODELS=1
    local log="$HOME/ollama-$HOSTNAME-$port.log"

    nohup "$HOME/.local/bin/ollama" serve > "$log" 2>&1 &
    export OLLAMA_PID=$!

    until curl -sf "http://$OLLAMA_HOST/api/tags" >/dev/null; do
        sleep 2; waited=$((waited + 2))
        if ! kill -0 "$OLLAMA_PID" 2>/dev/null || [ "$waited" -ge 60 ]; then
            echo "ollama did not come up; see $log" >&2
            tail -5 "$log" >&2
            return 1
        fi
    done
    echo "ollama pid $OLLAMA_PID on port $port (stop it with: kill \$OLLAMA_PID)"
    "$HOME/.local/bin/ollama" list
    echo "context ${OLLAMA_CONTEXT_LENGTH}, needed ${need_mib} MiB free"
    cd "$HOME/rtlforge" || return 1
    echo "ready. next: python tools/diagnose.py --preflight-only"
}

_gpu_up
