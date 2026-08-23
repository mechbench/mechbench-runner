"""The parent that owns the runner's lifecycle (000295, 000296).

Its whole justification is that a parent can upgrade a stopped child,
so the child never replaces its own running code. What is worth testing
is that it honours the same exit-code contract the OS supervisor does —
if the two levels disagree, a deliberately stopped runner gets restarted
forever, which is the failure this design exists to avoid.
"""

from __future__ import annotations

import pytest

from mechbench_runner.exits import EXIT_CRASH, EXIT_OK, EXIT_RESTART
from mechbench_runner.supervisor import Supervisor


class Fake(Supervisor):
    """A supervisor whose child is a scripted list of exit codes."""

    def __init__(self, codes, lived=999.0):
        super().__init__(argv=["/bin/true"])
        self.codes = list(codes)
        self.lived = lived
        self.runs = 0
        self.upgrades: list[str] = []
        self.rollbacks = 0

    def _run_child(self):  # noqa: D102
        self.runs += 1
        if not self.codes:
            self._stopping = True
            return EXIT_OK
        return self.codes.pop(0)

    def _take_pending_update(self):  # noqa: D102
        return bool(self.upgrades and self.upgrades.pop(0))

    def _roll_back(self):  # noqa: D102
        self.rollbacks += 1
        return True


@pytest.fixture(autouse=True)
def _no_sleeping(monkeypatch):
    monkeypatch.setattr("mechbench_runner.supervisor.time.sleep", lambda _s: None)
    monkeypatch.setattr("mechbench_runner.supervisor.time.monotonic",
                        lambda: 0.0)


class TestExitContract:
    def test_a_deliberate_stop_stops_the_supervisor_too(self):
        """Signed out, or SIGTERM'd. Restarting would defeat the point."""
        s = Fake([EXIT_OK])
        assert s.run() == EXIT_OK
        assert s.runs == 1

    def test_a_crash_is_restarted(self):
        s = Fake([EXIT_CRASH, EXIT_OK])
        assert s.run() == EXIT_OK
        assert s.runs == 2

    def test_an_asked_for_restart_comes_straight_back(self):
        s = Fake([EXIT_RESTART, EXIT_OK])
        assert s.run() == EXIT_OK
        assert s.runs == 2

    def test_it_gives_up_rather_than_looping(self):
        # A supervisor that retries forever turns a broken install into a
        # hot loop nobody notices.
        s = Fake([EXIT_CRASH] * 10)
        assert s.run() == EXIT_CRASH
        assert s.runs == 5


class TestUpgradeAndRollback:
    def test_a_child_that_will_not_start_after_an_upgrade_is_rolled_back(self):
        """The parent watched both, so it knows — no marker needed.

        This is the simplification the whole design is for: the child
        never has to record what it was, because something outside it
        saw the before and the after.
        """
        s = Fake([EXIT_CRASH, EXIT_CRASH, EXIT_OK])
        s.upgrades = [True]
        assert s.run() == EXIT_OK
        assert s.rollbacks == 1

    def test_a_crash_unrelated_to_an_upgrade_is_not_rolled_back(self):
        s = Fake([EXIT_CRASH, EXIT_CRASH, EXIT_OK])
        s.upgrades = [False]
        s.run()
        assert s.rollbacks == 0

    def test_an_update_request_does_not_count_as_a_crash(self):
        # Otherwise a few updates in a row would trip the crash limit.
        s = Fake([EXIT_RESTART] * 8 + [EXIT_OK])
        assert s.run() == EXIT_OK
        assert s.rollbacks == 0


class TestChildCommand:
    def test_it_runs_the_module_not_a_console_script(self):
        # A script's location depends on PATH, and a service has almost
        # none -- the same trap that broke self-update on 2026-08-22.
        argv = Supervisor().child_argv
        assert argv[1:3] == ["-m", "mechbench_runner.cli"]
        assert argv[3] == "run"


class TestProof:
    """An upgrade is proven by a child that stays up, not by a return code.

    The marker is kept only until something demonstrates the new version
    starts. Clearing it too early loses the rollback target; never
    clearing it means a crash months later would 'roll back' an upgrade
    that has been working the whole time.
    """

    def test_a_long_lived_child_settles_the_upgrade(self, monkeypatch):
        times = iter([0.0, 999.0, 0.0, 0.0])
        monkeypatch.setattr("mechbench_runner.supervisor.time.monotonic",
                            lambda: next(times))
        s = Fake([1, EXIT_OK])
        s.upgrades = [True]
        s.run()
        # It ran long enough to count as started, so a later crash is
        # not blamed on the upgrade.
        assert s.rollbacks == 0

    def test_an_immediately_crashing_child_does_not(self, monkeypatch):
        monkeypatch.setattr("mechbench_runner.supervisor.time.monotonic",
                            lambda: 0.0)
        s = Fake([1, 1, EXIT_OK])
        s.upgrades = [True]
        s.run()
        assert s.rollbacks == 1
