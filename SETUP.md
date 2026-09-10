# RTLForge — Local Setup, Step by Step

Written for **Windows + WSL2**, since that is what a Vivado machine usually is.
A native Linux / macOS section is at the end; skip Phases 1–2 if that's you.

Every phase ends with a **Checkpoint** showing the exact output you should see.
Do not move on until it matches. Debugging phase 8 when phase 3 was broken is
the single biggest time sink in a project like this.

Rough timings: Phases 1–3 about an hour, mostly downloads. Phases 4–6 about
twenty minutes. Phase 7 onward is the actual work.

---

## Phase 0 — What you are installing, and why

| Thing | Why |
|---|---|
| WSL2 + Ubuntu | Verilator, Icarus and Yosys are Linux-native. Windows builds exist and are a source of pain you do not need. |
| Docker Desktop | Runs the sandboxed EDA container. Uses WSL2 as its backend. |
| VS Code + Remote-WSL | Edit Windows-side, execute Linux-side, one window. |
| Python 3.11+ | The orchestrator. |
| EDA tools, bare metal | Phase 5 installs them directly in WSL so you can validate the harness *before* adding container complexity. |

The order matters: **bare metal first, containers second.** If you containerize
before the harness works, every bug has two possible causes instead of one.

---

## Phase 1 — WSL2 and Ubuntu

Open **PowerShell as Administrator**:

```powershell
wsl --install -d Ubuntu-22.04
```

Reboot when prompted. On first launch Ubuntu asks for a username and password —
this is your Linux user, unrelated to your Windows login. Pick a password you
will actually remember; you need it for every `sudo`.

Then inside the Ubuntu terminal:

```bash
sudo apt update && sudo apt upgrade -y
```

### Checkpoint 1

```bash
wsl --status          # in PowerShell: should say "Default Version: 2"
lsb_release -a        # in Ubuntu: should say Ubuntu 22.04
```

If `wsl --status` reports version 1, run `wsl --set-default-version 2` and
reinstall the distro. Version 1 will not run Docker properly.

---

## Phase 2 — Docker Desktop

1. Download Docker Desktop for Windows from docker.com and install it.
2. During install, leave **"Use WSL 2 instead of Hyper-V"** ticked.
3. Launch it, then open **Settings → Resources → WSL Integration**.
4. Enable the toggle for **Ubuntu-22.04**. This is the step people miss — without it
   the `docker` command does not exist inside WSL.
5. Apply & Restart.

### Checkpoint 2

From the **Ubuntu** terminal, not PowerShell:

```bash
docker run --rm hello-world
```

Expect `Hello from Docker!`. If you get `command not found`, WSL integration
is off — go back to step 3. If you get a permission or socket error, make sure
Docker Desktop is actually running in the Windows tray.

---

## Phase 3 — VS Code

Install VS Code on **Windows** (not inside WSL). Then install these extensions:

| Extension | Publisher | Purpose |
|---|---|---|
| WSL | Microsoft | The critical one. Runs VS Code's backend inside Linux. |
| Python | Microsoft | Interpreter, debugger |
| Pylance | Microsoft | Type checking |
| Docker | Microsoft | Container view in the sidebar |
| Verilog-HDL/SystemVerilog | mshr-h | Syntax highlighting for your RTL and testbenches |
| Even Better TOML | tamasfe | Optional, config files |

Now connect: press `Ctrl+Shift+P`, type **"WSL: Connect to WSL"**, hit enter.
A new window opens. **Check the bottom-left corner — it must read
`WSL: Ubuntu-22.04`.** If it says anything else you are editing on Windows and
nothing in this guide will work.

### Checkpoint 3

In the VS Code integrated terminal (`` Ctrl+` ``):

```bash
uname -a        # should mention Linux and microsoft-standard-WSL2
pwd             # should be /home/<your-linux-username>
```

If `pwd` shows `/mnt/c/Users/...` you opened a Windows folder through WSL.
Close it and use `File → Open Folder → /home/<you>/` instead.

**Keep the project inside the Linux filesystem** (`/home/you/...`), never in
`/mnt/c/...`. Filesystem calls across the Windows/Linux boundary are roughly an
order of magnitude slower, and this project makes thousands of them per run.

---

## Phase 4 — Get the project in place

```bash
mkdir -p ~/projects && cd ~/projects
# copy the rtlforge folder here, then:
cd rtlforge
```

Set up a virtual environment:

```bash
sudo apt install -y python3-venv python3-pip
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Tell VS Code about it: `Ctrl+Shift+P` → **"Python: Select Interpreter"** →
choose `./.venv/bin/python`.

### Checkpoint 4

```bash
which python        # ~/projects/rtlforge/.venv/bin/python
python -c "import requests; print('ok')"
```

Add `source .venv/bin/activate` to the top of your workflow, or you will spend
an afternoon on a `ModuleNotFoundError` that is just the wrong interpreter.

---

## Phase 5 — EDA tools, bare metal

```bash
sudo apt install -y verilator iverilog yosys
```

### Checkpoint 5

```bash
verilator --version     # Verilator 5.x
iverilog -V | head -1   # Icarus Verilog version 12.0
yosys -V                # Yosys 0.3x
```

Ubuntu 22.04 ships Verilator 4.x, which is older than the 5.020 the parsers were
captured against. The error format is compatible, but if `pytest` fails on a
Verilator test later, this is why — check with `verilator --version` and either
upgrade or re-capture the format:

```bash
verilator --lint-only -Wall examples/counter_syntax_error.v
```

Paste what it prints into `tests/test_parsers.py` as the new fixture string.

---

## Phase 6 — Harness self-test (no LLM, no API key)

This is the most important checkpoint in the guide.

```bash
python -m pytest tests/ -q
```

Expect `11 passed`.

```bash
python -m rtlforge.cli check --problem problems/counter --rtl examples/counter_good.v
```

### Checkpoint 6

```
[PASS] lint
[PASS] compile
[PASS] simulate
[PASS] synth  cells=10
```

`cells=10` may differ if your Yosys version differs — that's fine, any number
is fine. What matters is four PASS lines.

Now confirm it *catches* things. Each of these must fail at a different stage:

```bash
python -m rtlforge.cli check --problem problems/counter --rtl examples/counter_syntax_error.v
#   -> FAIL at lint

python -m rtlforge.cli check --problem problems/counter --rtl examples/counter_sync_reset_bug.v
#   -> PASS lint, PASS compile, FAIL simulate

python -m rtlforge.cli check --problem problems/counter --rtl examples/counter_missing_enable_bug.v
#   -> PASS lint, PASS compile, FAIL simulate
```

A harness that only ever says PASS is worthless. You have now proven it says
FAIL for the right reasons at the right stage.

---

## Phase 7 — API key

Pick one. Both are free and need no card.

**Groq** — published limits, easiest to reason about.
1. Go to `console.groq.com/keys`, sign in with Google or GitHub.
2. Create API Key, copy it immediately (shown once).

**Google AI Studio** — stronger models, limits visible only after sign-in.
1. Go to `aistudio.google.com/apikey`.
2. Create API key.

Then:

```bash
cp .env.example .env
nano .env        # paste your key after GROQ_API_KEY=
```

Load it into your shell:

```bash
set -a && source .env && set +a
```

### Checkpoint 7

```bash
echo ${GROQ_API_KEY:0:6}     # should print the first few characters
```

`.env` is already in `.gitignore`. Confirm with `git check-ignore -v .env`
before your first commit. A leaked key in git history is a genuine nuisance to
clean up.

---

## Phase 8 — First closed loop

```bash
python -m rtlforge.cli run --problem problems/counter --level full
```

### Checkpoint 8

```
PASS  counter  level=full  iters=1  cells=10  tokens=412+180  4.2s
log: ./results/20260909-143022
```

`iters=1` means the model got it right first try — likely for a counter. To see
the repair loop actually work, use a weaker model:

```bash
python -m rtlforge.cli run --problem problems/counter --level full \
    --model openai/gpt-oss-20b
```

Open the JSON in `results/`. Each attempt records its stages and diagnostics.
**Read one of these end to end now** — understanding the log format is what
makes the results interpretable later.

Common failures at this phase:

| Symptom | Cause |
|---|---|
| `GROQ_API_KEY is not set` | You did not `source .env` in *this* terminal |
| `401` | Key copied with trailing whitespace |
| `429` immediately | Free-tier daily cap; wait or switch provider |
| Passes but `iters=5` and never converges | Model is ignoring feedback — inspect the repair prompts in the JSON |

---

## Phase 9 — Containers

Only now. The harness already works, so anything that breaks here is a
container problem, which is a much smaller search space.

```bash
docker compose build          # a few minutes the first time
docker compose up -d
docker compose ps             # both services should show "running"
```

The design: **agent** holds your API key and has network access but no EDA
tools; **eda** has the tools and `network_mode: none`. Generated RTL is
untrusted input and only ever executes in the half that cannot reach the
internet.

Two ways to run:

**A. Orchestrator on the host, tools in the container** (recommended while
developing — you keep VS Code's debugger):

```bash
export RTLFORGE_EDA=docker:rtlforge-eda-1
export RTLFORGE_EDA_WORKDIR=/work
python -m rtlforge.cli check --problem problems/counter --rtl examples/counter_good.v
```

Check the container name first with `docker compose ps` — Compose v2 names it
`<folder>-eda-1`, so it depends on your directory name.

**B. Everything in containers:**

```bash
docker compose exec agent python -m rtlforge.cli run --problem problems/counter
```

### Checkpoint 9

Same four PASS lines as Checkpoint 6, but now produced inside the container.

```bash
docker compose exec eda verilator --version    # tools are in here
docker compose exec eda ping -c1 google.com    # must FAIL — that's the point
```

The second command failing is a successful test.

If Docker is unavailable on a machine later (the lab may not grant you the
`docker` group), unset `RTLFORGE_EDA` and everything falls back to bare metal.

---

## Phase 10 — Git

```bash
git init
git add -A
git status                     # verify .env is NOT listed
git commit -m "RTLForge: EDA harness, repair loop, container split"
```

Then create an empty repo on github.com/Jehka and:

```bash
git remote add origin git@github.com:Jehka/rtlforge.git
git branch -M main
git push -u origin main
```

Commit after every green checkpoint from here on. When you start changing
prompts and feedback formats, you will want to bisect which change moved the
numbers.

---

## Phase 11 — Moving to the lab GPU

Do this only after Phases 1–10 are green locally. The migration is then small.

**Before you go, find out three things:**

1. Are you in the `docker` group? `groups | grep docker`. If not, ask, and ask
   about Apptainer as the fallback in the same message.
2. Is there a scheduler (Slurm, PBS)? If yes you submit jobs rather than
   running interactively, and your CLI calls go in a batch script.
3. Where is scratch storage, and is your home directory quota'd? Model weights
   are tens of GB.

**Local free API path** (works today, no GPU needed):

```bash
curl -fsSL https://ollama.com/install.sh | sh
ollama serve &
ollama pull qwen2.5-coder:7b
python -m rtlforge.cli run --problem problems/counter --provider ollama
```

This runs on CPU on your laptop. It will be slow — minutes per generation — but
it proves the `--provider ollama` path works before you depend on lab hardware.
That is the whole point of doing it locally first.

**On the lab machine:**

```bash
nvidia-smi                  # confirm V100s and free memory
ollama serve &
ollama pull qwen2.5-coder:7b
python -m rtlforge.cli sweep --problem problems/counter --provider ollama --trials 5
```

Volta caveats, so you are not surprised: no bf16, no FP8, and Flash Attention 2
needs Ampere or newer. Many modern quantization kernels will not run. Plain
fp16 7B is the safe target — about 15 GB, comfortable in 32 GB. A 14B model is
tight and needs short context. Benchmark this on day one rather than planning
around a 32B model that will not load.

If the GPU is contended or the software stack fights you, the free APIs from
Phase 7 remain a complete substitute for everything except the
local-vs-cloud comparison.

---

## Native Linux / macOS

Skip Phases 1–2. Everything else is identical, except:

```bash
# Debian / Ubuntu
sudo apt install -y verilator iverilog yosys python3-venv

# macOS
brew install verilator icarus-verilog yosys
```

On macOS, Yosys cell counts will differ slightly from Linux. Do not compare
area numbers across operating systems in your results.

---

## Daily workflow, once set up

```bash
cd ~/projects/rtlforge
source .venv/bin/activate
set -a && source .env && set +a
python -m pytest tests/ -q                    # 5 seconds, catches regressions
python -m rtlforge.cli run --problem problems/counter --level full
```

## Where to go next

1. **Add two problems** — an FSM and a synchronous FIFO. Write the spec, write
   the testbench, then validate the testbench against one correct design and
   two deliberately broken ones *before* pointing a model at it. The counter
   testbench in this repo was written wrong twice; assume yours will be too.
2. **Run the sweep** with five trials per level. That is your first real data.
3. **Swap in VerilogEval v2** rather than hand-writing more oracles.
4. **Then** the small-model-vs-feedback experiment on the lab GPU.
