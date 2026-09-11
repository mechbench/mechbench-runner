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

Versions before this file existed are **not classified**. The
convention was adopted on 2026-09-11 (task 000434) and applied
backwards only in `mechbench-compute`, where experiment 024's numbers
were live enough to audit honestly. Classifying runner's history from
memory would have been fabrication, so it was not done.
