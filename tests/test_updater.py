"""Self-update (task 000296).

The worst outcome this feature can produce is a machine that needed no
attention until it bricked itself. So the assertions here are mostly
about failure: that a bad upgrade is undone, that a broken install does
not loop against the supervisor, and that a checkout is never touched.
"""

from __future__ import annotations

import pytest

from mechbench_runner import install as install_mod
from mechbench_runner import updater


@pytest.fixture()
def state(tmp_path):
    return tmp_path / "update.json"


class TestState:
    def test_round_trip(self, state):
        updater.request("0.2.0", "0.1.0", state)
        st = updater.load(state)
        assert (st.stage, st.target, st.previous) == ("requested", "0.2.0", "0.1.0")

    def test_absent_is_none(self, state):
        assert updater.load(state) is None

    def test_garbage_reads_as_no_update(self, state):
        # Never let an unreadable file wedge startup.
        state.write_text("{{{ not json")
        assert updater.load(state) is None
        state.write_text('{"nope": 1}')
        assert updater.load(state) is None

    def test_clear_is_idempotent(self, state):
        updater.request("0.2.0", "0.1.0", state)
        updater.clear(state)
        updater.clear(state)
        assert updater.load(state) is None


class TestNoPendingUpdate:
    def test_does_nothing(self, state):
        assert updater.take_pending_step(path=state) is False


class TestRefusals:
    def test_a_source_checkout_is_never_upgraded(self, state, monkeypatch, capsys):
        """`git pull` is the developer's to run, not ours."""
        updater.request("0.2.0", "0.1.0", state)
        monkeypatch.setattr(install_mod, "detect", lambda prefix=None:
                            install_mod.Installation("source", None, "use git pull"))
        assert updater.take_pending_step(path=state) is False
        assert updater.load(state) is None          # not left pending forever
        assert "cannot self-upgrade" in capsys.readouterr().out

    def test_it_gives_up_rather_than_looping(self, state, capsys):
        # A supervisor restarts us as fast as we exit; a third attempt
        # would be a loop, not a retry.
        updater.UpdateState(stage="requested", target="0.2.0", previous="0.1.0",
                            attempts=updater.MAX_ATTEMPTS,
                            error="boom").save(state)
        assert updater.take_pending_step(path=state) is False
        out = capsys.readouterr().out
        assert "giving up" in out and "Staying on 0.1.0" in out
        assert updater.load(state) is None


class TestVerifyAndRollback:
    def _installed(self, monkeypatch, ok: bool):
        monkeypatch.setattr(install_mod, "detect", lambda prefix=None:
                            install_mod.Installation("venv", ["true"], "x"))
        calls: list[str] = []
        monkeypatch.setattr(install_mod, "run_upgrade",
                            lambda w, t=None: (calls.append(t or ""), (True, ""))[1])
        monkeypatch.setattr(updater, "_self_check",
                            lambda: [] if ok else ["import failed: boom"])
        return calls

    def test_a_working_upgrade_is_kept(self, state, monkeypatch, capsys):
        updater.UpdateState("verify", "0.2.0", "0.1.0", attempts=1).save(state)
        self._installed(monkeypatch, ok=True)
        assert updater.take_pending_step(path=state) is False
        assert updater.load(state) is None
        assert "now on" in capsys.readouterr().out

    def test_a_broken_upgrade_rolls_back(self, state, monkeypatch, capsys):
        updater.UpdateState("verify", "0.2.0", "0.1.0", attempts=1).save(state)
        calls = self._installed(monkeypatch, ok=False)
        monkeypatch.setattr(updater, "_reexec", lambda say: True)
        assert updater.take_pending_step(path=state) is True
        # It reinstalled the version it came from.
        assert "0.1.0" in calls
        st = updater.load(state)
        assert st.stage == "rollback"
        assert "does not work here" in capsys.readouterr().out

    def test_rollback_reports_and_stops(self, state, monkeypatch, capsys):
        updater.UpdateState("rollback", "0.2.0", "0.1.0", attempts=1).save(state)
        self._installed(monkeypatch, ok=True)
        assert updater.take_pending_step(path=state) is False
        assert updater.load(state) is None
        assert "abandoned" in capsys.readouterr().out

    def test_a_rollback_that_also_fails_says_so(self, state, monkeypatch, capsys):
        updater.UpdateState("rollback", "0.2.0", "0.1.0", attempts=1).save(state)
        self._installed(monkeypatch, ok=False)
        updater.take_pending_step(path=state)
        assert "still fails" in capsys.readouterr().out
        assert updater.load(state) is None   # never wedged


class TestSelfCheck:
    def test_it_passes_on_a_working_install(self):
        assert updater._self_check() == []  # noqa: SLF001
