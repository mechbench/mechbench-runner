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
