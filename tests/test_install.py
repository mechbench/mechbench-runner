from __future__ import annotations

from mechbench_runner import install as m


class TestDetect:
    def test_uv_tool(self, monkeypatch):
        monkeypatch.setattr(m, "find_executable", lambda name: f"/stub/{name}")
        i = m.detect("/Users/x/.local/share/uv/tools/mechbench")
        assert i.method == "uv-tool"
        assert i.upgrade[-2:] == ["upgrade", "mechbench"]

    def test_pipx(self):
        assert m.detect("/Users/x/.local/pipx/venvs/mechbench").method == "pipx"

    def test_an_unknown_layout_refuses_rather_than_guessing(self):
        i = m.detect("/opt/somewhere/odd")
        assert i.method == "unknown"
        assert i.upgradable is False
        assert "installed" in i.advice

    def test_a_checkout_is_never_upgraded(self):
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
        assert seen[0] == ["/bin/uv", "tool", "install", "--reinstall",
                           f"{m.DIST}==0.2.1"]
