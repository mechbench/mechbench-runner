# The capability matrix

Task 000661, epic 000654. Every noun an agent works with, and its verbs,
on the three surfaces an agent reaches the platform through:

- **API**: mechbench-api, `src/routes/`.
- **MCP**: the tools in `mechbench_runner/mcp_server.py` (`mechbench mcp`).
- **CLI**: the `mechbench` command (`mechbench/cli.py`).

The nouns and verbs are declared once, in `mechbench_runner/verbs/` (a module per noun),
and the command line and the MCP tools are both built from that
declaration, so a verb is on both or on neither. Each verb names its API
route. The matrix below is written from the declaration by
`scripts/capabilities.py`, and `tests/test_parity.py` fails when:

- a command on the command line, or an MCP tool, is neither a noun's verb
  nor listed below as on one surface with its reason;
- a noun is missing one of list, read, create, update, delete or history
  without a reason;
- a verb's API route is not declared in mechbench-api's routes (when a
  mechbench-api checkout sits beside this one);
- this file's matrix is not what the declaration writes.

The Python `mechbench_compute.bench` module is not one of the three
surfaces. The verbs it has (launch, push, export, publish, copy, cancel,
delete, history, result, emit) are what the command line and MCP call
for those verbs, so there is one client for each; the rest go straight
to the API through the runner's client.

## Spelling a verb on each surface

| Surface | Spelling | Example |
|---|---|---|
| CLI | `mechbench <noun> <verb>`, arguments as `--flags` or positionals | `mechbench protocol push draws.json --into benji/lab` |
| MCP | one tool per noun, `<noun>(verb, args)`, the arguments by the CLI's names | `protocol(verb="push", args={"file": "draws.json", "into": "benji/lab"})` |
| API | the resource and an action | `POST /protocols/push` |

An argument has one name on the command line and in MCP's `args`:
`id`, `path`, `version`, `into` (`owner/project`), `owner`, `project`,
`search`, `limit`, `offset`, `full`, `yes`, `acknowledge_citations`,
`label`, `params`, `inputs`, `keep`, `budget`. The API's field names are
the resource's own (`displayName`, `labelContains`, `view`).

## What every noun shares

- **Discovery.** `list` takes `search` (a name, title, slug or label
  containing the text), `limit` and `offset`, and the noun's own filters
  (owner, project, status, kind, prefix). It answers `{items, next}`,
  where `next` is the offset of the following page or null; the API sends
  it as `X-Next-Offset`, set when the database returned a full page, so a
  page filtered short by visibility never ends a walk early.
- **Reads are summaries.** A read answers the API's `view=summary`
  unless `full` is asked for: a protocol with its signature by name and
  its node count but not its graph; a run with its status, progress,
  error and missing nodes but not its job's spec; an article without its
  body; an object as its header (`GET /objects/~meta`: kind, size, hash,
  item count). The API's default stays `full`, which is what the UI has
  always read.
- **Items are read on the server.** `object items` (task 000660) answers
  a collection's items without the collection leaving the store: `fields`
  (dot paths, comma-separated; each item comes back flat, keyed by them),
  `where` (`PATH OP VALUE`, repeated for AND; OP is `=` `!=` `<` `<=` `>`
  `>=` or `~`, text contains or list has), `sort`/`order`, `offset`/
  `limit`, `lines`/`chars` to cut every string (a story's title is
  `--fields id,text --lines 1`), and `count` or `header` alone. The API
  answers `matched` beside `total` and `X-Next-Offset` while more pass.
  The command line prints JSON lines, or a table with `--table`;
  `bench.items` is the same call from Python.
- **Deletion is permanent.** Nothing restores what is deleted. `delete`
  is a dry run unless `yes` is given, and answers the API's plan
  (`deletes`, `keeps`, `refusal`, `citedBy`); the refusals the API makes
  (`LINEAGE_CHILDREN`, `INCLUDED`, `JOBS_RUNNING`, `DEPENDED_ON`,
  `EXTERNAL_REFERENTS`, …) and the citing articles (`CITED`, until
  `acknowledge_citations`) stand on every surface. What is deleted keeps
  its history.
- **History.** `history` is the lifetime's audit log, readable after
  deletion. A run's is its job's; an object's is by path (every lifetime
  that held the path) or by id.
- **Refusals are data on MCP.** An API refusal comes back as
  `{"error": {status, code, error, …}}`; an argument the verb does not
  take is refused with the verb's own list.

## Why MCP has a tool per noun

Every MCP tool's schema sits in an agent's context on every turn.
Measured as the server lists them (name, description and input schema,
compact JSON; `~tokens` is bytes / 4):

| Tool set | Tools | Verbs | Bytes | ~Tokens |
|---|---|---|---|---|
| released (`run_protocol`, `get_result`, `list_jobs`) | 3 | 2 | 1,072 | 268 |
| this branch before 000661 (those and `run`, `runs`, `label`, `protocol_push`, `protocol_export`) | 8 | 7 | 4,570 | 1,142 |
| **a tool per noun, the verb an argument (chosen)** | 7 | 46 | 7,785 | 1,946 |
| the same 46 verbs as a tool each, typed parameters | 46 | 46 | 27,828 | 6,957 |

A tool per noun keeps every verb's shape in one description line
(`read(id, version?, full?): …`) and its arguments in one free-form
`args` object, so the whole lifecycle of six nouns costs less than
twice what seven verbs cost as separate tools. What it gives up, typed
validation of `args` by the client, is done by the verb instead, which
refuses an unknown or missing argument by name. One tool for everything
(`mechbench(noun, verb, args)`) would save about another 1.2 KB of
per-tool overhead at the cost of a single description too long to scan.

<!-- verbs:begin (scripts/capabilities.py writes this) -->

| Noun | Verb | API | MCP | CLI |
|---|---|---|---|---|
| object | **list**(prefix?, kind?, search?, limit?, offset?) | `GET /objects` | `object(verb="list")` | `mechbench object list` |
| object | **read**(path, full?) | `GET /objects/~meta` | `object(verb="read")` | `mechbench object read` |
| object | **items**(path, fields?, where?, sort?, order?, offset?, limit?, lines?, chars?, count?, header?) | `GET /objects/~items` | `object(verb="items")` | `mechbench object items` |
| object | **write**(path, file?, payload?, inputs?) | `PUT /objects/:path` | `object(verb="write")` | `mechbench object write` |
| object | **update**(path, visibility, prefix?) | `PATCH /objects/:path` | `object(verb="update")` | `mechbench object update` |
| object | **delete**(path, prefix?, yes?, acknowledge_citations?) | `DELETE /objects/:path` | `object(verb="delete")` | `mechbench object delete` |
| object | **history**(path) | `GET /history/object/~at` | `object(verb="history")` | `mechbench object history` |
| object | create | — | — | — (write is its create: a path is written, not minted) |
| protocol | **list**(owner?, project?, search?, limit?, offset?, full?) | `GET /protocols` | `protocol(verb="list")` | `mechbench protocol list` |
| protocol | **read**(id, version?, full?, format?) | `GET /protocols/:id` | `protocol(verb="read")` | `mechbench protocol read` |
| protocol | **versions**(id, limit?, offset?) | `GET /protocols/:id/versions` | `protocol(verb="versions")` | `mechbench protocol versions` |
| protocol | **push**(file, into, org?) | `POST /protocols/push` | `protocol(verb="push")` | `mechbench protocol push` |
| protocol | **export**(id, version?, path?) | `GET /protocols/:id/export` | `protocol(verb="export")` | `mechbench protocol export` |
| protocol | **update**(id, name?, description?, visibility?, project?) | `PATCH /protocols/:id` | `protocol(verb="update")` | `mechbench protocol update` |
| protocol | **edit**(id, file?, description_file?, name?, base_version?, format?) | `PUT /protocols/:id` | `protocol(verb="edit")` | `mechbench protocol edit` |
| protocol | **publish**(id, version?) | `POST /protocols/:id/versions/:n/publish` | `protocol(verb="publish")` | `mechbench protocol publish` |
| protocol | **unpublish**(id, version) | `POST /protocols/:id/versions/:n/unpublish` | `protocol(verb="unpublish")` | `mechbench protocol unpublish` |
| protocol | **restore**(id, version) | `POST /protocols/:id/versions/:n/restore` | `protocol(verb="restore")` | `mechbench protocol restore` |
| protocol | **copy**(id, version?, into, name?, org?, dry_run?) | `POST /protocols/:id/versions/:n/copy` | `protocol(verb="copy")` | `mechbench protocol copy` |
| protocol | **delete**(id, yes?, acknowledge_citations?) | `DELETE /protocols/:id` | `protocol(verb="delete")` | `mechbench protocol delete` |
| protocol | **history**(id) | `GET /history/:kind/:id` | `protocol(verb="history")` | `mechbench protocol history` |
| protocol | create | — | — | — (push is its create: a file is pushed, by its name) |
| run | **list**(label?, label_contains?, status?, protocol?, project?, owner?, search?, limit?, offset?, full?) | `GET /runs` | `run(verb="list")` | `mechbench run list` |
| run | **read**(id, full?) | `GET /runs/:id` | `run(verb="read")` | `mechbench run read` |
| run | **launch**(protocol, params?, inputs?, keep?, budget?, label?) | `POST /protocols/:id/runs` | `run(verb="launch")` | `mechbench run launch` |
| run | **update**(id, label?, clear?) | `PATCH /runs/:id` | `run(verb="update")` | `mechbench run update` |
| run | **watch**(id, timeout?) | `GET /runs/:id` | `run(verb="watch")` | `mechbench run watch` |
| run | **result**(id, node) | `GET /objects/:path` | `run(verb="result")` | `mechbench run result` |
| run | **cancel**(id, reason?) | `POST /jobs/:id/cancel` | `run(verb="cancel")` | `mechbench run cancel` |
| run | **rerun**(id) | `POST /jobs/:id/rerun` | `run(verb="rerun")` | `mechbench run rerun` |
| run | **delete**(id, yes?, acknowledge_citations?) | `DELETE /jobs/:id` | `run(verb="delete")` | `mechbench run delete` |
| run | **history**(id) | `GET /history/:kind/:id` | `run(verb="history")` | `mechbench run history` |
| run | create | — | — | — (launch is its create: a run is a protocol launched) |
| article | **list**(owner?, status?, mine?, search?, limit?, offset?, full?) | `GET /articles` | `article(verb="list")` | `mechbench article list` |
| article | **read**(id, full?, format?) | `GET /articles/:id` | `article(verb="read")` | `mechbench article read` |
| article | **create**(slug, title, owner?, org?, subtitle?, body_file?, visibility?, tags?) | `POST /articles` | `article(verb="create")` | `mechbench article create` |
| article | **update**(id, title?, subtitle?, slug?, status?, visibility?, tags?, body_file?, base_version?) | `PATCH /articles/:id` | `article(verb="update")` | `mechbench article update` |
| article | **edit**(id, file?, body_file?, title?, subtitle?, tags?, base_version?, format?) | `PUT /articles/:id` | `article(verb="edit")` | `mechbench article edit` |
| article | **versions**(id) | `GET /articles/:id/versions` | `article(verb="versions")` | `mechbench article versions` |
| article | **restore**(id, version) | `POST /articles/:id/versions/:n/restore` | `article(verb="restore")` | `mechbench article restore` |
| article | **delete**(id, yes?) | `DELETE /articles/:id` | `article(verb="delete")` | `mechbench article delete` |
| article | **history**(id) | `GET /history/:kind/:id` | `article(verb="history")` | `mechbench article history` |
| dataset | **list**(owner?, search?, limit?, offset?, full?) | `GET /datasets` | `dataset(verb="list")` | `mechbench dataset list` |
| dataset | **read**(id, full?) | `GET /datasets/:id` | `dataset(verb="read")` | `mechbench dataset read` |
| dataset | **create**(object, slug, title, owner?, org?, description?, visibility?) | `POST /datasets/register` | `dataset(verb="create")` | `mechbench dataset create` |
| dataset | **update**(id, title?, description?, visibility?, slug?) | `PATCH /datasets/:id` | `dataset(verb="update")` | `mechbench dataset update` |
| dataset | **delete**(id, yes?) | `DELETE /datasets/:id` | `dataset(verb="delete")` | `mechbench dataset delete` |
| dataset | **history**(id) | `GET /history/:kind/:id` | `dataset(verb="history")` | `mechbench dataset history` |
| project | **list**(owner?, search?, limit?, offset?, full?) | `GET /projects` | `project(verb="list")` | `mechbench project list` |
| project | **read**(id, full?) | `GET /projects/:id` | `project(verb="read")` | `mechbench project read` |
| project | **create**(slug, owner?, org?, name?, description?) | `POST /projects` | `project(verb="create")` | `mechbench project create` |
| project | **update**(id, name?, description?, slug?) | `PATCH /projects/:id` | `project(verb="update")` | `mechbench project update` |
| project | **delete**(id, yes?, acknowledge_citations?) | `DELETE /projects/:id` | `project(verb="delete")` | `mechbench project delete` |
| project | **history**(id) | `GET /history/:kind/:id` | `project(verb="history")` | `mechbench project history` |

**Command line only.**

- `mechbench login`, `mechbench logout`, `mechbench whoami`, `mechbench doctor`, `mechbench models`, `mechbench budget`, `mechbench update`, `mechbench supervise`, `mechbench install-service`, `mechbench uninstall-service`, `mechbench service-status`, `mechbench status`, `mechbench pause`, `mechbench resume`, `mechbench restart`, `mechbench smoke`: this machine's runner, not the platform: an agent reaches the platform, and the person at the machine runs its service.
- `mechbench mcp`: starts the MCP server itself.

**Shorter names for a noun's verb** (the command line keeps them):

- `mechbench run`: run launch (`mechbench run PROTOCOL`); with no PROTOCOL, the runner loop.
- `mechbench runs`: run list.
- `mechbench label`: run update.
- `mechbench watch`: run watch (several runs at once).
- `mechbench result`: run result (`JOB/NODE`, or found by `--protocol --bind`).
- `mechbench cancel`: run cancel (several runs at once).
- `mechbench delete`: `<noun> delete`, the noun read from the id's prefix or a path.
- `mechbench history`: `<noun> history`, by kind and id.

**MCP only.**

- `run_protocol`: runs a built-in kind in-process on the machine serving MCP; the queued, recorded way to run is `run launch` on every surface.

**API only.**

- kinds (`GET/PUT /kinds/:path`): registered by compute releases, not by agents.
- object lineage and inventory (`~lineage`, `~inventory`): read through `bench.lineage` and the UI; `object list` and `object read` are the agent's discovery.
- checkpoint files (`PUT /objects/:path` bytes): written and read by the runner's own jobs.
- a protocol's changelog, dependencies and citations: the composer's publish review; `protocol versions` and `protocol history` are the agent's record.
- article delta, media, comments: the collaborative editor's; agents edit a whole article with `article edit`, markdown or delta, at the version they read.
- dataset upload (`POST /datasets`, multipart): `object write` then `dataset create` names the stored object as one.
- project transfer, members and audit: an owner's administration, in the UI.
- runners (`GET /runners`, `PATCH`, `DELETE`, commands): the machines page; this machine's own are its command-line-only commands.
- spend total: no total exists on any surface yet; spend is per run (`run list`, `run read`).

<!-- verbs:end -->

## How the gaps closed

Surveyed 2026-09-23 before 000655 and 000656; closed by 000661.

1. **MCP was almost empty**: three tools, then eight. Now every verb of
   every noun, as six noun tools.
2. **Protocols had no file form**: closed by 000655 (`push`, `export`).
3. **Runs could not be named or found by name**: closed by 000656.
4. **The CLI stopped at one protocol**: protocol list, read, versions,
   update; run read, rerun and the rest of the run noun; `run` launches
   again (its keyword-only `bench.launch` call was fixed on this branch).
5. **Articles, datasets and projects were API-only**: each has list,
   read, create, update, delete and history on all three. The API gained
   reads by id (`GET /articles/:id`, `/datasets/:id`, `/projects/:id`),
   an owner's kind read from the handle, and an owner's own projects by
   default.
6. **Discovery had no search or paging** on most listings: `search`,
   `limit`, `offset` and `X-Next-Offset` on objects, protocols, runs,
   articles, datasets and projects; `kind` on objects; `status` on runs;
   `project` on protocols; `GET /protocols/:id/versions`.
7. **Spend** has no total anywhere yet; it is per run. Listed above as
   API-only with its reason, and not built here.
