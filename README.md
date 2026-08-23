# mechbench-runner

The machine-side process of the [mechbench](https://mechbench.ai) family: it claims queued jobs from `mechbench-api`, executes them against `mechbench-compute`, and posts results back. It also exposes those same primitives as [Model Context Protocol](https://modelcontextprotocol.io) tools, so an LLM agent can call them directly.

**Status:** in use. `login` pairs a machine with an account; the runner then claims and executes jobs, reports progress and preparing steps, holds a live WSS channel for control and telemetry, and installs as a launchd or systemd service so it survives reboots. `doctor` tells you whether a machine will work before it tries. Three MCP tools (`run_protocol`, `get_result`, `list_jobs`) expose the same primitives to an agent.

## What this repo is for

Two adjacent surfaces for different callers:

1. **MCP server.** An LLM agent (Claude, others) connects via MCP stdio and calls mechbench primitives as structured tools. Tool bodies run in-process against `mechbench-compute`.
2. **Job-runner.** Polls `mechbench-api`'s `/jobs/next` for UI-queued protocols, runs them, posts results back. Same compute path as the MCP `run_protocol` tool; different *trigger*.

Both modes share one binary (`mechbench-runner`) with subcommands; they share the loaded model, API client, and protocol executor. Splitting into separate processes is a later operational decision — see "Open design questions" below.

## Architectural decisions (task 000185)

- **Python.** `mechbench-compute` is Python; delegating to Python via RPC or subprocess-shell from a TS runner adds a layer that pays no dividends in v0. The MCP Python SDK is mature.
- **One binary, two subcommands.** `mechbench-runner mcp` launches the MCP server over stdio; `mechbench-runner run` starts the job-runner loop. They share `ExperimentRunner` (owns the loaded Gemma model) and `ApiClient`.
- **Agent authenticates to `mechbench-api` with a dedicated API key**, not a user's personal session. Export `MECHBENCH_API_KEY` (mint one at `/settings/api-keys`, or via `POST /auth/api-keys`). Matches the pattern from the e2e trace.
- **MCP `run_protocol` runs in-process**, not queued through `mechbench-api`. The MCP caller wants the answer; we are the compute target. Job-queue round-tripping exists for the *UI-triggered* path (job-runner subcommand).
- **stdio transport only.** SSE / HTTP-SSE transports earn their seat once remote MCP deploy matters (deferred).

## Install

```bash
uv tool install --managed-python mechbench-runner
mechbench-runner login
```

`--managed-python` has uv fetch its own interpreter rather than adopt
whichever `python3` the machine happens to have. It costs a one-time
download and buys a version we support (3.11–3.14) on a machine whose
own Python we then never touch. `pipx install mechbench-runner` works
too, against an interpreter you already have.

`login` prints a link and waits. Open it, approve the machine — the page
names it, along with its host and platform, before you do — and the
runner collects a credential it writes to `~/.mechbench/config.toml`
(mode 0600). Nothing durable passes through your hands: the code in the
URL grants nothing on its own, and the key is minted directly to the
machine that asked.

For a machine with no browser, `mechbench-runner login --token mbr_…`
takes a single-use token minted at [mechbench.ai/download](https://mechbench.ai/download).

`login` then offers to start the runner automatically. Say yes and there
is nothing further to do: it starts at login, comes back after a crash,
and is controlled from the website.

### Updating

```bash
mechbench-runner update
```

Upgrades and restarts the service. **Re-running the install command does
not upgrade anything** — `uv tool install` treats an already-installed
tool as nothing to do and reports that in a way that reads like success,
so a machine can sit on an old version while looking freshly installed.
`update` verifies by reading the installed version back afterwards
rather than trusting an exit code, and rolls back if the new version
cannot start.

`mechbench-runner doctor` answers "will this actually work here" —
Python, backend, credentials, API, model cache, disk — before you find
out the slow way.

Running a model needs Apple Silicon (the MLX backend from
`mechbench-compute`). The rest installs anywhere.

### Running it yourself

```bash
mechbench-runner run              # foreground, ^C to stop
mechbench-runner install-agent    # or have the OS keep it running
mechbench-runner agent-status
```

The service is supervised by launchd or systemd rather than by anything
we wrote — see `mechbench_runner/exits.py` for the contract that makes
that work.

**On macOS you will be told that software from "Ned Deily" can run in
the background.** That is this runner. macOS attributes a background
item to whoever code-signed the executable, and the executable is the
Python interpreter, which Ned Deily signs as CPython's macOS release
manager. Turning it off in Login Items & Extensions stops the runner;
`mechbench-runner doctor` reports it if that happens.

### From a checkout

```bash
git clone https://github.com/mechbench/mechbench-runner.git
cd mechbench-runner
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
```

## Usage

### MCP server

Launch as a stdio MCP server — connect from Claude Desktop via `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "mechbench": {
      "command": "/abs/path/to/mechbench-runner/.venv/bin/mechbench-runner",
      "args": ["mcp"],
      "env": {
        "MECHBENCH_API_URL": "http://localhost:3000",
        "MECHBENCH_API_KEY": "mbk_..."
      }
    }
  }
}
```

Three tools appear in Claude:

| tool | description |
|---|---|
| `run_protocol` | Run a layer-ablation protocol in-process on a prompt; return per-layer damage. |
| `get_result` | Fetch a cached payload from `mechbench-api` by MechbenchPath. |
| `list_jobs` | List the caller's queued / running / completed jobs. |

### Job-runner

Polls `mechbench-api` for UI-queued jobs. Same compute path as `run_protocol`; different trigger.

```bash
export MECHBENCH_API_URL=http://localhost:3000
export MECHBENCH_API_KEY=mbk_...
mechbench-runner run
```

Ctrl-C exits cleanly. API-unreachable is retried with exponential backoff capped at 30 s.

### In-process smoke test

```bash
mechbench-runner smoke            # quick: list_jobs + get_result
mechbench-runner smoke --full     # adds run_protocol (42 forwards, ~1-2 min)
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

- **One binary or two processes?** Current answer: one binary, two subcommands. Revisit if MCP-caller frequency vs. job-runner throughput diverges enough to want independent scaling.
- **Structured-summary interface.** The family's philosophy doc describes a read-side surface where agents consume JSON summaries of findings / experiments. Currently implicit in `list_jobs` + `get_result`. A richer summary layer (`GET /summary`, `POST /query`) is still on the table but unbuilt.
- **MCP-surface observability.** Rate limits, per-tool metrics, audit trail for the tool-calling side. Deferred until a second LLM-agent consumer exists.

## License

MIT.
