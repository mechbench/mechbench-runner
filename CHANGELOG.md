# mechbench-runner — changelog

Every release carries two lists, and an empty one says `_None._`
rather than being omitted: "there were none" and "nobody thought about
it" must not look the same.

```markdown
## <version> — <date>

### Changes that raise

### Changes that alter results without raising
```

The convention and the reasoning are in
[`mechbench/docs/RELEASE_NOTES.md`](https://github.com/mechbench/mechbench/blob/main/docs/RELEASE_NOTES.md).
The short version: a change that raises costs a reader ten minutes; a
change that alters results without raising can cost them a finding,
and it is invisible exactly when it matters.

The release gate blocks until the version being released has an entry
with both headings.

---

## Unreleased

### Changes that raise

_None._

### Changes that alter results without raising

- **A runner claims `local` jobs, which were `mlx-local`, and only where
  compute has a backend for them** (installed, and offered on this
  machine's accelerator), in what it advertises and on every claim; a
  machine without one claims `pure` and `remote` alone. Before, every
  runner claimed `mlx-local` and failed the job where MLX was missing.
  It advertises its backends beside them, and the API runs a `local` job
  that names no backend on `mlx` (mechbench-models 0.103.32), so on
  Apple silicon with MLX the jobs it takes are unchanged.
- **The accelerator a runner advertises is the one compute detects**
  (`backends.detect_accelerator()`: `metal`, `cuda`, `rocm`, `tpu` or
  `cpu`); a Mac's is `metal`, which was `applegpu`. Against a compute
  without detection it reads the hardware as before, under the new
  names. An API before mechbench-models 0.103.32 refuses `metal` and
  `local`: release this runner after the API that reads them.

### Other

- `mechbench doctor` names every compute backend: the ones here with
  their versions and the machine's accelerator, and each absent one with
  compute's reason ("mlx absent: it runs on metal, and this machine's
  accelerator is cuda"). Against a compute without
  `backends.detect_accelerator` and `backends.describe` it says what it
  did before, with the accelerator unknown.
- `mechbench run PROTOCOL --backend torch --accelerator cuda` (and
  `run launch`/`run sweep` with `backend` and `accelerator`) names the
  backend and the accelerator a run needs: they go into its job's
  requirements, so only a runner that has them claims it, and a job no
  runner can take says why ("laptop: it needs the torch backend, and
  this runner has mlx"). The stored runs history records them.

## 0.57.0 — 2026-10-01

### Changes that raise

- **A live run's runner claims nothing within 30 s of a try or event,
  and otherwise only `pure` jobs, and only while its model is warm**
  (`holding` now means warm). A live run released for idleness
  restricts nothing: the runner claims any job its policy serves, where
  before it claimed only `pure` jobs for as long as the live run stayed
  open. Extension upkeep and restarts still wait while any live run is
  attached.
- **A lease the runner's own policy refuses is given back**: before
  loading the model the runner checks its copy of the policy (`live`,
  and a non-empty `serve`; refreshed once on a disagreement), and when
  it still says no it reports `refused` with the reason and calls
  `POST /live-runs/:id/release`.

### Changes that alter results without raising

_None._

### Other

- **An open live run and `try`.** `LiveHost` attaches an open live run
  (`form: "open"`), loads its model and warms it with one throwaway
  forward pass (`logits/read`) before saying `ready`. A `try` event runs
  through compute's `run_try` on the warm model: `~scratch/<this
  id>/t<seq>` inputs come from an in-memory cache of the last 16 results
  (others go to the Resolver), tokens stream over the `stream` frame, a
  result over the inline limit (the attach's `limits.inlineBytes`, 256
  KB by default) is written to `~scratch/<id>/t<seq>` with provenance
  `inputs: []` and `produced_by` `mechbench-try`, and the step completes
  with the answer's fields (`refused` when compute refused it).
- **New verbs: `live list`, `live read`, `live start --model …
  [--idle 15m] [--close-after 1d] [--label …]`, `live try <op> [--in
  port=path|json] [--set k=v] [--slot …] [--wait …] [--json]` and `live
  close`**; `mechbench try …` is `live try …`. `start` prints the id
  and makes it this machine's current live run (`~/.mechbench/live.json`),
  which `try` and `close` use when `--live-run` is not given. `try`
  prints the try's lines and where its result is; `--json` prints the
  whole answer. The registry matches mechbench-models 0.103.22.
- The policy twin has `live` (`policy_holds_live`, `policy_live`) and
  the shared cases' `live` rule; `migrate_policy` gives an old body
  `live: own`.

---

## 0.56.0 — 2026-09-30

### Changes that raise

- **New verbs: `observability traces <id>` and `observability errors`**
  (docs/ANALYTICS.md §11.3; site admins only, as the API answers anyone
  else 404). `traces` reads `GET /admin/observability/traces/:id` and
  prints the transaction (method, route, status, duration, outcome; then
  origin, type, time, user, actor kind and surface, key, instance,
  release), the spans as an indented tree (name, type/subtype, duration,
  outcome), the errors, and the links (visit, visitor, user, entities).
  `errors [--since] [--search] [--limit] [--offset]` lists
  `GET /admin/observability/errors` as error groups (fingerprint, type,
  code, count, lastSeenAt, lastRoute, lastTraceId), paged by
  `X-Next-Offset`. The registry matches mechbench-models 0.103.0.

### Changes that alter results without raising

_None._

## 0.55.0 — 2026-09-30

### Changes that raise

- **The `case` verbs replace `support …`, with no alias** (docs/CASES.md;
  000985). `case list [--kind support|submission] [--status
  open|waiting|answered|closed|all] [--assignee <handle>|me] [--search]
  [--limit] [--offset]` reads `GET /cases`; `case read <id>` prints the
  case and its timeline, internal notes marked `## internal note` and
  events between the messages; `case reply <id> --body` is an external
  message and `case note <id> --body` an internal one (both `POST
  /cases/:id/messages` with `visibility`); `case assign <id> [--to
  <handle>|me|none]` (`POST /cases/:id/assign`; no `--to` is you, a
  handle is looked up with `GET /admin/users`, `none` takes everyone
  off); `case close <id> [--outcome]` (`POST /cases/:id/close`).
  `support list/read/open/reply/close` are gone: a support case opens by
  mail or in the app.
- **`extension submit <addr@n>`** asks the platform to verify a checked
  version (`POST …/submit`), opening a submission case.
- **`extension fetch <addr@n> --to DIR`** reads the version, takes its
  sdist's `~hash/sha256:…` from the package, downloads it through
  `GET /objects/~hash/…`, refuses it unless its sha256 is that hash
  (nothing is written then), and unpacks it into DIR, which must be
  empty or absent, refusing the whole sdist when any member is a link or
  a device or its path is absolute or leaves DIR. It prints the hash and
  the files written. Nothing in it is run.
- **`extension flag <addr@n> --code --severity notice|warning|blocker
  --summary [--where path:line] [--details]`** raises a flag with source
  `review` (`POST …/flags`); a `--where` that is not `path:line` or
  `path:line-line` is refused before any call.
- **`extension reject <addr@n> --reason`** rejects a submitted version
  (`POST …/reject`).

### Changes that alter results without raising

- `extension read` adds `inReview` ("in review since <date> (case <id>,
  <status>)") when the version has an open submission case.
- `extension review`'s help says what the reviewer is now: it asks the
  platform's reviewer agent for a report, when one is configured, and a
  person decides. `extension verify`'s says a person at the platform
  read the code and verified it, and that the submission case closes
  `verified`.
- The `mechbench-verified` policy option's description reads models
  0.102.0's words: "a person at the platform read the code and verified
  it".

---

## 0.54.0 — 2026-09-30

### Changes that raise

- **A policy has two axes, `serve` and `admit`, and the runner checks
  both before acting on a claim** (task 000977; CAPABILITY.md §5).
  `policy.py` holds `policy_serves` (whose protocols the machine runs:
  `own`, `org`, `members`, `listed`) and `policy_admits` (whose
  extensions it installs: `own`, `org`, `approved`, `verified`,
  `listed`), the twins of mechbench-models 0.99.0, and the vendored
  `policy_cases.json` is the models copy (85 cases over both rules,
  each reason word for word). Before acting on a claim the runner
  evaluates the job against `serve` and every install against `admit`
  from its own copy of the policy; a disagreement releases the claim
  with `POLICY_MISMATCH` and the rule's words, then re-fetches the
  policy and who owns the machine. A job the policy does not serve is
  released with "This runner does not serve it: …". A runner holding
  no policy takes no job. The claim is read by its new names: on the
  job `creatorId`, `projectId`, `projectOwner {kind, id}` and
  `creatorOrgIds`; on each install item `projectOwner`, `state`,
  `party`, `needs` and `approvedBy`. The runner learns its own owner
  and orgs from `GET /runners/me` (`owner` and `orgIds` when the API
  names them; otherwise an org-scoped runner is its org's and any
  other its account's, with no orgs), or from a `runner {owner,
  orgIds}` the claim carries. It needs an API that sends these names.
- **`mechbench policy create` and `update` take the policy by its
  options.** `--serve` and `--admit` take an option from the Runners
  page (`me only`, `me and my org`, `the org`, `the org and its
  members' own work`, `nobody (paused)`; `mine`, `mine and my org's`,
  `mine and my org's approved`, `mechbench-verified`, `nothing
  (locked)`) or sources joined by commas; `--serve-allow` and
  `--admit-allow` set the `listed` entries (`owner=…,org=…,project=…`
  and `owner=…,org=…,extension=…`, repeated); `--network`,
  `--upgrades` and `--unused-days` set the rest. `create` starts from
  the machine's default (a person's `serve: [own]`, `admit: [own]`; an
  org's, with `--org-id`, `admit: [own, approved]`) and `update` from
  the current version; `--body` is optional and must be the new shape.
  A body naming `install`, `require_approved` or `pools` is refused.
  `policy read` shows each axis as its option or Custom with the
  sources in words; `policy list` has `serve` and `admit` columns.
- **`mechbench extension verify` is renamed `extension check`** (it
  queues the checking job, `POST …/check`), and `extension push`
  without `--draft` calls `…/check` and answers `check` instead of
  `verify`. `extension verify` is now the site admin's approval for
  the platform (`POST …/verify`, `--override` for a blocker flag).
- **New verbs for an org's approval**: `extension approve <address@n>
  --org <handle> [--note] [--override]`, `extension revoke
  <address@n> --org <handle>`, and `extension review <address@n>
  [--org <handle>]` (with `--org`, the reviewer runs on the org's
  credential and budget). The org is named by its handle and sent by
  its id. `extension list` has an `approved` column and `extension
  read` answers `approvals` and `approved`.
- The mirrored policy (`~/.mechbench/policy.json`) in the shape before
  `serve` and `admit` is read through the models migration: `mine` →
  `admit: [own]`, `verified` → `[own, verified]`, `allowlist` →
  `[own, listed]`, `locked` → `[]`, `serve: [own]`; `require_approved`
  and `pools` are dropped.

### Changes that alter results without raising

_None._

## 0.53.0 — 2026-09-30

### Changes that raise

- **The install policy's twin follows the extension trust model**
  (task 000961, security review finding H2). A version the
  verification job passed is now `checked`; `verified` means a person
  and the platform's reviewer read the code. Under `mine` a runner
  installs its owner's own extensions in any state and the platform's
  own (`party: first`) verified ones, and no other author's; under
  `verified`, verified versions only; under `allowlist`, checked or
  verified versions an entry matches, a draft only when an entry names
  its owner. A checked version refused on another person's runner says
  "is checked, not verified: it runs only on its author's own
  runners." The vendored `policy_cases.json` is the models copy.
- **A job's provider credentials are fetched after the claim, held in
  memory, and dropped when the job ends** (task 000962, security review
  H3). The claim names the providers the job's graph uses; the runner
  asks `POST /jobs/:id/credentials` for exactly those, holds them in
  one in-memory holder for the job, and empties it in place when the
  job finishes, fails or is released, on SIGTERM while idle, and at
  exit. Nothing is written under `~/.mechbench` (the spool, held node
  results and checkpoints included). A runner restarted mid-job asks
  again after it re-claims. A provider the owner holds no credential
  for is named in the log, and the nodes that call it fail as before.
  Against an API that still puts `integrations` in the claim, those
  are taken out of the claim into the holder.
- **The runner's key acts only as its runner, and the verbs call with
  a second key.** `mechbench login` stores two keys in
  `~/.mechbench/config.toml`: `api_key`, the runner's own, which the
  service claims with, and `cli_key`, a user-scoped key named
  `cli on <runner name>` that enrollment mints for the person who
  enrolled the machine, which the verbs (`mechbench protocol list`,
  `mechbench support list`, `run <protocol>`, `runs`, `cancel` and the
  rest) call with. `MECHBENCH_API_KEY` still overrides both. A machine
  enrolled before this release holds only the runner key: its first
  verb asks the API for its CLI key with the runner key
  (`POST /runners/me/cli-key`, once per runner), stores it, and says so
  in one line. Should that key have been issued already and not be
  stored, the verb says to run `mechbench login`. A runner claiming with a hand-minted user key logs
  the API's deprecation line once; `mechbench login` registers the
  machine with a runner key, and warns when `MECHBENCH_API_KEY` is set
  and would take precedence over it.

- **`extension list --state` takes `checked`**, and `extension push`
  and `extension verify` say they queue the version's checks, which make
  it `checked`; `verified` takes a person's review.

- **Runners upgrade only from the signed release manifest** (task
  000965, security review finding H6). The hourly upgrade under
  `upgrades.compute: auto`, the `update` command from the site, and
  `mechbench update` fetch `GET /releases/manifest` from the runner's
  API, verify its Ed25519 signature against the release key shipped in
  the package (`mechbench_runner/release_key.pub`), download the two
  wheels and check their sha256, and install with `uv pip install
  --require-hashes` (pip when uv is absent) from requirements that pin
  every dependency to its hashes. PyPI's newest is never read. A
  missing, unsigned or tampered manifest, a wheel whose hash differs, or
  a manifest older than what is installed without `allow_downgrade:
  true` installs nothing and says why in one log line. Until a manifest
  is published, runners do not upgrade themselves.
- **The `update` command's `args.version` must be a strict version or
  absent**, and is carried out only when the current manifest names that
  same runner version.
- **`scripts/release.py` takes `--check` (the gate, no upload) or
  `--upload` (the gate, then twine)**; with neither it prints its usage.
  `--dry-run` is gone. The gate refuses a build whose
  `release_key.pub` holds no key.

### Changes that alter results without raising

- **A failed self-check after an upgrade restores the previous
  hash-locked install** (the requirements recorded under
  `~/.mechbench/release/`); a runner with none recorded reinstalls its
  earlier versions by pin, as before.
- **`cryptography` is a new dependency**, for the signature check.

## 0.52.0 — 2026-09-30

### Changes that raise

- **A job id or protocol node id outside `^[A-Za-z0-9_-]{1,64}$` is
  refused at claim** (task 000963, security review finding H4). The
  runner fails the job with the offending id in the message before it
  installs, spools or runs anything. The models schema still takes any
  1–64 characters for a node id, so a protocol with a node id such as
  `gen.1` or `a b` validates on the API and then fails on every runner
  from this version. None of mechbench-experiments' protocols has one.
- **A verification job refuses an example input whose object path
  resolves outside its scratch inputs directory**, before fetching it.
- **An extension package reference whose sha256 is not 64 lowercase hex
  digits is refused** (it used to be refused only after the fetch, when
  the hash did not match).

### Changes that alter results without raising

_None._ The spool's layout changes; what it holds, and what a resumed
run reuses, does not.

### Other

- **Spool directories are named by a hash of the id, never by the id**
  (task 000963). A job's spool is `~/.mechbench/spool/<h(job id)>/` and
  a node's is `<job>/<h(node id)>/`, where `h` is the first 32 hex
  digits of sha256. Each directory holds a `.mechbench-owned` marker
  naming its id, written when the runner creates it; the resume map and
  the spooled-job list read ids from the markers.
- **One helper, `mechbench_runner/confine.py`, derives every path the
  runner builds from a job id, node id, object path or package digest**:
  it checks the id, resolves the path, and refuses (`PathRefusedError`,
  naming the id) anything that is not strictly under its root, including
  through a symlink. It deletes only a directory carrying the marker,
  never follows a symlink when deleting, and never claims an existing
  directory it did not create. The verification scratch directory is
  named and cleaned the same way.
- **A result spooled by 0.51.0 or earlier is adopted**: a directory
  named by a valid job id holding `result.cbor` is moved to its hashed
  name and marked the first time the runner lists the spool, so an
  undelivered result survives the upgrade. Partial node spools from
  those versions are left where they are and not read; a job resumed
  across the upgrade recomputes those nodes.

## 0.51.0 — 2026-09-30

### Changes that raise

- **The `support` verbs need the API's support routes** (task 000923:
  `GET/POST /support/cases`, `GET/PATCH /support/cases/:id`,
  `POST /support/cases/:id/messages`) and, over MCP, the API's
  executors for them. Against an API without them every `support` verb
  answers a 404.
- **`--runner` needs an API that reads `runner` on a run and a sweep**
  (mechbench-models 0.89.0's `CreateRunRequestSchema` and
  `CreateSweepRequestSchema`) and carries it into the job's
  requirements. An API before it refuses a pinned sweep with a 400, and
  takes a pinned run as an unpinned one: any runner may claim it.

### Changes that alter results without raising

_None._ The after-canary moves later and `canary_after_delay_ms` joins
`ambient`, which is timing reported beside a run, never inside its
result or provenance. `quiet` after a model-bearing node reads the
machine rather than the node's own release, so fewer nodes read
`quiet: false`.

### Other

- **`support`** (task 000928), a noun in the verb registry, so the
  command line, MCP and the API expose the same verbs: `support list
  {status?, search?, limit?, offset?}` (`GET /support/cases`; prints id,
  status, subject, requester, age of the last inbound message, plan),
  `support read {id}` (`GET /support/cases/:id`; the command line prints
  every message and event in order with who, when and which way it came,
  and the attachments), `support open {subject, body}` (outward),
  `support reply {id, body}` (outward: on a case with a mail thread it
  goes out as mail in that thread), `support close {id}` (`PATCH` with
  `status: closed`). Every write sends `via: "cli"`. A `body` of `-` is
  read from stdin. `create`, `update`, `delete` and `history` are absent
  with their reasons.
- **A run names its runner** (task 000933). `mechbench run PROTOCOL
  --runner ID|NAME`, `run launch --runner` and `run sweep --runner` (or
  `runner` in the sweep file) send `runner` in the request; placement
  hands the job to that runner alone. A pinned launch posts the run
  itself rather than through compute's `bench.launch`, and the runs
  history records the runner.
- **`runner list`** (`mechbench runners`): `GET /runners` with the id a
  run pins, name, hostname, whether connected, phase, version, last
  seen; signed-out runners with `--signed-out`. `mechbench status`
  prints this machine's runner id and name (`runner_id`,
  `runner_name` in `status --json`).
- **The canary after a model-bearing node waits for the node's memory
  to settle** (task 000934). Before the after-canary the runner runs
  `gc.collect()`, `mx.synchronize()` and `mx.clear_cache()`, then reads
  MLX's active plus cache memory every 25 ms until it stops falling,
  bounded at 1 s; without MLX it measures at once. The wait is
  `canary_after_delay_ms` in `ambient`. 030's `lora-s20` read 0.02 of
  the baseline because the canary ran while the training step's buffers
  were being freed.

## 0.50.0 — 2026-09-30

### Changes that raise

- **The LaunchAgent's plist gains `LimitLoadToSessionType: [Aqua,
  Background]`**, and `install-service` bootstraps it into `gui/<uid>`
  when a login session exists and into `user/<uid>` when there is none
  (SSH on a machine nobody has logged in to); `bootout`, `status`,
  `kickstart` and `restart` find the domain it is loaded in. A plist
  written by an older runner keeps working in `gui/`; re-run
  `mechbench install-service` to take the new one.
- **The API must accept `span` on `PATCH /jobs/:id/progress`**
  (mechbench-api a32cbd6). An API before it refuses the report with a
  400; the runner logs "span report failed" and carries on.

### Changes that alter results without raising

_None._ Spans, the canary and the counters are timing, reported beside
a run and never inside its result or provenance.

### Other

- **Runner identity** (task 000876). `advertise()`, sent at
  registration, in `hello` and on every claim, adds `chip`
  (`sysctl machdep.cpu.brand_string`), `gpu_cores` (`ioreg`
  `gpu-core-count`, else `system_profiler`), `os` (`macOS 27.0
  (26A428)`), `python`, `stack` (`mlx`, `mlx_lm`, `mlx_vlm`, `torch`
  from `importlib.metadata`, null when absent), `backends` (compute's
  `backends.available()`), `architectures` and `architecture_levels`
  (compute's `local_architectures()`), asked once per process
  (`mechbench_runner.identity`). mechbench-models 0.83.0 names them and
  places on `architecture` and `backend`.
- **`mechbench calibrate`** (`runner calibrate`, task 000887), a local
  verb of the new `runner` noun. It times the two micro-benchmarks
  (`memcopy`: a 256 MiB float32 read and write; `matmul`: 4096³ in
  bf16), then for a model (default Gemma 4 E2B when it is cached):
  `load` cold (the weights evicted from the page cache with
  `msync(MS_INVALIDATE)`, residency checked with `mincore`) and warm,
  `forward` at n=128, b=1, `capture` at k=4 layers (interleaved with the
  forwards, the added seconds), and one LoRA `lora_step` (rank 8, q and
  v). Each is a median over `--repeats` (default 10; loads 3) after an
  untimed warm-up, with the interquartile range in seconds and MLX's peak
  memory. It answers a `platform/calibration` collection as compute
  0.171.0 declares it (`records/record` on an older compute): items
  `{id, chip, stack, model, dtype, primitive, shape, shape_key, seconds,
  bytes_per_second?, peak_memory_bytes, repeats, spread,
  warmup_seconds}`, header `{machine, stack_components, taken_at,
  quiet, ambient}`; `stack` is `sha256:` over the sorted stack
  components. `--out FILE` writes it, `--push --into OWNER/PROJECT`
  stores it at `<owner>/<project>/calibration/<chip>-<stack12>`. The
  micro-benchmarks are kept as `~/.mechbench/calibration/baseline.json`.
- **The canary and the counters** (task 000889). Before and after every
  model-bearing node (its requirements' class is `mlx-local`) the runner
  times both micro-benchmarks for about 300 ms and records each as a
  ratio to the baseline; a thread samples, once a second, the thermal
  state (`NSProcessInfo` when PyObjC is there, else `pmset -g therm`),
  GPU utilization (`ioreg`), memory (`memory_pressure`, `sysctl
  vm.swapusage`, `vm_stat`), the load average, the top three processes
  and the deny-list (`mds_stores`, `backupd`, `photoanalysisd`,
  `mediaanalysisd` busy, or anything over 4 GB resident and busy), and
  power (`pmset -g batt`, low power mode). The span gets `ambient`
  (`canary_before`, `canary_after`, `canary`, `samples`, `thermal`,
  `gpu_utilization`, `memory`, `load`, `top`, `busy`, `power`,
  `reasons`) and `quiet` (canaries at or above 0.85, thermal nominal,
  nothing on the deny-list busy, low power off). The runner takes
  compute's span through `on_node_span` (compute 0.171.0) and sends it
  whole, ambient added, as `span` on a progress report; with an older
  compute it sends the ambient alone when the node is done.
- **macOS service** (task 000912): checked on macOS 27.0 (26A428). The
  plist lints; under `ProcessType Standard` launchd spawns the agent as
  `daemon (3)` and a CPU loop runs as fast as in a terminal, under
  `Background` 3.4× slower. The README gives the steps for a fresh
  dedicated machine.

## 0.49.0 — 2026-09-29

### Changes that raise

- **`VERB_REGISTRY`'s `method` and `route` may be null** (models'
  `verbs.generated.ts`): `extension new` and `extension test` run on the
  caller's machine and name no API route. A consumer that reads either
  field as a string handles the null.
- **The API has no executor for the new verbs yet.** Its
  `executorGaps()` test lists every `op`, `extension` and `policy` verb
  as missing once it takes this registry; until they are written, a
  thread's agent or an MCP client calling one is answered that there is
  no such verb.

### Changes that alter results without raising

- **Three nouns: `op`, `extension` and `policy`**, on the command line
  and in the registry MCP and threads read (task 000415, 000815).
  - `op list [--reads K] [--emits K] [--owner H] [--search Q]` reads
    `GET /ops` (core's in the lexicon's order, then the extensions' you
    would use); `op read <address>` reads `GET /ops/:address`, core's or
    an extension's with its declaration and pin; `op next <kind>` is
    `op list --reads <kind>` with the ports that take it, and given an
    object's path follows its kind. All reads.
  - `extension new <owner>/<project> --name N --op family/leaf`
    scaffolds a package (pyproject.toml with the `mechbench.extensions`
    entry point and a hatchling build, the manifest, one op file with
    `OP` and `run`, an empty `kinds/`, a README) that passes
    `extension test` as written; a leaf that is not a verb is refused
    first. `extension test <dir>` runs
    `python -m mechbench_compute.conformance <module:MANIFEST>` in this
    environment (or `--python`), `--inputs <dir>/inputs` when present,
    `--model` when asked or when `MECHBENCH_WARM_MODEL_ID` is set and an
    op needs a model; it prints the report and exits 1 when it fails.
  - `extension push <dir> [--draft]` (outward, consent) compiles the
    declarations with `Extension.to_dict()`, builds the sdist
    (`uv build --sdist`, else `python -m build --sdist`), stores it as
    raw bytes by hash at
    `<owner>/<project>/extensions/<name>/sdist/<hex16>`, PUTs the
    manifest without the platform's fields and with `package.sdist:
    ~hash/sha256:…`, and answers the address, version, hash, pin, the
    sdist and a consent line saying who will see it; without `--draft`
    it asks for verification and answers the job and `waitingFor`. The
    pin this machine's compute computes is compared with the platform's.
  - `extension verify`, `list`, `read`, `history` (the versions),
    `withdraw` (delete, consent) and `visibility` (outward when it
    widens). There is no install verb: machines install under policy.
  - `policy list`, `read`, `create`; `policy update` and
    `policy apply <runner> <policy>` name the runners they reach and
    change nothing unless `--yes` (outward, consent when yes).
- The CI installs `build`, so the push test builds an sdist without uv.

## 0.48.0 — 2026-09-29

### Changes that raise

_None._

### Changes that alter results without raising

- **The runner runs verification jobs.** A claimed job of kind
  `verification` (queued by `POST /extensions/…@n/verify`, placed only
  on a runner whose policy admits the version) is handled by
  `mechbench_runner/verification.py` and never installs anything into
  the runner's own environment. It re-checks the version against its
  own policy (a refusal releases the job, as an install does), fetches
  the sdist by hash, makes a scratch venv under
  `~/.mechbench/extensions/verify/<job>` with the runner's own
  interpreter (`uv venv --python`), builds the wheel from the sdist
  (`uv build --wheel`), freezes the runner's environment
  (`uv pip freeze`) into a constraints file, installs
  `mechbench-compute==<the runner's compute>` and the wheel into the
  scratch venv under those constraints (`uv pip install --constraint`),
  resolves the lock under the same constraints
  (`uv pip compile --constraint … --generate-hashes
  --no-emit-package <the package>`), so a lock can never move a package
  compute pins, fetches each example's `$ref` bench input, and runs
  `python -m mechbench_compute.conformance <entry point> --inputs <dir>`
  there (`--model` when a warm model is set and an op needs one). The
  wheel and lock are stored as objects under the job's `builds` path,
  and the job completes with `{report}`: compute's report
  (`conformance`, `passed`, `findings`, `examples`), the `wheel` and
  `lock` refs, `model`, and `compute`. Any step that fails completes the
  job with `failure: {stage, message}` (the resolver's message for a
  conflict); the scratch venv is removed either way. An editable
  install of compute or schema (a source checkout) is built into a
  wheelhouse and pinned at the checkout's version.
- `ApiClient.put_bytes` (an octet-stream object write by hash) and
  `ApiClient.complete_verification`.

## 0.47.1 — 2026-09-29

### Changes that raise

_None._

### Changes that alter results without raising

- **Concurrency slots no longer survive a restart.** `SharedLimiter`
  saved every bucket to `~/.mechbench/limits.json`, including the
  concurrency buckets, whose tokens only `release()` returns; slots held
  by calls in flight when the file was written were lost for good after
  a restart, so each restart could only lower a provider's concurrency
  (8 → 1 for three keys by 2026-09-29, which slowed 024's judge from
  2 to 14.7 minutes with no provider pressure). `save()` now leaves
  concurrency buckets out, and `load()` skips any it finds in a file
  from an older runner, so the registry's capacity applies with every
  slot free. Runs that were throttled by lost slots finish faster;
  `throttled_seconds` and `waited_seconds` drop accordingly.
- **The other currencies are saved on `release()` and when the runner
  stops**, not only from `observe()` at most every five seconds, so a
  restarted runner reads `requests` and token buckets as they were when
  it stopped. `release()` saves at the same five-second rate as
  `observe()`; `JobRunner.run()` saves once on every exit, including
  after SIGTERM or SIGINT.

---

## 0.47.0 — 2026-09-29

### Changes that raise

- **The runner's floor on compute is 0.165.0** (the release with the
  extension registry), and it depends on `packaging`. An environment
  holding an older compute is upgraded when the runner is.

### Changes that alter results without raising

- **The runner installs the extensions a claimed job needs.** When a
  claim's `install` list is admitted (by `check_installs`, then by
  `policy_admits` again for each item right before installing), the
  runner (`mechbench_runner/extensions.py`) fetches each item's wheel
  (else its sdist) and lock by hash (`GET /objects/~hash/sha256:<hex>`)
  into `~/.mechbench/extensions/cache/`, refuses any file whose sha256
  differs from its reference, and installs into its own environment:
  `uv pip install --python <this interpreter> --require-hashes -r <req>`
  when there is a lock (`<req>` is the lock plus the wheel as a
  `name @ file://… --hash=sha256:…` line, since uv's hash mode refuses a
  bare wheel path), else `uv pip install --python <this interpreter>
  <wheel>`. It records `{hash, address, version, name, package_name,
  installed_at, by_job, used_at}` under the pin in
  `~/.mechbench/extensions/installed.json` (the file compute's
  `InstalledSource` reads for the digest), then calls compute's
  `REGISTRY.refresh()`, then runs the job. `advertise()` now says
  `installs: true`, with those pins.
- **A failed install releases the job.** A failed download, a hash
  mismatch, a uv error or an extension compute refuses at load releases
  the job with `INSTALL_FAILED` and `install of <address>@<n> failed on
  <runner>: <reason>`, writes the reason, the compute version and the
  policy version to `~/.mechbench/extensions/failed.json`, and is not
  tried again until one of those two changes. An extension that installed
  but did not load is uninstalled.
- **A new version of a loaded extension restarts the runner.** Python
  cannot unload a module, so when `refresh()` raises `RestartRequired`
  the job is interrupted (not released: this runner re-claims it as a
  resume) and the `run` child exits with the restart code the supervisor
  already honours, between jobs, and not while a live run is held.
- **Extensions are collected.** Between jobs, at most once an hour, an
  extension no job has named for `policy.gc.unused_days`, or whose
  version `GET /extensions/<address>@<n>` says is withdrawn, is
  `uv pip uninstall`ed and dropped from `installed.json`.
- **The runner upgrades itself under `upgrades.compute: auto`.** Between
  jobs, at most once an hour, it reads the newest `mechbench` and the
  newest `mechbench-compute` that release allows from PyPI's JSON API,
  and when either is newer than what it runs, installs both pinned
  (`uv pip install --python <this interpreter> --refresh-package …
  mechbench==<v> mechbench-compute==<v>`), checks the new code imports,
  and restarts. A failed install or check restores the previous pair and
  skips that pair from then on. `upgrades: hold` does nothing, and a
  runner running from a source checkout never upgrades itself.

Also:

- `POST /runners/me/installed` is sent after each change; against an API
  without the route (404) the runner stops sending it, and the next
  claim's `X-Runner-Capabilities` carries the same set.

## 0.46.0 — 2026-09-29

### Changes that raise

_None._

### Changes that alter results without raising

- **The runner advertises what it can do, and the API places jobs by
  it.** `advertise()` (`api_client.py`) says `{classes, compute,
  installs, installed, accelerator, memory_gb}`: the classes it claims
  (`mlx-local`, `pure`, `remote`, as before), the compute release it
  runs, `installs: false` (it installs no extension yet), the pin hashes
  in `~/.mechbench/extensions/installed.json` if that file exists, the
  accelerator (`applegpu` when MLX sees an Apple GPU, `cuda` when
  `nvidia-smi` is on the path, else `cpu`) and its physical memory in
  whole GB. It is sent at registration, in the channel's `hello`, and on
  every claim as `X-Runner-Capabilities`. An API that places by it
  (000417, 000422) hands this runner only jobs whose `min_compute`,
  accelerator and memory it meets, and no job that needs an extension it
  does not hold. An API that does not yet read it ignores it.

Also:

- The policy check reads the runner's owner from `GET /runners/me` as the
  API serves it (`account.userId`); it read a top-level `userId`, which
  the API does not send, and would have failed on the first claim that
  carried an `install` list.

## 0.45.1 — 2026-09-29

### Changes that raise

_None._

### Changes that alter results without raising

- **A claim the policy refuses is released, not failed.** `check_installs`
  now calls `POST /jobs/:id/release` with `code: "POLICY_MISMATCH"` and
  the reason (`ApiClient.release_job`). The job goes back to `queued`
  for another runner instead of ending, and the API keeps this runner off
  it until its policy changes version or a day passes. Against an API
  without the route (a 404), the runner fails the job as 0.45.0 did.

Also:

- The policy's `name` (from `GET /runners/me/policy`) is held, mirrored
  and used in log lines: `policy personal (pol_personal) v1 applied`.

## 0.45.0 — 2026-09-29

### Changes that raise

_None._

### Changes that alter results without raising

- **A claim made under another policy version than the runner holds is
  released, not run.** The runner re-fetches its policy when a claim names
  a different `{id, version}`; if the two still disagree, it fails the job
  with `POLICY_MISMATCH: …` and the reason, and runs nothing of it. The
  API has no call that returns a claimed job to the queue, so the release
  is a `fail`.

Also:

- **The runner holds its policy.** It fetches `GET /runners/me/policy` at
  startup, mirrors it to `~/.mechbench/policy.json` so a restart with the
  API unreachable starts from the last known copy, and re-fetches when a
  `policy` frame, a ping or a claim names another version. A change logs
  `policy <id> v<n> applied`.
- `mechbench_runner.policy.policy_admits`: the twin of mechbench-models'
  `policyAdmits`, tested against `policy_cases.json`, vendored verbatim
  from models. `check_installs` re-checks every item of a claim's
  `install` list against the held copy and releases the claim on any
  refusal; no claim carries an `install` list yet.

## 0.44.1 — 2026-09-28

### Changes that raise

_None._

### Changes that alter results without raising

_None._ A live run says it is ready once, after its warm-up step, not
also between loading the model and warming up.

## 0.44.0 — 2026-09-28

### Changes that raise

_None._

### Changes that alter results without raising

_None._

Also:

- **A live run warms up when it is leased**: the runner loads its model,
  resolves its other inputs once (they were fetched again on every step,
  twice for an input wired to two ports), and runs one step on a greeting
  it throws away, so MLX has compiled what the handler generates with
  before the first message arrives.

## 0.43.1 — 2026-09-28

### Changes that raise

_None._

### Changes that alter results without raising

_None._ Needs mechbench-compute 0.162.1, whose per-token readouts read
bfloat16 activations; on 0.162.0 a live chat that colours its reply fails
on most local weights.

## 0.43.0 — 2026-09-28

### Changes that raise

_None._

### Changes that alter results without raising

_None._

Also:

- **Live runs** (epic 000699): a runner holds the live runs its owner
  starts. The API leases one to it over the channel; the runner loads its
  model and keeps it warm, runs each event's step on the main thread,
  streams each token (and its readout against a direction) back as it is
  made, and records the step over HTTP. A `cancel` stops a step; an idle
  live run lets its model go after its `idleSeconds`. While it holds one,
  the runner claims only jobs that need no model. A runner that reconnects
  catches up from `GET /live-runs/leased`. Needs mechbench-compute 0.162.0.

## 0.42.0 — 2026-09-27

### Changes that raise

_None._

### Changes that alter results without raising

_None._

Also:

- **`object url PATH`**: a link to a picture (a PNG object) that needs no
  credential until it expires (`--expires`, 60 to 3600 seconds, default
  900), drawn smaller with `--width`.
- **`--link`** on `object render` and `protocol render`, with
  `--format png`: keeps the picture in the figure's project under
  `renders/` and answers its path and a link, instead of the bytes.
- **A page's link in place of a path or id**: `object render` and
  `protocol render` take the link to the figure's page on mechbench.ai,
  and draw the view it carries (its step, selected block, theme) unless
  the call names its own.
- Needs mechbench-compute 0.148.0.

## 0.41.0 — 2026-09-27

### Changes that raise

_None._

### Changes that alter results without raising

_None._

Also:

- **`object render`** and **`protocol render`**: a chart, token strip or
  protocol diagram as a reader sees it. `--format text` (the default)
  prints a reading in markdown: the axes as drawn, the marks in order,
  and the problems the renderer met (rows it could not draw, colours it
  clipped, labels it could not place). `json` is the same reading as
  data; `svg` and `png` draw the figure (`-o` to write it; a png needs
  one). `--theme light|dark`, `--width`, `--step-by FIELD --step N` for
  a chart, `--selected BLOCK` for a diagram, `--full` for every mark.

## 0.40.1 — 2026-09-27

### Changes that raise

_None._

### Changes that alter results without raising

- **Needs mechbench-compute 0.147.0 or later**, whose table operations
  order numeric groups by value and type their columns from their
  values, and whose tracked answers carry a `rank`.

Also: the package description no longer mentions MCP tools.

## 0.40.0 — 2026-09-27

### Changes that raise

- **`mechbench mcp` is gone**, and with it the local stdio MCP server
  and its `run_protocol` tool. An agent connects to the platform's MCP
  server at `https://api.mechbench.ai/mcp`, which has the same tools, a
  noun each, plus `thread` and `docs`, and signs in with OAuth instead
  of an API key in a config file. An agent with a shell uses
  `mechbench <noun> <verb>`. A recorded run of a built-in kind is
  `run launch`, as it was on every other surface. A client configured
  with `"args": ["mcp"]` fails to start.
- **`mechbench smoke --full` is gone**: it ran `run_protocol`.
  `mechbench smoke` checks the key and reads runs, as before.
- **The `mcp` package is no longer a dependency.**

### Changes that alter results without raising

_None._

## 0.39.0 — 2026-09-26

### Changes that raise

_None._

### Changes that alter results without raising

- **`run result` shortens a long list of numbers**: a list of 32 numbers
  or more comes back as `{first, count, shortened}`, its first 8 and its
  length, wherever it sits in the answer. `--full` answers every value,
  as before. A script that reads a direction's or vector's values from
  `run result` needs `--full`.
- **`protocol push` answers a summary**: the protocol's own fields, its
  graph as `{nodes, edges}` counts and its signature as names. `--full`
  answers the whole protocol, as before.

Also:

- **`run check`** (`POST /protocols/:id/check`): the checks a launch
  makes, and whether each input is stored and of its port's kind, each
  local model is one compute loads, and each activation capture fits
  under compute's cap. Takes launch's arguments; queues and spends
  nothing.
- **`model check`** (`GET /models/check`), a new noun: whether compute
  loads a Hugging Face repo, its size, and its shape (layers, width,
  heads, key/value heads, global layers), read before anything runs.

## 0.38.0 — 2026-09-25

### Changes that raise

_None._

### Changes that alter results without raising

_None._

### Other

- Every verb says what it does to the platform, its `effect`: `read`,
  `draft` (the caller's own things, reversibly), `spend` (compute or
  provider money), `delete` or `outward` (publishes, or widens who
  reads). The last three need the person's consent, on every call or
  only for some arguments (`consent_when`): a delete only with `yes`,
  an update only when it widens visibility, an article update also
  when it publishes. `run launch`, `run sweep`, `run rerun`,
  `protocol publish` and `protocol unpublish` always do. MCP marks
  those verbs `[consent: …]` in each tool's description, and
  docs/CAPABILITIES.md has an Effect column and a legend. The
  platform's own agent proposes such a call as a card the person
  clicks and never makes it.
- An argument naming a file on the caller's machine is marked `local`;
  a caller without a disk (the platform's agent) gives the thing
  itself: `protocol push` takes `protocol` (its JSON) as well as a file,
  `protocol edit` takes `description`, and `article create`, `update`
  and `edit` take `body`. `run diff` is marked as computing on the
  caller's machine.
- A new noun, `thread` (CLI `mechbench thread`, MCP `thread(verb, args)`):
  `list`, `read`, `create`, `update`, `fork`, `delete` (a dry run
  unless `yes`) and `history`, over `GET/POST /threads`,
  `PATCH/DELETE /threads/:id` and `POST /threads/:id/fork`.
- `scripts/dump_verbs_ts.py` writes the registry (nouns, verbs, their
  arguments, routes and effects) as
  `mechbench-models/src/verbs.generated.ts`, which the platform's agent
  reads its tools from; `tests/test_verbs_ts.py` fails when that copy
  is stale.

## 0.37.0 — 2026-09-25

### Changes that raise

_None._

### Changes that alter results without raising

_None._

### Other

- `run sweep` (CLI `mechbench run sweep PROTOCOL`, MCP `run(verb="sweep")`)
  launches one run per binding in one call, `POST /protocols/:id/sweeps`:
  members from a JSON or YAML `--file` (a list of `{params, inputs}`, or a
  whole sweep) or `--members` JSON, or a grid of values with
  `--grid NAME=V1,V2` and `--grid-input NAME=PATH1,PATH2`, every
  combination run with the first name varying slowest. `--param`,
  `--input`, `--label`, `--keep` and `--budget` apply to every run.
  `--wait` polls the sweep until every run has finished (or `--timeout`,
  default an hour) and answers their summaries with `finished`. The API
  refuses the whole sweep when one member cannot run, naming it, and
  refuses more than 200 runs.
- `run list` and `run jobs` take `--sweep` (MCP `sweep`): one sweep's runs,
  in the order it launched them, and its jobs.

## 0.36.0 — 2026-09-24

### Changes that raise

_None._

### Changes that alter results without raising

_None._

### Other

- `run jobs` (CLI `mechbench run jobs`, MCP `run(verb="jobs")`) lists jobs
  from `GET /jobs`, newest first, paged by `limit` and `offset` with the
  next offset in `next`, and narrowed by `status`, `protocol`, `search`
  (the run's label or the protocol's name) and `owner`. `order=oldest`
  walks them oldest first, which stays exact while new jobs are queued.
  Needs an API that pages `GET /jobs`; an older one ignores the query
  and answers its newest 100 with no `next`.
- The parity gate requires every listing verb to take `search`, `limit`
  and `offset`.

## 0.35.0 — 2026-09-24

Needs mechbench-compute 0.135.0 and mechbench-schema 0.17.0.

### Changes that raise

_None._

### Changes that alter results without raising

_None_ in the runner itself. Compute 0.135.0, which this release requires,
changes Gemma 4 direct logit attribution with `apply_ln` and every probe of
attention internals on Gemma 3, Qwen2 and Llama (see its changelog).

### Other

- **Comments and docstrings erased.** Only directives remain, plus one
  MCP tool description the server reads. A gate in the suite keeps it so,
  and tests now hold the outside facts the comments carried: TLS roots,
  websocket keepalive, launchd and systemd behaviour, the claim token after
  an interrupt or a re-claim, upload limits, file permissions, exit codes.
  `docs/CAPABILITIES.md` and the README no longer name internal tasks; the
  README states the exit-code contract.

## 0.34.0 — 2026-09-23

Needs mechbench-compute 0.133.0 and the mechbench-api deployed with it.

### Changes that raise

_None._

### Changes that alter results without raising

_None._

### Other

- **`run diff A B --node gen`**, on the command line and as
  `run(verb="diff")` over MCP: two runs' node, or two object paths,
  compared record by record by mechbench-compute's `records/diff`, the
  function a protocol node runs. `--key prompt,sample` matches records
  by coordinates; `--fields`, `--exclude`, `--include-moving`,
  `--allow` (the expected differences, JSON), `--by`, `--limit` and
  `--full` are the operation's parameters and the answer's size. Both
  results are read where the command runs, with their provenance, which
  is compared too; nothing is computed in the API.
- **`object items` reads projected items (000660):** `--fields`
  (dot paths), `--where PATH OP VALUE` (repeated for AND), `--sort`,
  `--order`, `--lines` and `--chars` (trim every string), `--count` and
  `--header`, as JSON lines or `--table`; the same over MCP as
  `object(verb="items", args={…})`. The server does the work, so reading
  200 story titles costs about 3k tokens, not 99k.

## 0.33.0 — 2026-09-23

Needs mechbench-compute 0.132.0 (`bench.push_protocol`,
`export_protocol`, `runs`, `label_run`) and the mechbench-api deployed
with it: `POST /protocols/push`, `/runs`, the discovery routes (`view=
summary`, `search`, paging, reads by id, `/protocols/:id/versions`,
`/objects/~meta`), version restore, and `?format=markdown` reads with
`PUT` writes at a base version.

### Changes that raise

- The MCP tools `get_result` and `list_jobs` are gone, and the tools
  are one per noun: `object`, `protocol`, `run`, `article`, `dataset`,
  `project`, each `(verb, args)`. `get_result(path)` is
  `object(verb="read", args={"path": …, "full": true})`; `list_jobs()` is
  `run(verb="list")`. `run_protocol` is unchanged. An agent config that
  names the old tools gets "unknown tool".
- `mechbench protocol copy` also takes `ID --version N`; `ID@N` still works.
- `mechbench run --bind` is refused, naming `--param` and `--input`. It
  passed the legacy binding to `bench.launch`, which has not taken one
  since compute 650a68f, so every `mechbench run <protocol>` raised
  `TypeError`; the refusal says why instead.

### Changes that alter results without raising

- _None._

### Other

- `mechbench protocol push FILE --into OWNER/PROJECT [--org]` and
  `mechbench protocol export PROTOCOL [--version N] [-o FILE]`: protocols
  as files, created by name, versioned on change, unchanged when the
  file is the head (epic 000654, task 000655).
- `mechbench run PROTOCOL --label TEXT`, `mechbench runs [--label TEXT |
  --label-contains TEXT] [--protocol ID] [--project OWNER/PROJECT]
  [--owner HANDLE] [--limit N] [--json]`, and `mechbench label RUN TEXT |
  --clear` (task 000656).
- Every noun's verbs on the command line and over MCP (task 000661),
  from one registry (`mechbench_runner/verbs/`): `mechbench <noun> <verb>`
  and `<noun>(verb, args)` for object (list, read, items, write, update,
  delete, history), protocol (list, read, versions, push, export, update,
  publish, unpublish, copy, delete, history), run (list, read, launch,
  update, watch, result, cancel, rerun, delete, history), and article,
  dataset and project (list, read, create, update, delete, history).
  Reads are summaries unless `--full`; listings take `--search`,
  `--limit`, `--offset` and answer `{items, next}`; `delete` is a dry run
  unless `--yes`. `mechbench run <verb>` is the run noun, and `mechbench
  run PROTOCOL` still launches.
- `tests/test_parity.py`: fails when a command or MCP tool is on one
  surface without a recorded reason, a noun lacks a lifecycle verb
  without one, a verb's API route is not in mechbench-api, or
  docs/CAPABILITIES.md is stale (`scripts/capabilities.py` writes it).
- A claim sends `X-Compute-Version`, so a runs listing says which compute
  version ran each job.
- docs/CAPABILITIES.md: the capability matrix, generated (task 000661).
- `mechbench protocol restore ID --version N` and `mechbench article
  versions` / `article restore`, and the same over MCP: an earlier
  version becomes the head again as a new version; nothing is restored
  after a delete (task 000663).
- Articles and protocols read and write as markdown: `article read
  --format markdown`, `protocol read --format markdown`, `article edit`,
  `protocol edit`, and `article update --body-file x.md --base-version N`.
  A write is diffed at its base version and stored as a minimal op, so a
  collaborator's edits since are kept (tasks 000528, 000529).
- A result whose generated items did not all end naturally says so:
  `mechbench result` on stderr, and `run result` and `object read` put an
  `ended_notice` list first (task 000657).

## 0.32.0 — 2026-09-18

### Changes that raise

- _None._

### Changes that alter results without raising

- _None._

### Other

- `mechbench run <protocol> --param n=12 --input prompts=<path> --keep
  outputs` (epic 000553, task 000563): a run binds the protocol's
  declared params and inputs by name; `--param` reads a value that
  parses as JSON as that value and any other as text. `--bind` stays as
  the legacy spelling. The run's record in `~/.mechbench/runs.jsonl`
  carries `params`, `inputs` and `keep`.
- Needs mechbench-compute 0.103.0.

## 0.31.0 — 2026-09-18

### Changes that raise

- _None._

### Changes that alter results without raising

- _None._ See mechbench-compute 0.102.0: every run manifest gains
  `node_hashes` and `node_inputs`. No result's bytes change.

### Other

- Needs mechbench-compute 0.102.0 (epic 000553, task 000561): a run with
  `keep: "outputs"` holds its intermediates on this machine instead of
  emitting them. The spool keeps each held result under
  `<job>/<node>/held.cbor` beside the node's fingerprint, offers it to a
  resume as `held`, and drops it with the node's partials when the
  fingerprint changes. A held result that cannot be written is said in
  the log; the run continues from memory.

## 0.30.0 — 2026-09-18

### Changes that raise

- _None._

### Changes that alter results without raising

- _None._ See mechbench-compute 0.101.0: a node's lineage inputs now
  name the stored objects it read by reference. No result's bytes change.

### Other

- Needs mechbench-compute 0.101.0 (epic 000553): the executor reads the
  declared dataflow form (`{"$param"}`, `{"$ref"}`, protocol inputs as
  edge sources, declared outputs stored by name). It also carries
  compute 0.100.0's `text/measure` mode `items` and the release gate that
  no longer loads a model.

## 0.29.0 — 2026-09-17

### Changes that raise

- _None._

### Changes that alter results without raising

- _None._

### Other

- Needs mechbench-compute 0.99.0 (task 000549): `text/generate`
  `continue_prefill` and `stop`, and `metadata.sampling.ended` on every
  generated item.

## 0.28.0 — 2026-09-17

### Changes that raise

- _None_ here. mechbench-compute 0.98.0, which this version requires,
  refuses an `adapter/train` `batch` kind the target's shape does not
  build, which it used to skip. See its notes.

### Changes that alter results without raising

- _None._

### Other

- Needs mechbench-compute 0.98.0 (task 000548). Everything new is in
  compute: outcomes of many tokens trained whole (`batch.path`), item
  slots drawn without replacement (`target.unit`, `target.replace`),
  exact complete-outcome reads (`logits/read` `complete`), `eval/expect`
  `absent`, and `text/measure` `list`. A runner below this version runs
  an older compute that refuses those params by name.

## 0.27.0 — 2026-09-17

### Changes that raise

- _None._

### Changes that alter results without raising

- _None._

### Other

- **Publishing, copying, deleting and histories from the command line.**
  - `mechbench protocol publish <prt_…> [--version N]` publishes a
    version (the head by default) and prints its public page.
  - `protocol unpublish <prt_…> --version N` withdraws one and names the
    articles citing it.
  - `protocol copy <prt_…>@N --into owner/project [--name] [--org]
    [--dry-run]` copies a version, sub-protocols and all.
  - `mechbench delete <path | prt_/j_/art_/ds_/proj_ id> [--prefix]`
    says what the deletion would take, keep, and what refuses it; `--yes`
    does it, and `--acknowledge-citations` when articles cite it.
  - `mechbench history <kind> <id>` prints a lifetime's audit log, also
    after deletion.
- Needs mechbench-compute 0.97.0, whose `bench` library these wrap.

## 0.26.0 — 2026-09-17

### Changes that raise

- _None._

### Changes that alter results without raising

- _None._

### Other

- **A run that lost a branch is reported as one** (task 000515). Since
  compute 0.86.0 an edge may declare `on_missing`, so a failed branch
  can leave the run standing — and the job still landed as a plain
  `done`. The runner now reads the manifest's `nodes_missing` once, at
  the moment the result is produced, spools it beside the result, and
  declares it when finalizing a presigned upload (the one path where
  the API never holds the bytes to read it itself). The job lands as
  `done_with_missing`, carrying what did not run and why.
- `done_with_missing` joins the terminal statuses everywhere the runner
  lists them: the spool flush clears a result for such a job rather
  than offering it forever, `mechbench watch` stops and names each
  absent node instead of calling the run a failure, and the MCP smoke
  check will read its result.


## 0.25.0 — 2026-09-16

### Changes that raise

- _None._

### Changes that alter results without raising

- **A restart interrupts the job it is abandoning, and that now lands**
  (task 000511). `mechbench restart --force` runs in a different process
  from the one holding the job's claim token, so its `/interrupt` was
  refused (`403 missing x-claim-token`) and the job stayed `running` —
  which meant it could not be cancelled either, and every runner start
  adopted it again. `/interrupt` is now authorized by the claim's
  identity (this key, this machine) rather than its token, and hands
  back a fresh token; the CLI says so, and points at
  `mechbench cancel <job>` for ending the job rather than resuming it.
  Needs an API from 2026-09-16 or later; against an older one the
  restart prints the refusal and the job is resumed on the next start,
  as before.
- **A delivery the server will never accept is disowned instead of
  retried forever.** A spooled result whose claim the server no longer
  honours, or whose job was cancelled, was re-offered every five minutes
  for as long as the runner ran. A refusal that is a standing verdict
  (`NOT_CLAIMANT`, `BAD_CLAIM_TOKEN`, `BAD_STATE`) is now said once: the
  bytes are kept beside the spool as `result.cbor.disowned` with a note
  saying why, the reconcile stops offering them, and a `job.disowned`
  event goes to the live channel. Transient failures are retried exactly
  as before. A cancelled job's spool is cleared outright.

## 0.24.0 — 2026-09-16

### Changes that raise

- _None._

### Changes that alter results without raising

- **The service runs as a standard process, not a background one.**
  `mechbench install-service` wrote the launchd agent with
  `ProcessType: Background` — launchd's class for housekeeping, which
  on Apple Silicon steers the process to the efficiency cores at low
  priority (scheduling priority 4, where a process started from a
  terminal gets 31). Every model forward is a Python-bound graph build,
  so a job ran about 1.8× slower under the service than the same code
  run from a shell: a 312-condition decision read with rollout took 7.2
  minutes as a service and 3.9 in-process on the same idle machine, and
  the runs of August, launched from a terminal before the runner became
  a service, took 4. The agent is now `ProcessType: Standard`: no
  priority over the user's own work, and no penalty. Run
  `mechbench install-service` once to rewrite the agent; the numbers a
  job produces do not change, only how long it takes. (Compute task
  000506 has the measurements.)

## 0.23.0 — 2026-09-13

### Changes that raise

- _None._

### Changes that alter results without raising

- **A large result goes straight to object storage under a grant** (task
  000492). Above 8 MiB the runner asks `POST /jobs/:id/result-upload` for
  a presigned PUT bound to the job's own result key, the exact length and
  the sha256; PUTs the bytes there — no bearer token, the URL is the
  capability, and the store rejects any other bytes; then finalizes with
  `/complete {uploaded: true, contentHash}`. Below the threshold, or when
  the deployment's store answers 501 `UPLOAD_GRANT_UNSUPPORTED`, the
  direct completion is used as before. Both the first delivery and a
  reconcile-time late delivery take the same chooser, so a large result
  is never pushed at the API's 64 MiB body cap by either.
  `MECHBENCH_PRESIGN_THRESHOLD_BYTES` overrides the threshold; 0 forces
  the grant path, which is how it is exercised live. No result changes;
  what changes is that results the instance could not hold now land.

## 0.22.0 — 2026-09-13

### Changes that raise

- _None._

### Changes that alter results without raising

- **Every job-scoped write carries the claim's token** (task 000491). The
  claim response now returns a per-claim secret once, as `claimToken`;
  the runner sends it as `X-Claim-Token` on progress, preparing, interrupt,
  fail and complete, and persists it beside the spooled result so a late
  delivery after a restart still carries the claim that produced it. The
  server rotates the token on every re-claim, so a stale process holding
  the same key can no longer act on a job that has since been claimed
  again — which is the case the key check alone could not express. An
  API from before this returns no token and the runner sends no header;
  a job claimed before this has no hash and the server lets it pass. No
  result changes.

## 0.21.0 — 2026-09-13

### Changes that raise

- **Floors compute at 0.71.0**, whose `bench.emit` refuses a body over
  the API's 64 MiB object limit before sending it (task 000484). A
  runner on this version therefore fails such a job in one line with the
  size named, instead of retrying an upload the API now answers with 413
  and then interrupting for a resume. No runner code changed.

### Changes that alter results without raising

- _None._

## 0.20.0 — 2026-09-13

### Changes that raise

- **The transport-resume bound now holds on every resume path** (task
  000483, the hole found the day 0.19.0 shipped). 0.19.0 bounded resumes
  only in the error handler; a job could still come back through
  reconciliation — claimed here, nothing executing it — and that path
  resumed around the bound: "resume 1/2", then "attempt 2", "attempt 3",
  until the runner was stopped by hand. The check now lives at the
  resume decision every claim passes through: a job whose last interrupt
  was a transport failure and whose server-side `resumeCount` has
  reached the bound is FAILED with the same message, whichever path
  brought it back. A crash or watchdog resume is not bounded here — those
  may legitimately resume many times. The interrupt reason is matched by
  a shared prefix constant, so the writer and the reader cannot drift.

### Changes that alter results without raising

- **An item the spool cannot write is counted and logged, never silently
  dropped** (task 000485, corrected). `_spool_item` wrapped
  `JobSpool.item` in `suppress(Exception)`. For weeks every item of an
  adapted generate node raised `CBOREncodeError` inside it — the item
  carried a live `ModelRef` (000488) — and every one was discarded
  without a word, so the node's spool held only its fingerprint and a
  resume recovered nothing. The job still does not fail on a drop (the
  item exists in memory; the run continues), but the drop is now logged
  once per node with the key and the reason, counted per node on the
  spool, and reported as `dropped` in the resume summary. No result
  changes; what changes is that "resume recovered nothing" can no longer
  read as "nothing was there".

  Requires compute 0.70.0, whose fingerprint and hash-before-emit changes
  close the other two places the same error was being hidden.

## 0.19.0 — 2026-09-12

### Changes that raise

- **A job whose result the API will not accept now FAILS after two
  resumes**, instead of being resumed indefinitely (task 000483). This
  corrects 0.18.0, which is one hour old: interrupt-and-resume assumes the
  failure was transient, and for a deterministic one — a payload over the
  API's body limit — each resume re-runs the node that produced it to reach
  the same rejection. Experiment 014 turned a 35-minute generation node
  into exactly that loop. Before 0.18.0 such a job failed once; for one
  hour it looped forever; it now fails after a bounded retry, which is the
  behaviour both of the others were reaching for.

  The count is `max(in-process tally, the server's resumeCount)`. The tally
  alone resets on a runner restart, which is precisely when a loop would
  restart too; `resumeCount` alone counts resumes this branch did not
  cause, such as a watchdog kill. Taking the max can trip the bound early
  for an unrelated reason — the right way to be wrong here, since a
  premature failure costs a re-launch and the loop costs half an hour per
  cycle, indefinitely.

  The failure message names the API's body limit as the thing to check,
  because the next reader's question is "network or payload" and a repeat
  at the same node has already answered it.

### Changes that alter results without raising

- _None._

## 0.18.0 — 2026-09-12

### Changes that raise

- _None._

### Changes that alter results without raising

- **A job whose UPLOAD failed is now `interrupted`, not `failed`** (task
  000464). The old path called `fail_job` and then `_clear_spool`, which is
  correct for a block that raised — its partials would mislead a later
  reader — and wrong when the compute succeeded and only the upload did
  not. In that case the spool was the only surviving copy of the work, and
  deleting it threw the work away.

  The job is now interrupted instead, which is the state epic 000320 built
  for exactly this: claim, progress and `resultPath` survive on the server,
  the spool stays on disk, and the next claim resumes from the node
  boundary. Experiment 014 lost about 35 minutes of generation twice to the
  old behaviour.

  **Corrected in 0.19.0, and read that entry with this one.** Two claims
  here were wrong. A resume recovers only what a node has spooled, and the
  generation node spools nothing (task 000485), so this kept far less than
  "the items already spooled" implied. And resuming is only safe when the
  failure is transient; 014's was not, so 0.18.0 on its own loops (task
  000483). The real cause was an API that stalls on an oversized body
  instead of returning 413 (task 000484).

  The distinction is drawn by exception CLASS, not by matching the message:
  compute raises `bench.BenchTransportError` only after its bounded retry
  has exhausted itself (compute 0.68.0), and the cause chain is walked so a
  wrapped node error is still recognised. Against older compute there is no
  such class and every failure remains a failure, which is the previous
  behaviour.

- **`job.interrupted` on the control channel**, and an interrupt is no
  longer counted as a failure in `mechbench status`. Reporting a failure
  for a job that is about to resume and finish is a lie an operator then
  has to un-learn.

## 0.17.0 — 2026-09-13

### Changes that raise

- **Pin floor moves to mechbench-compute >= 0.67.0** for `bench.cancel`.

### Changes that alter results without raising

- **`mechbench cancel <job>…`** withdraws queued work (task 000463).
  Takes several ids, because draining a queue is the reason it exists,
  and reports each one (`cancelled (was queued)`, or `was already
  cancelled`); a refusal goes to stderr and the others are still tried,
  with a non-zero exit if any failed. `--reason` is recorded on the job
  and in the audit log. A thin wrapper over `bench.cancel`, like the
  other bench verbs.

## 0.16.0 — 2026-09-13

### Changes that raise

- **A second runner on one machine now stops DELIBERATELY.** Finding the
  control socket held by a live runner used to exit 1, and 1 means "come
  back" to both supervisors — so a runner that could never have the
  socket was restarted forever: the parent hit its crash limit, launchd
  restarted the parent, round it went, every message going to a log
  nobody was watching. It exits 0 now, naming the pid that holds the
  socket. `mechbench run` beside a running runner therefore returns 0,
  not 1, with the same message on stderr.
- **`mechbench restart` reports failure when nothing changed.** It used
  to ask the service manager whether SOMETHING was running and report
  success when it said yes; something always was — the process it had
  just asked to leave. A restart is now judged by the pid serving the
  control socket before versus after, and says `running (pid N, was M)`.
  A restart that did not take exits 1 and explains why.
- **A busy runner refuses a plain `restart` with the real reason.**
  SIGTERM means "finish the current job" by contract, so a restart
  during a job would wait, not restart. It says that, and points at
  `--force`.

### Changes that alter results without raising

- **`restart --force` abandons the job on the SERVER first**, with
  `POST /jobs/:id/interrupt` — the job keeps its claim, progress and
  result path and can be resumed (epic 000320) instead of waiting for
  the watchdog to reap it. Then, if the same pid is still serving, it
  escalates once by pid. An orphan is not the service manager's to stop,
  so nothing short of this could reach one.
- **A supervisor never leaves its child behind** (task 000462). Its
  stop grace is now strictly less than the service manager's own kill
  timeout — when both were 300 s, launchd killed the parent mid-wait and
  the child was re-parented to pid 1, where it held the socket and
  answered `status` with a version nobody had installed for another
  hour. Every exit path reaps the child, and the child is told which pid
  owns it so it can notice being orphaned and stand down between jobs.
- **`status` says whose answer it is:** a pid that has lost its
  supervisor is marked `ORPHAN`, and a hand-started runner
  `(unsupervised)`. The version on that line is only as trustworthy as
  the process reporting it.

## 0.15.0 — 2026-09-13

### Changes that raise

- **`mechbench run` with a launch flag but no PROTOCOL exits 2.**
  `mechbench run --wait` (or `--bind`, `--budget`) used to fall through
  to the bare form and start the runner daemon loop in the foreground,
  silently dropping the flag. A launch flag is a request to launch;
  without a protocol it is a typo, and it now says so.

### Changes that alter results without raising

- **`mechbench restart`'s busy guard names the phases the runner
  actually reports** — `executing`, `loading-model`, `downloading-model`
  (control.py) — instead of `preparing`/`running`, which never occur.
  The phase clause was dead; the `job is not None` clause carried the
  guard, so no restart was ever wrongly allowed. Now both clauses work.

## 0.14.0 — 2026-09-13

### Changes that raise

- **The bench verbs need mechbench-compute >= 0.61.0.** `run`, `watch`
  and `result` are now thin wrappers over the bench client library
  (`mechbench_compute.bench.launch` / `watch` / `results_for` /
  `result`, task 000450) — one implementation of the launch/watch/find/
  read plumbing, shared with the experiment scripts, instead of a second
  copy in the runner. An older compute has no such functions, so the pin
  floor moves to 0.61.0.

### Changes that alter results without raising

- **The verbs' transport and envelope handling are the library's now.**
  `ApiClient.create_run` and `find_runs` are removed — the library owns
  binding a protocol and finding a run by binding; `get_job` and
  `fetch_object` stay for the job loop and the MCP server. Output is
  unchanged: the same job-id line, change-only progress and metric
  table, applied to the payload the library already unwrapped. A
  transient poll error during `watch` is now shown as a `(fetch error:
  …)` line and retried, rather than swallowed.

## 0.13.0 — 2026-09-13

### Changes that raise

- _None._

### Changes that alter results without raising

- **`mechbench restart` restarts THIS runner through its own service
  manager** (task 000452): `launchctl kickstart -k` on macOS,
  `systemctl --user restart` on Linux. It refuses while a job is in
  flight unless `--force`, waits for the runner to come back (the
  status poll, not the command's exit, is the source of truth — on
  macOS `kickstart -k` blocks past a short timeout even on success),
  and prints the compute version it came back on. `kill <run-child>`
  looked right but took the supervisor with it; this does not.
- **`mechbench status` shows the compute version** the running process
  actually imported, next to the runner version — an editable install
  can be paused on stale bytes, and the number now says so. The runner
  reads `mechbench_compute.__version__` at start and carries it on the
  status snapshot; `""` when compute is not importable.

## 0.12.0 — 2026-09-13

### Changes that raise

- _None._

### Changes that alter results without raising

- **The bench verbs read the ONE response shape** now that the API has
  one (mechbench-api, task 000451): `run` reads the bare run (`id` +
  `jobId` at the top), and `watch`/`result` read the bare job — the
  defensive `x.get("thing", x)` accessors and the "find the job id
  wherever it hides" fallback are gone. Requires an API on or after the
  000451 change (deployed the same day).

## 0.11.0 — 2026-09-13

### Changes that raise

- _None._

### Changes that alter results without raising

- **`mechbench result` can find the job by what it RAN**:
  `mechbench result <node> --protocol <ref> --bind corpus=<path>`
  instead of `<job>/<node>`. It queries `GET /protocols/:ref/runs?
  binding.corpus=…` (mechbench-api, task 000449), takes the newest
  matching run with a result, and reads the node — so the six
  hand-maintained job-id sidecars an experiment kept (and that once
  published a wrong number when one drifted) can be deleted.
  `api_client` gains `find_runs`.

## 0.10.0 — 2026-09-13

### Changes that raise

- **`mechbench run` overloads.** With a PROTOCOL argument it now
  *launches* a protocol (bind, queue, record, optionally `--wait`) —
  the researcher's verb (task 000448). With none it is the runner
  polling loop, exactly as before (what the supervisor invokes). A
  bare `mechbench run` is unchanged; a typo that names a protocol no
  longer silently starts a loop.

### Changes that alter results without raising

- **Three bench verbs, the ones every experiment rewrote by hand
  (epic 000447):** `mechbench run <protocol> --bind k=v --budget N
  [--wait]`, `mechbench watch <job>…`, `mechbench result <job>/<node>
  [--json|--table|-o file]`. `run` records the job id before anything
  else (to stdout and `~/.mechbench/runs.jsonl`), and finds it wherever
  the response hides it (the `{run,jobId}` inconsistency, task 000451).
  `watch` prints only on change and exits non-zero if any job did not
  finish `done`. `result` strips the Emitted envelope, prints a table
  for a metric table and JSON otherwise. A `--bind` value starting with
  `{` or `[` is JSON (a model-ref binding is an object).


Versions before this file existed are **not classified**. The
convention was adopted on 2026-09-11 (task 000434) and applied
backwards only in `mechbench-compute`, where experiment 024's numbers
were live enough to audit honestly. Classifying runner's history from
memory would have been fabrication, so it was not done.
