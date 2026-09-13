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
