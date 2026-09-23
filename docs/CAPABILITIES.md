# The capability matrix

Task 000661, epic 000654. Every capability an agent needs, against the
three surfaces an agent can reach it on, with a fourth, the Python
`mechbench_compute.bench` module, noted beside them:

- **API**: mechbench-api, `src/routes/`.
- **MCP**: the tools in `mechbench_runner/mcp_server.py` (`mechbench mcp`).
- **CLI**: the `mechbench` command (`mechbench/cli.py`, verbs in
  `mechbench_runner/bench_cmd.py`).
- **bench**: `mechbench_compute/bench.py`, which the CLI verbs wrap.

A dash is a gap. The rule is one verb, designed once, with the same
name and arguments on each surface; how the name is spelled on each is
fixed below so that a verb is found on one surface from its name on
another.

## Spelling a verb on each surface

| Surface | Spelling | Example |
|---|---|---|
| CLI | `mechbench <noun> <verb>`, or a bare verb for the commonest | `mechbench protocol publish`, `mechbench run` |
| MCP | the CLI words joined with `_` | `protocol_publish`, `run` |
| bench | `<verb>_<noun>`, the module's established order | `publish_protocol_version`, `launch` |
| API | the resource and an action segment | `POST /protocols/:id/versions/:n/publish` |

The arguments keep one name everywhere: `protocol` (an id), `version`,
`into` (`owner/project`), `params`, `inputs`, `keep`, `budget`, `label`.

## Surveyed 2026-09-23, before 000655 and 000656

| Capability | API | MCP | CLI | bench |
|---|---|---|---|---|
| **Objects** | | | | |
| read one | `GET /objects/:path` | `get_result` | `result` (a node of a job) | `fetch`, `fetch_envelope` |
| write one | `PUT /objects/:path` | - | - | `emit` |
| list under a prefix | `GET /objects?prefix=` | - | - | `listing` |
| items of a collection | `GET /objects/~items` | - | - | `fetch_items` |
| lineage | `GET /objects/~lineage` | - | - | `lineage` |
| inventory | `GET /objects/~inventory` | - | - | `list_prefix_hashes` (hashes only) |
| checkpoint files | `PUT /objects/:path` (bytes) | - | - | `put_file`, `get_file_chunks` |
| kinds | `GET/PUT /kinds/:path` | - | - | `get_kind`, `register_kind` |
| **Protocols** | | | | |
| create | `POST /protocols` | - | - | `create_protocol` (PATCHes on a taken name) |
| read | `GET /protocols/:id` | - | - | `get_protocol` |
| list | `GET /protocols?owner=` | - | - | - |
| update (new version) | `PATCH /protocols/:id` | - | - | `create_protocol` (on a taken name) |
| a sealed version | `GET /protocols/:id/versions/:n` | - | - | - |
| push a file | - | - | - | - |
| export to a file | - | - | - | - |
| publish / unpublish | `POST …/versions/:n/publish`, `/unpublish` | - | `protocol publish`, `protocol unpublish` | `publish_protocol_version`, `unpublish_protocol_version` |
| copy | `POST …/versions/:n/copy` | - | `protocol copy` | `copy_protocol_version` |
| changelog, dependencies, citations | `GET /protocols/:id/changelog`, `/dependencies`, `/citations` | - | - | - |
| **Runs and jobs** | | | | |
| launch | `POST /protocols/:id/runs` | - (`run_protocol` runs a built-in kind in-process, not a protocol) | `run` | `launch` |
| label a run | - | - | - | - |
| list a protocol's runs, by binding | `GET /protocols/:id/runs?binding.k=v` | - | `result --protocol --bind` (reads one) | `results_for` |
| list runs by label, project | - | - | - | - |
| list jobs | `GET /jobs` (`?project=`, `?owner=`) | `list_jobs` | - | - |
| read a job | `GET /jobs/:id` | - | - | `get_job` |
| watch | (poll `GET /jobs/:id`) | - | `watch` | `watch` |
| cancel | `POST /jobs/:id/cancel` | - | `cancel` | `cancel` |
| rerun | `POST /jobs/:id/rerun` | - | - | - |
| **Results** | | | | |
| read a node's output | `GET /objects/<resultPath>/<node>` | `get_result` (by path) | `result` | `result` |
| **Articles** | full CRUD, versions, delta, media, comments | - | - | - |
| **Datasets** | `POST /datasets`, `/register`, list, read, `PATCH` | - | - | - |
| **Projects** | `POST /projects`, list, read, `PATCH`, transfer, members, audit | - | - | `path` (builds one, no call) |
| **Runners** | `GET /runners`, `PATCH`, `DELETE`, `POST /:id/commands` | - | `login`, `logout`, `whoami`, `status`, `pause`, `resume`, `restart` (this machine only) | - |
| **Spend** | `spentUsd`, `budgetUsd` on each job; no total | - | `watch` prints it | on `get_job` |
| **History** | `GET /history/:kind/:id`, `/history/object/~at` | - | `history` | `history` |
| **Deletion** | `DELETE` on objects, protocols, jobs, articles, datasets, projects (`?dryRun=1`) | - | `delete` | `delete` |

### Headline gaps

1. **MCP is almost empty.** Three tools, one of which (`run_protocol`)
   runs a built-in kind in-process rather than a protocol. Nothing an
   experiment does (launch, watch, read, protocols, labels, deletion,
   history) is reachable from MCP.
2. **Protocols have no file form.** There is no push and no export, so
   every experiment wrote an author script around `create_protocol`
   (000655).
3. **Runs cannot be named or found by name.** No label, no listing by
   project or label; every experiment keeps a `jobs.json` (000656).
4. **The CLI stops at one protocol.** No protocol list or read, no job
   list, no rerun, and `run` could not launch: it passed legacy
   `bindings` positionally to `bench.launch`, which compute 650a68f
   (000565) made keyword-only, so every `mechbench run <protocol>` raised
   `TypeError` (its tests used a fake with the old signature).
5. **Articles, datasets and projects** are API-only.
6. **Spend** has no total anywhere; it is per job only.
