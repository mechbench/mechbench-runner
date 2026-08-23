"""Detecting how the runner was installed (task 000296)."""
from __future__ import annotations

from mechbench_runner import install as m


class TestDetect:
    def test_uv_tool(self):
        i = m.detect("/Users/x/.local/share/uv/tools/mechbench-runner")
        assert i.method == "uv-tool"
        # `--reinstall` leaves satisfied dependencies alone -- observed
        # 2026-08-23 with compute stuck a version behind a moved floor.
        assert i.upgrade[-2:] == ["upgrade", "mechbench-runner"]

    def test_pipx(self):
        assert m.detect("/Users/x/.local/pipx/venvs/mechbench-runner").method == "pipx"

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
        assert set(v) == {"mechbench-runner", "mechbench-compute", "mechbench-schema"}

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

    def test_a_missing_binary_is_reported_not_raised(self):
        i = m.Installation("venv", ["/definitely/not/here"], "x")
        ok, msg = m.run_upgrade(i)
        assert ok is False and msg
