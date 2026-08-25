"""Detecting how the runner was installed (task 000296)."""
from __future__ import annotations

from mechbench_runner import install as m


class TestDetect:
    def test_uv_tool(self):
        i = m.detect("/Users/x/.local/share/uv/tools/mechbench")
        assert i.method == "uv-tool"
        # `--reinstall` leaves satisfied dependencies alone -- observed
        # 2026-08-23 with compute stuck a version behind a moved floor.
        assert i.upgrade[-2:] == ["upgrade", "mechbench"]

    def test_pipx(self):
        assert m.detect("/Users/x/.local/pipx/venvs/mechbench").method == "pipx"

    def test_an_unknown_layout_refuses_rather_than_guessing(self):
        i = m.detect("/opt/somewhere/odd")
        assert i.method == "unknown"
        assert i.upgradable is False
        # Refusing has to say what to do instead.
        assert "installed" in i.advice

    def test_a_checkout_is_never_upgraded(self):
        # Asked of the running module, because a stale egg-info in a
        # tree shadows the real dist-info and hides direct_url.json.
        assert m.detect().method == "source"
        assert m.detect().upgradable is False


class TestVersions:
    def test_it_reports_the_three_that_matter(self):
        v = m.installed_versions()
        assert set(v) == {"mechbench", "mechbench-compute", "mechbench-schema"}

    def test_it_never_raises_on_a_missing_package(self, monkeypatch):
        import importlib.metadata as md

        def boom(_n):
            raise md.PackageNotFoundError("nope")

        monkeypatch.setattr(md, "version", boom)
        assert set(m.installed_versions().values()) == {"(absent)"}


class TestRunUpgrade:
    def test_it_refuses_when_there_is_no_command(self):
        i = m.Installation("unknown", None, "do it yourself")
        ok, msg = m.run_upgrade(i)
        assert ok is False and msg == "do it yourself"

    def test_a_missing_binary_is_reported_not_raised(self, monkeypatch):
        def gone(cmd, **_kw):
            raise FileNotFoundError(cmd[0])

        monkeypatch.setattr(m, "_run", gone)
        i = m.Installation("venv", ["/definitely/not/here"], "x")
        ok, msg = m.run_upgrade(i)
        assert ok is False and msg


class TestFindingInstallers:
    """A service does not inherit a login shell's PATH.

    A LaunchAgent runs with PATH=/usr/bin:/bin:/usr/sbin:/sbin — no
    Homebrew, no ~/.local/bin. The first real self-update died on
    FileNotFoundError for `uv` while the identical command worked by
    hand, which is the whole reason this does not trust `which`.
    """

    def test_path_is_tried_first(self, monkeypatch):
        monkeypatch.setattr(m.shutil, "which", lambda n: "/from/path/" + n)
        assert m.find_executable("uv") == "/from/path/uv"

    def test_known_locations_are_searched_when_path_fails(self, monkeypatch, tmp_path):
        monkeypatch.setattr(m.shutil, "which", lambda _n: None)
        fake = tmp_path / "uv"
        fake.write_text("#!/bin/sh\n")
        fake.chmod(0o755)
        monkeypatch.setattr(m, "_SEARCH", (str(tmp_path),))
        assert m.find_executable("uv") == str(fake)

    def test_a_non_executable_file_does_not_count(self, monkeypatch, tmp_path):
        monkeypatch.setattr(m.shutil, "which", lambda _n: None)
        (tmp_path / "uv").write_text("not executable")
        monkeypatch.setattr(m, "_SEARCH", (str(tmp_path),))
        assert m.find_executable("uv") is None

    def test_a_missing_installer_refuses_with_the_command_to_run(self, monkeypatch):
        monkeypatch.setattr(m, "find_executable", lambda _n: None)
        i = m.detect("/Users/x/.local/share/uv/tools/mechbench")
        assert i.method == "uv-tool"
        assert i.upgradable is False
        assert "uv tool upgrade" in i.advice


class TestSuccessIsMeasuredNotAssumed:
    """Exit 0 does not mean anything moved.

    `uv tool upgrade` on a tool installed with an exact pin prints
    "Nothing to upgrade" and exits 0; pip does the same when the
    requirement is already satisfied. Trusting the return code would
    report a successful update that never happened — and then the verify
    step would pass, because the old version does still work.
    """

    def _proc(self, monkeypatch, code=0):
        import subprocess

        monkeypatch.setattr(
            m, "_run",
            lambda cmd, **k: subprocess.CompletedProcess(cmd, code, "Nothing to upgrade", ""),
        )

    def test_unchanged_version_is_a_failure(self, monkeypatch):
        self._proc(monkeypatch)
        monkeypatch.setattr(m, "installed_versions", lambda: {m.DIST: "0.2.0"})
        ok, msg = m.run_upgrade(m.Installation("venv", ["true"], "x"), "0.2.1")
        assert ok is False
        assert "still 0.2.0" in msg

    def test_the_target_landing_is_success(self, monkeypatch):
        self._proc(monkeypatch)
        monkeypatch.setattr(m, "installed_versions", lambda: {m.DIST: "0.2.1"})
        ok, _ = m.run_upgrade(m.Installation("venv", ["true"], "x"), "0.2.1")
        assert ok is True

    def test_no_target_reinstalls_the_whole_env(self, monkeypatch):
        # Targeted self-updates pin the tool at an exact version, and
        # `uv tool upgrade` honors that pin by refusing to move ANYTHING
        # — including a dependency fix the machine actually needs. A
        # no-target update therefore upgrades the whole env: --upgrade moves
        # deps (--reinstall would rebuild them at current versions), and
        # installing unpinned clears the pin (task 000312 follow-up: "already on 0.5.3;
        # nothing to do" while compute sat one fix behind).
        seen: list[list[str]] = []
        import subprocess

        monkeypatch.setattr(
            m, "_run",
            lambda cmd, **k: (seen.append(cmd),
                              subprocess.CompletedProcess(cmd, 0, "", ""))[1],
        )
        monkeypatch.setattr(m, "installed_versions", lambda: {m.DIST: "0.5.3"})
        m.run_upgrade(m.Installation("uv-tool", ["/bin/uv", "tool", "upgrade", m.DIST], "x"))
        assert seen[0] == ["/bin/uv", "tool", "install", "--upgrade", "--refresh", m.DIST]

    def test_uv_is_asked_for_the_exact_version(self, monkeypatch):
        seen: list[list[str]] = []
        import subprocess

        monkeypatch.setattr(
            m, "_run",
            lambda cmd, **k: (seen.append(cmd),
                              subprocess.CompletedProcess(cmd, 0, "", ""))[1],
        )
        monkeypatch.setattr(m, "installed_versions", lambda: {m.DIST: "0.2.1"})
        m.run_upgrade(m.Installation("uv-tool", ["/bin/uv", "tool", "upgrade", m.DIST], "x"), "0.2.1")
        # `upgrade` honours the pin a tool was installed with and refuses
        # to move; naming the version is what actually changes it, and is
        # what a rollback needs.
        assert seen[0] == ["/bin/uv", "tool", "install", "--reinstall",
                           f"{m.DIST}==0.2.1"]
