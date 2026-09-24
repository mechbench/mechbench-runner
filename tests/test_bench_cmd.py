from __future__ import annotations

import inspect
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


CFG = object()


@pytest.fixture
def patched(monkeypatch, tmp_path):
    monkeypatch.setattr(b, "HISTORY", tmp_path / "runs.jsonl")
    monkeypatch.setattr(b, "_connect", lambda config: None)

    def install(**fns):
        for name, fn in fns.items():
            sig = inspect.signature(getattr(b.bench, name))

            def checked(*args, _fn=fn, _sig=sig, **kwargs):
                _sig.bind(*args, **kwargs)
                return _fn(*args, **kwargs)
            monkeypatch.setattr(b.bench, name, checked)
    return install


class TestRun:
    def test_it_records_the_job_id_before_returning(self, patched, capsys):
        seen = {}

        def launch(protocol, **declared):
            seen["protocol"] = protocol
            seen["declared"] = declared
            return {"id": "r1", "jobId": "j_abc"}
        patched(launch=launch)
        rc = b.run(CFG, "owner/p/proto", None, 1.0, wait=False,
                   params=["model=gemma"], label="P0, reasoning on")
        assert rc == 0
        assert capsys.readouterr().out.splitlines()[0] == "j_abc"
        rec = json.loads(b.HISTORY.read_text().strip())
        assert rec["job"] == "j_abc" and rec["run"] == "r1"
        assert rec["params"] == {"model": "gemma"} and rec["budget_usd"] == 1.0
        assert rec["label"] == "P0, reasoning on"
        assert seen["protocol"] == "owner/p/proto"
        assert seen["declared"]["budget"] == 1.0
        assert seen["declared"]["label"] == "P0, reasoning on"

    def test_the_legacy_binding_is_refused_before_anything_is_sent(
            self, patched, capsys):
        patched(launch=lambda protocol, **_d: pytest.fail("launched"))
        assert b.run(CFG, "p", ["model=gemma"], None, wait=False) == 2
        assert "--param" in capsys.readouterr().err

    def test_params_and_inputs_bind_by_name_and_keep_is_passed(self, patched):
        seen = {}

        def launch(protocol, **declared):
            seen["declared"] = declared
            return {"id": "r2", "jobId": "j_def"}
        patched(launch=launch)
        rc = b.run(CFG, "p", None, None, wait=False,
                   params=["n=12", "label=draws", "flag=true"],
                   inputs=["prompts=lab/p/prompts"], keep="outputs")
        assert rc == 0
        assert seen["declared"] == {"params": {"n": 12, "label": "draws", "flag": True},
                                    "inputs": {"prompts": "lab/p/prompts"},
                                    "keep": "outputs", "budget": None, "label": None}
        rec = json.loads(b.HISTORY.read_text().strip().splitlines()[-1])
        assert rec["params"] == {"n": 12, "label": "draws", "flag": True}
        assert rec["inputs"] == {"prompts": "lab/p/prompts"}
        assert rec["keep"] == "outputs"

    def test_no_job_id_is_a_clean_failure(self, patched, capsys):
        patched(launch=lambda protocol, **_d: {"id": "r1"})
        assert b.run(CFG, "p", None, None, wait=False) == 1
        assert "no job id" in capsys.readouterr().err

    def test_a_launch_error_is_a_clean_failure(self, patched, capsys):
        def boom(protocol, **_d):
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
        assert "j_queued cancelled" in cap.out


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
        assert seen["args"] == ("j", "ask")

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
        assert seen["find"] == ("024-variety", {"corpus": "benji/c/animals"})
        assert seen["read"][0]["jobId"] == "j_new" and seen["read"][1] == "grade"
        assert json.loads(capsys.readouterr().out) == {"kind": "document_collection",
                                                       "items": [1]}

    def test_no_matching_run_is_a_clean_failure(self, patched, capsys):
        patched(results_for=lambda protocol, **binds: [])
        rc = b.result(CFG, "grade", "auto", None,
                      protocol="024-variety", binds=["corpus=nope"])
        assert rc == 1 and "no run of 024-variety" in capsys.readouterr().err

    def test_a_run_without_a_result_yet_is_skipped(self, patched, capsys):
        patched(results_for=lambda protocol, **binds: [
            {"jobId": "j", "resultPath": None}])
        assert b.result(CFG, "grade", "auto", None,
                        protocol="p", binds=["corpus=x"]) == 1


SITE = type("Cfg", (), {"api_base_url": "https://api.mechbench.ai"})()


class TestProtocolPublish:
    def test_the_head_by_default_and_the_public_page(self, patched, capsys):
        seen = {}

        def publish(protocol, version):
            seen["args"] = (protocol, version)
            return {"publicPath": "/benji/lab/protocols/prt_1/v/3",
                    "unpublishedIncludes": [{"name": "leaf"}]}
        patched(get_protocol=lambda protocol: {"id": protocol, "version": 3},
                publish_protocol_version=publish)
        assert b.protocol_publish(SITE, "prt_1", None) == 0
        assert seen["args"] == ("prt_1", 3)
        cap = capsys.readouterr()
        assert "prt_1 v3 published" in cap.out
        assert "https://mechbench.ai/benji/lab/protocols/prt_1/v/3" in cap.out
        assert "leaf, which is not published" in cap.err

    def test_unpublish_names_the_citing_articles(self, patched, capsys):
        patched(unpublish_protocol_version=lambda protocol, version: {
            "citedBy": [{"title": "Lighthouse", "ownerHandle": "benji", "slug": "lh",
                         "status": "published"}], "unreadable": 1})
        assert b.protocol_unpublish(SITE, "prt_1", 3) == 0
        out = capsys.readouterr().out
        assert "2 article(s) cite it" in out and "Lighthouse — benji/articles/lh" in out


class TestProtocolCopy:
    def test_it_parses_the_source_and_destination_and_reports_each_step(
            self, patched, capsys):
        seen = {}

        def copy(pid, version, owner, project, name=None, owner_kind="user",
                 dry_run=False):
            seen["args"] = (pid, version, owner, project, name, owner_kind, dry_run)
            return {"name": "top-2", "reused": [],
                    "copied": [{"from": {"name": "leaf", "version": 1},
                                "to": {"name": "leaf"}}]}
        patched(copy_protocol_version=copy)
        assert b.protocol_copy(SITE, "prt_1@4", "lab/bench", None, True, True) == 0
        assert seen["args"] == ("prt_1", 4, "lab", "bench", None, "org", True)
        out = capsys.readouterr().out
        assert "would create lab/bench/top-2" in out
        assert "would copy leaf v1 as leaf" in out

    def test_a_malformed_source_is_a_usage_error(self, patched):
        assert b.protocol_copy(SITE, "prt_1", "lab/bench", None, False, False) == 2
        assert b.protocol_copy(SITE, "prt_1@4", "lab", None, False, False) == 2


class TestDelete:
    def test_without_yes_it_only_describes(self, patched, capsys):
        calls = []

        def delete(target, prefix=False, dry_run=False, acknowledge_citations=False):
            calls.append((target, prefix, dry_run, acknowledge_citations))
            return {"deletes": {"objects": 2}, "keeps": {}, "refusal": None,
                    "citedBy": [], "unreadable": 0}
        patched(delete=delete)
        assert b.delete(SITE, "benji/lab/notes", True, False, False) == 0
        assert calls == [("benji/lab/notes", True, True, False)]
        assert "would delete: 2 objects" in capsys.readouterr().out

    def test_a_refusal_names_what_is_in_the_way(self, patched, capsys):
        refusal = {"code": "INCLUDED", "error": "other protocols include it",
                   "includedBy": [{"ownerHandle": "benji", "name": "outer",
                                   "id": "prt_2"}]}
        patched(delete=lambda target, prefix=False, dry_run=False,
                acknowledge_citations=False: {"refusal": refusal})
        assert b.delete(SITE, "prt_1", False, True, False) == 1
        out = capsys.readouterr().out
        assert "refused (INCLUDED)" in out and "benji/outer (prt_2)" in out

    def test_citations_stop_a_yes_until_acknowledged(self, patched, capsys):
        calls = []

        def delete(target, prefix=False, dry_run=False, acknowledge_citations=False):
            calls.append((dry_run, acknowledge_citations))
            return {"deletes": {"protocols": 1}, "keeps": {}, "refusal": None,
                    "citedBy": [{"title": "t", "ownerHandle": "o", "slug": "s",
                                 "status": "draft"}],
                    "unreadable": 0}
        patched(delete=delete)
        assert b.delete(SITE, "prt_1", False, True, False) == 1
        assert calls == [(True, False)]
        assert b.delete(SITE, "prt_1", False, True, True) == 0
        assert calls[-1] == (False, True)
