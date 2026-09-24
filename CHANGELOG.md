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
