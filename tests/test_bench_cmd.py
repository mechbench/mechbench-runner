"""The three bench verbs — run / watch / result (task 000448).

The pure logic (bind parsing, envelope unwrapping, table rendering, the
change-only progress line) runs directly. The verbs themselves run
against a fake ApiClient so nothing touches the network — the point is
the ordering (record the job id first), the change-only printing, the
non-zero exit on failure, and the stripped envelope.
"""

from __future__ import annotations

import json

import pytest

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


class TestUnwrap:
    def test_the_emitted_envelope_is_stripped(self):
        assert b._unwrap({"payload": {"k": 1}, "provenance": {}}) == {"k": 1}

    def test_a_bare_payload_is_left_alone(self):
        assert b._unwrap({"kind": "metric_table"}) == {"kind": "metric_table"}
        assert b._unwrap({"payload": 1}) == {"payload": 1}  # no provenance → not an envelope


class TestTable:
    def test_a_metric_table_renders_aligned(self):
        mt = {"kind": "metric_table",
              "rows": [{"corpus": "base", "rate": 0.9995},
                       {"corpus": "digit6cap", "rate": 0.919}]}
        t = b._as_table(mt)
        assert "corpus" in t and "rate" in t and "0.9995" in t
        # columns align: every line is the same width
        widths = {len(line.rstrip()) for line in t.splitlines()}
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


class FakeApi:
    """A stand-in for ApiClient: scripted responses, recorded calls."""

    def __init__(self, *, create=None, jobs=None, obj=None, error=None, runs=None):
        self._create = create
        self._jobs = jobs or {}
        self._obj = obj
        self._error = error
        self._runs = runs
        self.created = None
        self.find_args = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def create_run(self, protocol, body):
        self.created = (protocol, body)
        if self._error:
            raise self._error
        return self._create

    def get_job(self, job_id):
        seq = self._jobs.get(job_id)
        return seq.pop(0) if isinstance(seq, list) and len(seq) > 1 else (
            seq[0] if isinstance(seq, list) else seq)

    def find_runs(self, protocol, bindings=None):
        self.find_args = (protocol, bindings)
        if self._error:
            raise self._error
        return self._runs or []

    def fetch_object(self, path):
        if self._error:
            raise self._error
        return self._obj


@pytest.fixture
def patched(monkeypatch, tmp_path):
    """Bench verbs with a fake client and an isolated history file."""
    monkeypatch.setattr(b, "HISTORY", tmp_path / "runs.jsonl")

    def install(fake):
        monkeypatch.setattr(b, "ApiClient", lambda config: fake)
        return fake
    return install


CFG = object()  # config is passed straight to ApiClient, which is faked


class TestRun:
    def test_it_records_the_job_id_before_returning(self, patched, capsys):
        # One shape now (task 000451): the bare run, with jobId on it.
        fake = patched(FakeApi(create={"id": "r1", "jobId": "j_abc"}))
        rc = b.run(CFG, "owner/p/proto", ["model=gemma"], 1.0, wait=False)
        assert rc == 0
        # the job id is the first line of stdout, for JOB=$(mechbench run …)
        assert capsys.readouterr().out.splitlines()[0] == "j_abc"
        # …and it is on disk, with the run id, the binding and the cap
        rec = json.loads(b.HISTORY.read_text().strip())
        assert rec["job"] == "j_abc" and rec["run"] == "r1"
        assert rec["bindings"] == {"model": "gemma"} and rec["budget_usd"] == 1.0
        assert fake.created == ("owner/p/proto", {"bindings": {"model": "gemma"},
                                                  "budgetUsd": 1.0})

    def test_no_job_id_is_a_clean_failure(self, patched, capsys):
        patched(FakeApi(create={"id": "r1"}))  # a run with no jobId
        assert b.run(CFG, "p", None, None, wait=False) == 1
        assert "no job id" in capsys.readouterr().err

    def test_an_api_error_is_a_clean_failure(self, patched, capsys):
        from mechbench_runner.api_client import ApiError
        patched(FakeApi(error=ApiError(404, "nope")))
        assert b.run(CFG, "p", None, None, wait=False) == 1
        assert "run failed" in capsys.readouterr().err


class TestWatch:
    def test_it_prints_only_on_change_and_exits_zero_on_done(self, patched, capsys):
        patched(FakeApi(jobs={"j": [
            {"status": "running", "progressNum": 1, "progressDen": 2},
            {"status": "running", "progressNum": 1, "progressDen": 2},  # no change
            {"status": "done", "progressNum": 2, "progressDen": 2},
        ]}))
        rc = b.watch(CFG, ["j"], interval=0)
        lines = [ln for ln in capsys.readouterr().out.splitlines() if "j" in ln]
        # three polls, two distinct states → two printed lines, not three
        assert len(lines) == 2
        assert rc == 0

    def test_a_failure_is_loud_and_exits_non_zero(self, patched, capsys):
        patched(FakeApi(jobs={"j": {"status": "failed",
                                    "errorMessage": "boom"}}))
        rc = b.watch(CFG, ["j"], interval=0)
        out = capsys.readouterr().out
        assert "FAILED" in out and "boom" in out
        assert rc == 1


class TestResult:
    def _obj(self, payload):
        import mechbench_schema as ms
        return ms.dump_canonical(payload)

    def test_a_document_payload_prints_json_without_the_envelope(self, patched, capsys):
        env = {"payload": {"kind": "document_collection", "items": [1]},
               "provenance": {"created_at": "now"}}
        patched(FakeApi(jobs={"j": {"resultPath": "o/p/results/j"}},
                        obj=self._obj(env)))
        assert b.result(CFG, "j/ask", "auto", None) == 0
        out = json.loads(capsys.readouterr().out)
        assert out == {"kind": "document_collection", "items": [1]}  # unwrapped

    def test_a_metric_table_prints_a_table(self, patched, capsys):
        env = {"payload": {"kind": "metric_table", "rows": [{"n": 3}]},
               "provenance": {"created_at": "now"}}
        patched(FakeApi(jobs={"j": {"resultPath": "o/p/results/j"}},
                        obj=self._obj(env)))
        assert b.result(CFG, "j/grade", "auto", None) == 0
        assert "n" in capsys.readouterr().out

    def test_no_slash_is_a_usage_error(self, patched):
        patched(FakeApi())
        assert b.result(CFG, "just-a-job", "auto", None) == 2

    def test_a_job_without_a_result_is_a_clean_failure(self, patched, capsys):
        patched(FakeApi(jobs={"j": {"status": "running"}}))
        assert b.result(CFG, "j/ask", "auto", None) == 1
        assert "no result yet" in capsys.readouterr().err

    def test_o_writes_json_to_a_file(self, patched, tmp_path):
        env = {"payload": {"k": 1}, "provenance": {"created_at": "now"}}
        patched(FakeApi(jobs={"j": {"resultPath": "o/p/results/j"}},
                        obj=self._obj(env)))
        out = tmp_path / "r.json"
        assert b.result(CFG, "j/n", "auto", str(out)) == 0
        assert json.loads(out.read_text()) == {"k": 1}


class TestResultByBinding:
    """`result <node> --protocol X --bind corpus=Y` — find the job by
    what it ran, the end of the job-id sidecars (task 000449)."""

    def _obj(self, payload):
        import mechbench_schema as ms
        return ms.dump_canonical(payload)

    def test_it_finds_the_run_by_binding_and_reads_the_node(self, patched, capsys):
        env = {"payload": {"kind": "document_collection", "items": [1]},
               "provenance": {"created_at": "now"}}
        fake = patched(FakeApi(
            runs=[{"jobId": "j_new", "resultPath": "o/p/results/j_new"}],
            obj=self._obj(env)))
        rc = b.result(CFG, "grade", "auto", None,
                      protocol="024-variety", binds=["corpus=benji/c/animals"])
        assert rc == 0
        # the binding filter went to the server
        assert fake.find_args == ("024-variety", {"corpus": "benji/c/animals"})
        assert json.loads(capsys.readouterr().out) == {"kind": "document_collection",
                                                       "items": [1]}

    def test_no_matching_run_is_a_clean_failure(self, patched, capsys):
        patched(FakeApi(runs=[]))
        rc = b.result(CFG, "grade", "auto", None,
                      protocol="024-variety", binds=["corpus=nope"])
        assert rc == 1 and "no run of 024-variety" in capsys.readouterr().err

    def test_a_run_without_a_result_yet_is_skipped(self, patched, capsys):
        # a matching run exists but its job has not produced a result
        patched(FakeApi(runs=[{"jobId": "j", "resultPath": None}]))
        assert b.result(CFG, "grade", "auto", None,
                        protocol="p", binds=["corpus=x"]) == 1
