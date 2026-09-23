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

## The matrix

Surveyed 2026-09-23, before 000655 and 000656, and updated as they
landed: the rows marked **new** are theirs.

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
| **new** push a file | `POST /protocols/push` | `protocol_push` | `protocol push FILE --into OWNER/PROJECT [--org]` | `push_protocol` |
| **new** export to a file | `GET /protocols/:id/export?version=N` | `protocol_export` | `protocol export PROTOCOL [--version N] [-o FILE]` | `export_protocol` |
| publish / unpublish | `POST …/versions/:n/publish`, `/unpublish` | - | `protocol publish`, `protocol unpublish` | `publish_protocol_version`, `unpublish_protocol_version` |
| copy | `POST …/versions/:n/copy` | - | `protocol copy` | `copy_protocol_version` |
| changelog, dependencies, citations | `GET /protocols/:id/changelog`, `/dependencies`, `/citations` | - | - | - |
| **Runs and jobs** | | | | |
| launch, with a **new** label | `POST /protocols/:id/runs` `{label}` | `run` (**new**; `run_protocol` runs a built-in kind in-process) | `run PROTOCOL --label TEXT` | `launch(label=)` |
| **new** relabel a run | `PATCH /runs/:id` (run or job id) | `label` | `label RUN TEXT \| --clear` | `label_run` |
| list a protocol's runs, by binding | `GET /protocols/:id/runs?binding.k=v&label=` | - | `result --protocol --bind` (reads one) | `results_for` |
| **new** list runs by label, protocol, project | `GET /runs?label=&labelContains=&protocol=&project=&owner=&limit=` | `runs` | `runs [--label] [--label-contains] [--protocol] [--project] [--owner] [--limit] [--json]` | `runs` |
| **new** read one run | `GET /runs/:id` | - | - | - |
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
| **Spend** | `spentUsd`, `budgetUsd` on each job and each `runs` row; no total | on `runs` rows | `watch`, `runs` print it | on `get_job`, `runs` |
| **History** | `GET /history/:kind/:id`, `/history/object/~at` | - | `history` | `history` |
| **Deletion** | `DELETE` on objects, protocols, jobs, articles, datasets, projects (`?dryRun=1`) | - | `delete` | `delete` |

A run's history (`history job <id>`) now carries its relabels as
`run.label` events, beside `run.create`.

### Headline gaps, as surveyed

1. **MCP is almost empty.** Three tools, one of which (`run_protocol`)
   runs a built-in kind in-process rather than a protocol. Nothing an
   experiment does (launch, watch, read, protocols, labels, deletion,
   history) is reachable from MCP. *Now: `run`, `runs`, `label`,
   `protocol_push`, `protocol_export`. Still missing: watch, result,
   cancel, delete, history, publish, copy.*
2. **Protocols have no file form.** There is no push and no export, so
   every experiment wrote an author script around `create_protocol`.
   *Closed by 000655.*
3. **Runs cannot be named or found by name.** No label, no listing by
   project or label; every experiment keeps a `jobs.json`. *Closed by
   000656.*
4. **The CLI stops at one protocol.** No protocol list or read, no job
   list, no rerun, and `run` could not launch: it passed legacy
   `bindings` positionally to `bench.launch`, which compute 650a68f
   (000565) made keyword-only, so every `mechbench run <protocol>` raised
   `TypeError` (its tests used a fake with the old signature). *`run`
   fixed on this branch, and the tests' fakes are now held to the real
   signatures; `runs` lists jobs by run. Still missing: protocol list and
   read, rerun.*
5. **Articles, datasets and projects** are API-only.
6. **Spend** has no total anywhere; it is per job only (and per `runs`
   row).

Every open row is 000661's to close or to write down with its reason;
the suite check that fails on an unexplained gap is 000661's too.
