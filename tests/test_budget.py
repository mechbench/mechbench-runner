"""The model-cache budget (task 000297).

What matters: the deficit math is honest, LRU means least-recently-USED,
the recency floor beats the budget, protected names never go, and every
eviction is announced. Everything runs against temp dirs and fake
inventories — the conftest fence guarantees none of it can see the real
cache, and these tests never try.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from mechbench_runner import budget

DAY = 86400.0
NOW = 1_800_000_000.0


# -- fixtures: a fake hub inventory and a real (temp) checkpoint cache


class FakeInventory:
    """Quacks like mechbench_compute.inventory: scan() + delete_revisions()."""

    def __init__(self, repos):
        self._repos = repos
        self.deleted: list[list[str]] = []

    def scan(self):
        return list(self._repos)

    def delete_revisions(self, commits):
        self.deleted.append(list(commits))
        return sum(1 for _ in commits)


def repo(repo_id, gb, *, modified_days_ago, commits=("c1",)):
    when = datetime.fromtimestamp(NOW - modified_days_ago * DAY, tz=UTC)
    revs = [
        SimpleNamespace(commit=c, last_modified=when) for c in commits
    ]
    return SimpleNamespace(
        repo_id=repo_id, disk_bytes=int(gb * 1e9), revisions=revs
    )


def free_disk(gb):
    def usage(_path):
        return SimpleNamespace(free=int(gb * 1e9))
    return usage


def make_checkpoint(root, key, *, gb, used_days_ago, label=None):
    d = root / key
    d.mkdir(parents=True)
    (d / "w.safetensors").write_bytes(b"x" * 1024)
    # the declared size is faked through a sparse-ish trick: tests care
    # about ordering and totals, not real gigabytes on a CI disk — so we
    # keep files tiny and lie only in the fake *hub* sizes. Checkpoint
    # sizes here are their true (small) byte counts.
    mark = d / ".complete"
    mark.touch()
    then = NOW - used_days_ago * DAY
    import os

    os.utime(mark, (then, then))
    if label:
        (d / ".label").write_text(label)
    return d


# -- the policy file


class TestBudgetFile:
    def test_round_trips(self, tmp_path):
        b = budget.Budget(keep_free_gb=100.0, max_cache_gb=None, recent_days=5.0)
        budget.save(b, tmp_path)
        assert budget.load(tmp_path) == b

    def test_absent_file_is_no_budget(self, tmp_path):
        b = budget.load(tmp_path)
        assert not b.is_set
        assert b.recent_days == budget.DEFAULT_RECENT_DAYS

    def test_garbage_file_is_no_budget(self, tmp_path):
        budget.budget_path(tmp_path).write_text("not toml {{{")
        assert not budget.load(tmp_path).is_set

    def test_clear_removes_it(self, tmp_path):
        budget.save(budget.Budget(keep_free_gb=50.0), tmp_path)
        budget.clear(tmp_path)
        assert not budget.load(tmp_path).is_set


class TestUsageJournal:
    def test_records_by_repo_dropping_the_revision(self, tmp_path):
        budget.record_use("org/model@abc123", tmp_path, now=NOW)
        journal = budget._read_journal(budget.usage_path(tmp_path))
        assert journal == {"org/model": NOW}

    def test_a_corrupt_journal_reads_empty_and_recovers(self, tmp_path):
        budget.usage_path(tmp_path).write_text("[not a dict]")
        budget.record_use("org/model", tmp_path, now=NOW)
        journal = budget._read_journal(budget.usage_path(tmp_path))
        assert journal == {"org/model": NOW}


# -- planning


class TestPlan:
    def test_no_budget_plans_nothing(self, tmp_path):
        p = budget.plan(
            budget.Budget(), root=tmp_path, checkpoints_root=tmp_path / "ck",
            inventory=FakeInventory([]), disk_usage=free_disk(5), now=NOW,
        )
        assert p.deficit_bytes == 0 and p.evictions == []

    def test_keep_free_deficit_is_the_shortfall(self, tmp_path):
        inv = FakeInventory([repo("org/a", 30, modified_days_ago=30)])
        p = budget.plan(
            budget.Budget(keep_free_gb=100.0), root=tmp_path,
            checkpoints_root=tmp_path / "ck", inventory=inv,
            disk_usage=free_disk(80), now=NOW,
        )
        assert p.deficit_bytes == pytest.approx(20e9)
        assert [c.name for c in p.evictions] == ["org/a"]

    def test_max_cache_deficit_is_the_overage(self, tmp_path):
        inv = FakeInventory([
            repo("org/a", 30, modified_days_ago=30),
            repo("org/b", 40, modified_days_ago=10),
        ])
        p = budget.plan(
            budget.Budget(max_cache_gb=50.0), root=tmp_path,
            checkpoints_root=tmp_path / "ck", inventory=inv,
            disk_usage=free_disk(500), now=NOW,
        )
        assert p.deficit_bytes == pytest.approx(20e9)
        # oldest first, and one 30 GB eviction already covers 20 GB
        assert [c.name for c in p.evictions] == ["org/a"]

    def test_lru_prefers_the_journal_over_last_modified(self, tmp_path):
        # b was DOWNLOADED long ago but USED yesterday; a is the reverse.
        inv = FakeInventory([
            repo("org/a", 30, modified_days_ago=5),
            repo("org/b", 30, modified_days_ago=60),
        ])
        budget.record_use("org/b", tmp_path, now=NOW - 1 * DAY)
        budget.record_use("org/a", tmp_path, now=NOW - 20 * DAY)
        p = budget.plan(
            budget.Budget(keep_free_gb=100.0), root=tmp_path,
            checkpoints_root=tmp_path / "ck", inventory=inv,
            disk_usage=free_disk(90), now=NOW,
        )
        # a's journal entry is OLDER than its download stamp; the max of
        # the two stamps still makes a (5 days) older than b (1 day).
        assert [c.name for c in p.evictions] == ["org/a"]

    def test_recent_floor_beats_the_budget(self, tmp_path):
        inv = FakeInventory([repo("org/a", 30, modified_days_ago=1)])
        p = budget.plan(
            budget.Budget(keep_free_gb=100.0, recent_days=3.0), root=tmp_path,
            checkpoints_root=tmp_path / "ck", inventory=inv,
            disk_usage=free_disk(50), now=NOW,
        )
        assert p.evictions == []
        assert p.short_bytes == p.deficit_bytes > 0

    def test_protected_names_never_go(self, tmp_path):
        inv = FakeInventory([
            repo("org/warm", 30, modified_days_ago=90),
            repo("org/cold", 30, modified_days_ago=60),
        ])
        p = budget.plan(
            budget.Budget(keep_free_gb=100.0), root=tmp_path,
            checkpoints_root=tmp_path / "ck", inventory=inv,
            disk_usage=free_disk(90), protect={"org/warm"}, now=NOW,
        )
        assert [c.name for c in p.evictions] == ["org/cold"]

    def test_checkpoints_are_candidates_with_their_labels(self, tmp_path):
        ck = tmp_path / "ck"
        make_checkpoint(ck, "abc123", gb=10, used_days_ago=30,
                        label="me/proj/checkpoints/v1")
        p = budget.plan(
            budget.Budget(keep_free_gb=100.0), root=tmp_path,
            checkpoints_root=ck, inventory=FakeInventory([]),
            disk_usage=free_disk(50), now=NOW,
        )
        assert [c.name for c in p.evictions] == ["me/proj/checkpoints/v1"]
        assert p.evictions[0].kind == "checkpoint"


# -- sweeping


class TestSweep:
    def test_deletes_all_commits_of_an_evicted_repo(self, tmp_path):
        inv = FakeInventory([
            repo("org/a", 30, modified_days_ago=30, commits=("c1", "c2")),
        ])
        done = budget.sweep(
            budget.Budget(keep_free_gb=100.0), root=tmp_path,
            checkpoints_root=tmp_path / "ck", inventory=inv,
            disk_usage=free_disk(80), now=NOW,
        )
        assert [c.name for c in done] == ["org/a"]
        assert inv.deleted == [["c1", "c2"]]

    def test_removes_checkpoint_directories(self, tmp_path):
        ck = tmp_path / "ck"
        d = make_checkpoint(ck, "abc123", gb=10, used_days_ago=30)
        done = budget.sweep(
            budget.Budget(keep_free_gb=100.0), root=tmp_path,
            checkpoints_root=ck, inventory=FakeInventory([]),
            disk_usage=free_disk(50), now=NOW,
        )
        assert [c.kind for c in done] == ["checkpoint"]
        assert not d.exists()

    def test_announces_every_eviction(self, tmp_path):
        inv = FakeInventory([repo("org/a", 30, modified_days_ago=30)])
        events = []
        budget.sweep(
            budget.Budget(keep_free_gb=100.0), root=tmp_path,
            checkpoints_root=tmp_path / "ck", inventory=inv,
            disk_usage=free_disk(80), now=NOW,
            emit=lambda ev, data: events.append((ev, data)),
        )
        assert len(events) == 1
        ev, data = events[0]
        assert ev == "cache.evicted"
        assert data["name"] == "org/a" and data["kind"] == "model"

    def test_a_failed_deletion_does_not_stop_the_sweep(self, tmp_path):
        class Grumpy(FakeInventory):
            def delete_revisions(self, commits):
                if commits == ["bad"]:
                    raise ValueError("nope")
                return super().delete_revisions(commits)

        inv = Grumpy([
            repo("org/bad", 30, modified_days_ago=60, commits=("bad",)),
            repo("org/good", 30, modified_days_ago=30, commits=("good",)),
        ])
        said = []
        done = budget.sweep(
            budget.Budget(keep_free_gb=100.0), root=tmp_path,
            checkpoints_root=tmp_path / "ck", inventory=inv,
            disk_usage=free_disk(50), now=NOW, say=said.append,
        )
        assert [c.name for c in done] == ["org/good"]
        assert any("org/bad" in m for m in said)

    def test_no_budget_is_a_fast_no_op(self, tmp_path):
        # No inventory scan, no disk probe — this runs on every claim.
        def explode(_path):
            raise AssertionError("disk_usage consulted with no budget set")

        done = budget.sweep(
            budget.Budget(), root=tmp_path, checkpoints_root=tmp_path / "ck",
            inventory=FakeInventory([]), disk_usage=explode, now=NOW,
        )
        assert done == []


class TestDescribe:
    def test_reads_like_a_sentence(self):
        b = budget.Budget(keep_free_gb=100.0, recent_days=3.0)
        s = budget.describe(b)
        assert "100" in s and "3" in s
        assert budget.describe(budget.Budget()) == "no budget set"

    def test_both_forms_compose(self):
        b = budget.Budget(keep_free_gb=100.0, max_cache_gb=300.0)
        s = budget.describe(b)
        assert "100" in s and "300" in s and " and " in s


class TestDoctorCheck:
    def test_warns_without_a_budget_and_settles_with_one(self, monkeypatch):
        from mechbench_runner import doctor

        monkeypatch.setattr(budget, "load", lambda root=None: budget.Budget())
        check = doctor._budget()
        assert check.status == doctor.WARN

        monkeypatch.setattr(
            budget, "load",
            lambda root=None: budget.Budget(keep_free_gb=100.0),
        )
        check = doctor._budget()
        assert check.status == doctor.OK
        assert "100" in check.detail


class TestRunnerIntegration:
    def test_sweep_cache_records_use_and_protects_the_job(self, monkeypatch, tmp_path):
        """The runner's claim-time hook: the claimed model lands in the
        journal and in the protect set; the warm model is protected too."""
        from mechbench_runner import job_runner
        from mechbench_runner.config import Config

        recorded, swept = [], []
        monkeypatch.setattr(
            budget, "record_use",
            lambda model, root=None, now=None: recorded.append(model),
        )
        monkeypatch.setattr(
            budget, "sweep",
            lambda *a, **kw: swept.append(kw.get("protect")) or [],
        )
        config = Config(
            api_base_url="http://127.0.0.1:1", api_key="k",
            poll_interval_seconds=0.01, warm_model_id="org/warm@rev",
        )
        runner = job_runner.JobRunner(config)
        runner._sweep_cache({"spec": {"modelId": "org/job@abc"}})
        assert recorded == ["org/job@abc"]
        assert swept == [{"org/warm", "org/job"}]
