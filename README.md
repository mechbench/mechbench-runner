# mechbench (the runner)

The `mechbench` command: what you install on a machine to connect it to
[mechbench.ai](https://mechbench.ai). The repository keeps its old name
— the PyPI package and the command are `mechbench`, and
the distribution ships two modules: `mechbench`, the bare front door,
and `mechbench_runner`, the engine it dispatches into.

The machine-side process of the [mechbench](https://mechbench.ai) family: it claims queued jobs from `mechbench-api`, executes them against `mechbench-compute`, and posts results back. It is also the command line for the platform's verbs, `mechbench <noun> <verb>`, for a person or an agent with a shell.

**Status:** in use. `login` pairs a machine with an account; the runner then claims and executes jobs, reports progress and preparing steps, holds a live WSS channel for control and telemetry, and installs as a launchd or systemd service so it survives reboots. `doctor` tells you whether a machine will work before it tries. `mechbench <noun> <verb>` reaches every verb the platform has; see docs/CAPABILITIES.md.

## What this repo is for

Two surfaces for different callers:

1. **Command line.** `mechbench <noun> <verb>` lists, reads, pushes, launches and deletes the platform's objects, protocols, runs, models, articles, datasets, projects, threads, ops, extensions and policies, as a person or an agent with a shell.
2. **Job-runner.** Polls `mechbench-api`'s `/jobs/next` for queued protocols, runs them against `mechbench-compute`, posts results back.

An agent without a shell connects to the platform's MCP server at `https://api.mechbench.ai/mcp`, which has the same verbs; it is not part of this package.

## Architectural decisions

- **Python.** `mechbench-compute` is Python; delegating to Python via RPC or subprocess-shell from a TS runner adds a layer that pays no dividends.
- **One binary.** `mechbench run` starts the job-runner loop; the nouns and the machine's own commands (`login`, `doctor`, `status`, …) are its other subcommands.
- **Agent authenticates to `mechbench-api` with a dedicated API key**, not a user's personal session. Export `MECHBENCH_API_KEY` (mint one at `/settings/api-keys`, or via `POST /auth/api-keys`).

## Install

```bash
uv tool install --managed-python mechbench
mechbench login
```

`--managed-python` has uv fetch its own interpreter rather than adopt
whichever `python3` the machine happens to have. It costs a one-time
download and buys a version we support (3.11–3.14) on a machine whose
own Python we then never touch. `pipx install mechbench` works
too, against an interpreter you already have.

`login` prints a link and waits. Open it, approve the machine — the page
names it, along with its host and platform, before you do — and the
runner collects a credential it writes to `~/.mechbench/config.toml`
(mode 0600). Nothing durable passes through your hands: the code in the
URL grants nothing on its own, and the key is minted directly to the
machine that asked.

For a machine with no browser, `mechbench login --token mbr_…`
takes a single-use token minted at [mechbench.ai/download](https://mechbench.ai/download).

`login` then offers to start the runner automatically. Say yes and there
is nothing further to do: it starts at login, comes back after a crash,
and is controlled from the website.

### Updating

```bash
mechbench update
```

Installs the versions the platform's signed release manifest names
(`GET /releases/manifest`), every package pinned to its sha256, and
restarts the service; it never takes PyPI's newest.
**Re-running the install command does
not upgrade anything** — `uv tool install` treats an already-installed
tool as nothing to do and reports that in a way that reads like success,
so a machine can sit on an old version while looking freshly installed.
`update` verifies by reading the installed version back afterwards
rather than trusting an exit code, and rolls back if the new version
cannot start.

`mechbench doctor` answers "will this actually work here" —
Python, backend, credentials, API, model cache, disk — before you find
out the slow way.

Running a model needs Apple Silicon (the MLX backend from
`mechbench-compute`). The rest installs anywhere.

### Running it yourself

```bash
mechbench run              # foreground, ^C to stop
mechbench install-service  # or have the OS keep it running
mechbench service-status
```

The service is supervised by launchd or systemd rather than by anything
we wrote, through the exit code (`mechbench_runner/exits.py`): 0 is a
deliberate stop (signed out, SIGTERM) and stays stopped, 1 is a crash or
a wedge and is restarted with a throttle, and 75 asks to be restarted,
which is how an approved update gets a fresh process to install into.

**On macOS you will be told that software from "Ned Deily" can run in
the background.** That is this runner. macOS attributes a background
item to whoever code-signed the executable, and the executable is the
Python interpreter, which Ned Deily signs as CPython's macOS release
manager. Turning it off in Login Items & Extensions stops the runner;
`mechbench doctor` reports it if that happens.

### A dedicated machine (a rented Mac Studio)

The steps for a fresh Mac that runs nothing but the runner, reached over
SSH. Checked on macOS 27.0 (26A428), the home machine, which is newer
than the Tahoe (26.x) the rented Studios run; nothing below changed
between them.

1. **Check the OS and the chip.** `sw_vers` and `sysctl -n
   machdep.cpu.brand_string`. Apple Silicon is required.
2. **Keep it awake and bring it back after a power cut.** The runner
   holds the machine awake only while a job runs; an idle machine that
   sleeps stops claiming.

   ```bash
   sudo pmset -a sleep 0 disksleep 0 autorestart 1
   ```

3. **Log the runner's user in automatically.** A LaunchAgent starts when
   its user's session does, so after a reboot nothing runs until someone
   logs in. With FileVault off (`fdesetup status`), turn on automatic
   login for the user in System Settings → Users & Groups, or
   `sudo sysadminctl -autologin set -userName "$USER" -password -`.
4. **Install and sign in.** No browser is needed: mint a single-use
   token at [mechbench.ai/download](https://mechbench.ai/download).
   `scripts/dedicated-mac.sh` does steps 1, 2, 4, 5 and 7 unattended —
   paste it (with the token) into the provider's startup script.

   ```bash
   curl -LsSf https://astral.sh/uv/install.sh | sh
   uv tool install --managed-python mechbench
   mechbench login --token mbr_…
   mechbench install-service
   ```

5. **Check the service and its class.**

   ```bash
   mechbench service-status
   launchctl print gui/$(id -u)/ai.mechbench.runner | grep -E 'state =|spawn type'
   ```

   Expect `state = running` and `spawn type = daemon (3)`, which is
   `ProcessType Standard`. `background (5)` is the class that ran jobs
   1.8× slower; measured again on macOS 27, a CPU loop under it
   took 3.4× as long as under Standard, and Standard matched a terminal.
   Over SSH with nobody logged in there is no `gui/` domain: the agent is
   bootstrapped into `user/<uid>` instead (its plist allows both session
   types), and `launchctl print user/$(id -u)/ai.mechbench.runner` shows
   it. It moves to `gui/` at the next login.
6. **Keep the weights and `~/.mechbench` out of Desktop, Documents and
   Downloads.** macOS asks the person at the screen before a background
   process reads those, and on a headless box nobody answers. The
   Hugging Face cache (`~/.cache/huggingface`) and `~/.mechbench` are in
   the home directory proper, which needs no permission.
7. **Calibrate** (`mechbench calibrate`, below), then reboot
   (`sudo reboot`) and check that `mechbench service-status` says
   running without anyone logging in by hand.

Restart it with `mechbench restart` (a `launchctl kickstart`), never by
killing the process: the `run` child and the supervisor go together.
Upgrades follow the runner's policy (`upgrades.compute`), or
`mechbench update` by hand.

### Calibrating a machine

```bash
mechbench calibrate                                   # micro-benchmarks, and Gemma 4 E2B if it is cached
mechbench calibrate --model mlx-community/gemma-4-e2b-it-bf16 --out cal.json
mechbench calibrate --push --into benji/lab           # stores it at benji/lab/calibration/<chip>-<fingerprint>
```

`calibrate` is `mechbench runner calibrate`. It times two
micro-benchmarks that contain none of our code (a 256 MiB memory copy
and a 4096² bf16 matmul), then, for a model: `load` with the weights
evicted from the page cache (cold) and cached (warm), one `forward` at
128 tokens, the added cost of `capture` at four layers, and one LoRA
`lora_step`. Each is repeated; the answer is the median, the spread
(interquartile range over the median) and the peak memory, with the
warm-up reported apart. It answers a calibration collection, one record
per (chip, stack fingerprint, model, dtype, primitive, shape), whose
header carries the machine, the stack and its fingerprint, and the
ambient load while it ran. The micro-benchmarks are kept in
`~/.mechbench/calibration/baseline.json`: the idle baseline the canary
compares against before and after each model-bearing node of every job.

On a CUDA machine `calibrate` runs on the torch backend (the backend the
machine offers first; `--backend torch` picks it anywhere, on the CPU
when there is no GPU):

```bash
mechbench calibrate --out g0.json                                  # the machine: probes and 5 minutes of sustained load
mechbench calibrate --model google/gemma-3-27b-it --repeats 2 \
  --sustained-minutes 0 --out g2.json                              # a model: load, prefill, decode, instrumentation, numerics
mechbench calibrate --model google/gemma-3-4b-it --residuals box.json --against home.json
```

The collection's header carries the GPU's identity (name, compute
capability, driver, CUDA, cuDNN, NCCL, unified memory, power limit,
persistence and MIG from nvidia-smi, the TF32, deterministic and SDPA
settings in force), what was skipped and why, the sustained-load series
and a `summary` of the numbers a session decides on. Every row carries
`backend`, `accelerator` and `device` beside the stack fingerprint, and a
`probe` naming what measured it. A probe that needs CUDA, or a
capability the installed compute lacks (LoRA training on torch before
compute 0.194.0), is listed under `skipped` with its reason rather than
failing; decode without compute's batched generation is timed with
transformers' `generate` and says so in `path`. `--residuals` writes the residual stream after every layer for
one fixed input (MLX writes the same file); `--against` runs a file's
tokens and puts the layer-by-layer difference in the header.

### From a checkout

```bash
git clone https://github.com/mechbench/mechbench-runner.git
cd mechbench
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
```

## Usage

### The platform's verbs

One command per noun, each taking a verb and its arguments, so
`mechbench protocol push draws.json --into benji/lab` is
`protocol(verb="push", args={"protocol": {...}, "into": "benji/lab"})`
on the platform's MCP server.
[docs/CAPABILITIES.md](docs/CAPABILITIES.md) lists every verb on the API,
MCP and the command line.

| command | verbs |
|---|---|
| `mechbench object` | list, read, items, write, update, delete, history |
| `mechbench protocol` | list, read, versions, push, export, update, edit, publish, unpublish, restore, copy, delete, history |
| `mechbench run` | list, jobs, read, launch, check, sweep, update, watch, result, diff, cancel, rerun, delete, history |
| `mechbench model` | check |
| `mechbench article` | list, read, create, update, edit, versions, restore, delete, history |
| `mechbench dataset` | list, read, create, update, delete, history |
| `mechbench project` | list, read, create, update, delete, history |
| `mechbench thread` | list, read, create, update, fork, delete, history |
| `mechbench op` | list, read, next |
| `mechbench extension` | new, test, push, verify, list, read, history, withdraw, visibility |
| `mechbench policy` | list, read, create, update, apply |
| `mechbench runner` | calibrate |

An extension's loop, from nothing to a verified version:

```bash
mechbench extension new alice/lab --name count-things --op records/count
mechbench extension test count-things            # conformance, here, examples twice
mechbench extension push count-things --draft    # usable by you at once
mechbench extension push count-things            # the same bytes again: verification queued on your runner
```

### Job-runner

Polls `mechbench-api` for queued jobs.

```bash
export MECHBENCH_API_URL=http://localhost:3000
export MECHBENCH_API_KEY=mbk_...
mechbench run
```

Ctrl-C exits cleanly. API-unreachable is retried with exponential backoff capped at 30 s.

### Smoke test

```bash
mechbench smoke            # this machine's key reaches the platform: run list + run read
```

## Configuration

All via env vars:

| var | default | purpose |
|---|---|---|
| `MECHBENCH_API_URL` | `https://api.mechbench.ai` | mechbench-api base URL. Set it to `http://localhost:3000` to develop against a local API. Ignored when credentials are stored, which carry their own. |
| `MECHBENCH_API_KEY` | *(from `login`)* | Overrides the stored credential entirely, URL included. For CI and containers, which have nowhere to put a config file. |
| `MECHBENCH_POLL_INTERVAL_SECONDS` | `2.0` | Job-runner poll cadence. |
| `MECHBENCH_WARM_MODEL_ID` | *(none)* | Optional model to load at startup so the first job skips cold start. There is deliberately no default: a protocol names the model it runs against, and a job that names none is an error. |
| `MECHBENCH_WATCHDOG_SECONDS` | `900` | How long without progress counts as wedged. `0` disables it. |
| `MECHBENCH_GPU_SAMPLE_SECONDS` | `1` | On a machine with an NVIDIA GPU, how often a job samples its utilization, memory, clocks, temperature, power and throttle state. `0` turns it off. |

## Relationship to other mechbench repos

- **`mechbench-compute`** — imported directly. `Model`, `Ablate`, hook-aware forward.
- **`mechbench-schema`** — produces `LayerAblationPayload` etc. as typed results.
- **`mechbench-api`** — the runner's only platform dependency. All workspace state (jobs, cache reads) goes through it.
- **`mechbench-ui`** — no coupling. UI queues jobs; the job-runner consumes them.
- **`mechbench-experiments`** — research scripts that use `mechbench-compute` directly, without the job machinery.

## Open design questions (deferred)

- **Structured-summary interface.** The family's philosophy doc describes a read-side surface where agents consume JSON summaries of findings / experiments. Currently implicit in `run list` + `run result`. A richer summary layer (`GET /summary`, `POST /query`) is still on the table but unbuilt.

## License

MIT.
