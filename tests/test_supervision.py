"""The exit contract, the watchdog, and bounded logs (task 000294).

These are what a supervisor sees. An OS supervisor understands nothing
about this process except the code it exits with, so the codes are the
contract and deserve tests that name the consequence rather than the
value.
"""

from __future__ import annotations

import sys
import time

from mechbench_runner import logs
from mechbench_runner.exits import EXIT_CRASH, EXIT_OK, EXIT_RESTART
from mechbench_runner.watchdog import Watchdog


class TestExitContract:
    def test_deliberate_is_zero_so_a_supervisor_leaves_it_stopped(self):
        # KeepAlive{SuccessfulExit:false} and Restart=on-failure both key
        # off exactly this.
        assert EXIT_OK == 0

    def test_a_fault_is_non_zero_so_it_comes_back(self):
        assert EXIT_CRASH != 0

    def test_an_asked_for_restart_is_non_zero_but_distinct(self):
        # Non-zero so it restarts; distinct so a log says why (000296).
        assert EXIT_RESTART != 0
        assert EXIT_RESTART != EXIT_CRASH


class TestWatchdog:
    def test_a_fresh_stamp_is_not_a_stall(self):
        w = Watchdog(stall_seconds=10)
        w.stamp()
        assert w.idle_seconds() < 1

    def test_idle_grows_without_stamps(self):
        w = Watchdog(stall_seconds=10)
        time.sleep(0.05)
        assert w.idle_seconds() >= 0.05

    def test_pause_covers_a_wait_we_cannot_bound(self):
        w = Watchdog(stall_seconds=10)
        w.pause()
        time.sleep(0.05)
        assert w.idle_seconds() == 0.0
        w.resume()

    def test_it_kills_the_process_rather_than_the_thread(self, monkeypatch):
        """The whole point: the rest of the process is wedged and will
        not unwind, so sys.exit from a thread would achieve nothing."""
        killed: list[int] = []
        monkeypatch.setattr(
            "mechbench_runner.watchdog.os._exit", lambda code: killed.append(code)
        )
        reported: list[float] = []
        w = Watchdog(
            stall_seconds=0.01, on_stall=reported.append, exit_code=EXIT_CRASH
        )
        w._die(99.0)  # noqa: SLF001
        assert killed == [EXIT_CRASH]
        assert reported == [99.0]

    def test_a_failing_report_does_not_stop_it_dying(self, monkeypatch):
        killed: list[int] = []
        monkeypatch.setattr(
            "mechbench_runner.watchdog.os._exit", lambda code: killed.append(code)
        )

        def boom(_idle):
            raise RuntimeError("the channel is gone too")

        Watchdog(stall_seconds=0.01, on_stall=boom)._die(1.0)  # noqa: SLF001
        assert killed == [1]

    def test_zero_disables_it(self):
        w = Watchdog(stall_seconds=0)
        w.start()
        assert w._thread is None  # noqa: SLF001


class TestRotatingLog:
    def test_it_rotates_at_the_cap(self, tmp_path):
        path = tmp_path / "runner.log"
        w = logs.RotatingWriter(path, max_bytes=200, keep=2)
        for i in range(60):
            w.write(f"line {i} " + "x" * 20 + "\n")
        w.flush()
        assert path.exists()
        assert path.with_suffix(".log.1").exists()

    def test_it_keeps_only_what_it_promised(self, tmp_path):
        path = tmp_path / "runner.log"
        w = logs.RotatingWriter(path, max_bytes=100, keep=2)
        for i in range(200):
            w.write(f"{i} " + "y" * 40 + "\n")
        w.flush()
        # runner.log + .1 + .2, and nothing past the cap.
        assert not path.with_suffix(".log.3").exists()
        rolled = sorted(p.name for p in tmp_path.iterdir())
        assert rolled == ["runner.log", "runner.log.1", "runner.log.2"]

    def test_the_total_stays_bounded(self, tmp_path):
        """The property that matters on a machine nobody looks at."""
        path = tmp_path / "runner.log"
        w = logs.RotatingWriter(path, max_bytes=1000, keep=2)
        for i in range(5000):
            w.write(f"{i} " + "z" * 50 + "\n")
        w.flush()
        total = sum(p.stat().st_size for p in tmp_path.iterdir())
        # Three files, each capped: finite, which is the whole
        # difference from what launchd does on its own.
        assert total < 1000 * 4

    def test_it_survives_the_terminal_going_away(self, tmp_path):
        class Dead:
            def write(self, _t):
                raise ValueError("I/O operation on closed file")

            def flush(self):
                pass

        path = tmp_path / "runner.log"
        w = logs.RotatingWriter(path, mirror=Dead())
        w.write("still recorded\n")
        w.flush()
        assert "still recorded" in path.read_text()
        assert w.mirror is None

    def test_install_and_uninstall_restore_the_streams(self, tmp_path, monkeypatch):
        monkeypatch.setattr(logs, "log_dir", lambda: tmp_path)
        real = sys.stdout
        w = logs.install(name="t.log", mirror=False)
        print("captured")
        assert sys.stdout is w
        logs.uninstall()
        assert sys.stdout is not w
        assert "captured" in (tmp_path / "t.log").read_text()
        sys.stdout = real
