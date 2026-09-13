"""The three bench verbs — run / watch / result (task 000448), now thin
wrappers over `mechbench_compute.bench` (task 000450).

The pure rendering logic (bind parsing, the metric table, the change-only
progress line) runs directly. The verbs run against a faked bench library
— nothing touches the network or credentials — so what is tested here is
exactly the wrapper's job: the ordering (record the job id first), the
loud failure, the usage exits, and that the presentation is applied to the
library's already-unwrapped payload.
"""

from __future__ import annotations

import json

import pytest

from mechbench_compute.bench import BenchError
from mechbench_runner import bench_cmd as b


class TestBinds:
    def test_a_plain_value_is_a_string(self):
        assert b._binds(["model=gemma", "n=4"]) == {"model": "gemma", "n": "4"}

    def test_a_brace_or_bracket_value_is_json(self):
        got = b._binds(['ref={"provider":"x","model":"y"}', "layers=[1,2]"])
        assert got == {"ref": {"provider": "x", "model": "y"}, "layers": [1, 2]}

    def test_a_missing_equals_is_refused(self):
        with pytest.raises(SystemExit):
            b._binds(["justaname"])

    def test_bad_json_is_refused_with_the_name(self):
        with pytest.raises(SystemExit, match="ref"):
            b._binds(["ref={not json}"])


class TestTable:
    def test_a_metric_table_renders_aligned(self):
        mt = {"kind": "metric_table",
              "rows": [{"corpus": "base", "rate": 0.9995},
                       {"corpus": "digit6cap", "rate": 0.919}]}
        t = b._as_table(mt)
        assert "corpus" in t and "rate" in t and "0.9995" in t
        assert t.splitlines()[0].startswith("corpus")

    def test_non_tables_are_not_tables(self):
        assert b._as_table({"kind": "document_collection"}) is None
        assert b._as_table([1, 2, 3]) is None

    def test_records_is_accepted_as_well_as_rows(self):
        assert b._as_table({"kind": "metric_table", "records": [{"a": 1}]}) is not None


class TestProgressLine:
    def test_it_reads_the_job_shape(self):
        j = {"status": "running", "progressNum": 3, "progressDen": 8,
             "progressUnit": "steps", "progressNode": {"id": "ask"}, "spentUsd": 0.4}
        assert b._line(j) == "running 3/8 steps [ask] $0.4"

    def test_a_bare_status_is_fine(self):
        assert b._line({"status": "queued"}) == "queued"


CFG = object()  # never reached: _connect is stubbed out in `patched`


@pytest.fixture
def patched(monkeypatch, tmp_path):
    """Stub the bench library and the credential wiring, and isolate the
    history file. `install(name=fn, ...)` swaps in fake library verbs."""
    monkeypatch.setattr(b, "HISTORY", tmp_path / "runs.jsonl")
    monkeypatch.setattr(b, "_connect", lambda config: None)

    def install(**fns):
        for name, fn in fns.items():
            monkeypatch.setattr(b.bench, name, fn)
    return install


class TestRun:
    def test_it_records_the_job_id_before_returning(self, patched, capsys):
        seen = {}

        def launch(protocol, bindings, budget=None):
            seen["args"] = (protocol, bindings, budget)
            return {"id": "r1", "jobId": "j_abc"}
        patched(launch=launch)
        rc = b.run(CFG, "owner/p/proto", ["model=gemma"], 1.0, wait=False)
        assert rc == 0
        # the job id is the first line of stdout, for JOB=$(mechbench run …)
        assert capsys.readouterr().out.splitlines()[0] == "j_abc"
        # …and it is on disk, with the run id, the binding and the cap
        rec = json.loads(b.HISTORY.read_text().strip())
        assert rec["job"] == "j_abc" and rec["run"] == "r1"
        assert rec["bindings"] == {"model": "gemma"} and rec["budget_usd"] == 1.0
        assert seen["args"] == ("owner/p/proto", {"model": "gemma"}, 1.0)

    def test_no_job_id_is_a_clean_failure(self, patched, capsys):
        patched(launch=lambda protocol, bindings, budget=None: {"id": "r1"})
        assert b.run(CFG, "p", None, None, wait=False) == 1
        assert "no job id" in capsys.readouterr().err

    def test_a_launch_error_is_a_clean_failure(self, patched, capsys):
        def boom(protocol, bindings, budget=None):
            raise BenchError("POST /protocols/p/runs -> 404: nope")
        patched(launch=boom)
        assert b.run(CFG, "p", None, None, wait=False) == 1
        assert "run failed" in capsys.readouterr().err


class TestWatch:
    def test_it_prints_each_change_and_exits_zero_on_done(self, patched, capsys):
        def watch(jobs, interval=4.0):
            yield "j", {"status": "running", "progressNum": 1, "progressDen": 2}
            yield "j", {"status": "done", "progressNum": 2, "progressDen": 2}
        patched(watch=watch)
        rc = b.watch(CFG, ["j"], interval=0)
        lines = [ln for ln in capsys.readouterr().out.splitlines() if "j" in ln]
        # the library yields only on change; the wrapper prints each yield
        assert len(lines) == 2
        assert rc == 0

    def test_a_failure_is_loud_and_exits_non_zero(self, patched, capsys):
        def watch(jobs, interval=4.0):
            yield "j", {"status": "failed", "errorMessage": "boom"}
        patched(watch=watch)
        rc = b.watch(CFG, ["j"], interval=0)
        out = capsys.readouterr().out
        assert "FAILED" in out and "boom" in out
        assert rc == 1

    def test_a_transient_fetch_error_is_shown_and_not_fatal(self, patched, capsys):
        def watch(jobs, interval=4.0):
            yield "j", {"status": None, "error": "502 bad gateway"}
            yield "j", {"status": "done"}
        patched(watch=watch)
        rc = b.watch(CFG, ["j"], interval=0)
        out = capsys.readouterr().out
        assert "fetch error" in out and "502" in out
        assert rc == 0


class TestCancel:
    """Draining a queue is the reason this verb exists, so it takes
    several ids and reports each one (task 000463)."""

    def test_it_cancels_each_id_and_says_what_each_was(self, patched, capsys):
        seen = []

        def cancel(job, reason=""):
            seen.append((job, reason))
            return {"ok": True, "status": "cancelled", "from": "queued"}
        patched(cancel=cancel)
        assert b.cancel(CFG, ["j_a", "j_b"], "duplicate") == 0
        assert seen == [("j_a", "duplicate"), ("j_b", "duplicate")]
        out = capsys.readouterr().out
        assert "j_a cancelled (was queued)" in out and "j_b cancelled" in out

    def test_an_already_cancelled_job_is_not_an_error(self, patched, capsys):
        patched(cancel=lambda job, reason="": {"ok": True, "status": "cancelled",
                                               "alreadyCancelled": True})
        assert b.cancel(CFG, ["j_a"], "") == 0
        assert "already cancelled" in capsys.readouterr().out

    def test_a_refusal_exits_non_zero_but_still_tries_the_rest(self, patched, capsys):
        def cancel(job, reason=""):
            if job == "j_running":
                raise BenchError("409: a running job cannot be cancelled")
            return {"ok": True, "status": "cancelled", "from": "queued"}
        patched(cancel=cancel)
        assert b.cancel(CFG, ["j_running", "j_queued"], "") == 1
        cap = capsys.readouterr()
        assert "running job cannot be cancelled" in cap.err
        assert "j_queued cancelled" in cap.out  # the rest were still done


class TestResult:
    def test_a_document_payload_prints_json(self, patched, capsys):
        seen = {}

        def result(source, node):
            seen["args"] = (source, node)
            return {"kind": "document_collection", "items": [1]}
        patched(result=result)
        assert b.result(CFG, "j/ask", "auto", None) == 0
        out = json.loads(capsys.readouterr().out)
        assert out == {"kind": "document_collection", "items": [1]}
        assert seen["args"] == ("j", "ask")  # <job>/<node> split by the wrapper

    def test_a_metric_table_prints_a_table(self, patched, capsys):
        patched(result=lambda source, node: {"kind": "metric_table",
                                             "rows": [{"n": 3}]})
        assert b.result(CFG, "j/grade", "auto", None) == 0
        assert "n" in capsys.readouterr().out

    def test_no_slash_is_a_usage_error(self, patched):
        patched(result=lambda *a: None)
        assert b.result(CFG, "just-a-job", "auto", None) == 2

    def test_a_job_without_a_result_is_a_clean_failure(self, patched, capsys):
        def result(source, node):
            raise BenchError("job j has no result yet (status running)")
        patched(result=result)
        assert b.result(CFG, "j/ask", "auto", None) == 1
        assert "no result yet" in capsys.readouterr().err

    def test_o_writes_json_to_a_file(self, patched, tmp_path):
        patched(result=lambda source, node: {"k": 1})
        out = tmp_path / "r.json"
        assert b.result(CFG, "j/n", "auto", str(out)) == 0
        assert json.loads(out.read_text()) == {"k": 1}


class TestResultByBinding:
    """`result <node> --protocol X --bind corpus=Y` — find the run by what
    it ran, the end of the job-id sidecars (task 000449)."""

    def test_it_finds_the_run_by_binding_and_reads_the_node(self, patched, capsys):
        seen = {}

        def results_for(protocol, **binds):
            seen["find"] = (protocol, binds)
            return [{"jobId": "j_new", "resultPath": "o/p/results/j_new"}]

        def result(source, node):
            seen["read"] = (source, node)
            return {"kind": "document_collection", "items": [1]}
        patched(results_for=results_for, result=result)
        rc = b.result(CFG, "grade", "auto", None,
                      protocol="024-variety", binds=["corpus=benji/c/animals"])
        assert rc == 0
        # the binding filter went to the library as keywords
        assert seen["find"] == ("024-variety", {"corpus": "benji/c/animals"})
        # the newest matching run (with a result) is the one read
        assert seen["read"][0]["jobId"] == "j_new" and seen["read"][1] == "grade"
        assert json.loads(capsys.readouterr().out) == {"kind": "document_collection",
                                                       "items": [1]}

    def test_no_matching_run_is_a_clean_failure(self, patched, capsys):
        patched(results_for=lambda protocol, **binds: [])
        rc = b.result(CFG, "grade", "auto", None,
                      protocol="024-variety", binds=["corpus=nope"])
        assert rc == 1 and "no run of 024-variety" in capsys.readouterr().err

    def test_a_run_without_a_result_yet_is_skipped(self, patched, capsys):
        # a matching run exists but its job has not produced a result
        patched(results_for=lambda protocol, **binds: [
            {"jobId": "j", "resultPath": None}])
        assert b.result(CFG, "grade", "auto", None,
                        protocol="p", binds=["corpus=x"]) == 1
