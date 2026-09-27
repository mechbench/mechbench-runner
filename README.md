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

1. **Command line.** `mechbench <noun> <verb>` lists, reads, pushes, launches and deletes the platform's objects, protocols, runs, models, articles, datasets, projects and threads, as a person or an agent with a shell.
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

Upgrades and restarts the service. **Re-running the install command does
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
