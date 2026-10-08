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


class TestExtras:
    COMPUTE = [
        "numpy>=1.26",
        "mlx>=0.20; sys_platform == 'darwin' and platform_machine == 'arm64'",
        'torch>=2.4; extra == "torch"',
        'nnsight<0.9,>=0.7; extra == "torch"',
        'pytest>=8; extra == "dev"',
    ]

    def _env(self, monkeypatch, versions, requires=None):
        import importlib.metadata as md

        reqs = {"mechbench-compute": self.COMPUTE, **(requires or {})}

        def version(name):
            if name not in versions:
                raise md.PackageNotFoundError(name)
            return versions[name]

        def requires_of(name):
            if name not in versions:
                raise md.PackageNotFoundError(name)
            return reqs.get(name, [])

        class Meta:
            def get_all(self, key):
                return ["torch", "dev"] if key == "Provides-Extra" else []

        monkeypatch.setattr(md, "version", version)
        monkeypatch.setattr(md, "requires", requires_of)
        monkeypatch.setattr(md, "metadata", lambda name: Meta())

    def test_an_extra_is_installed_when_every_requirement_it_adds_is(self, monkeypatch):
        self._env(monkeypatch, {"mechbench-compute": "0.196.0", "numpy": "2.0",
                                "torch": "2.14.1", "nnsight": "0.8.2"})
        assert [str(r) for r in m.requirements_of("mechbench-compute", "torch")] == [
            'torch>=2.4; extra == "torch"', 'nnsight<0.9,>=0.7; extra == "torch"']
        assert m.installed_extras() == ["torch"]
        assert m.unmet_extras() == {"torch": []}
        self._env(monkeypatch, {"mechbench-compute": "0.196.0", "numpy": "2.0"})
        assert m.installed_extras() == []

    def test_what_an_extra_needs_and_lacks_is_named_down_its_closure(self, monkeypatch):
        self._env(monkeypatch, {"mechbench-compute": "0.196.0", "torch": "2.14.1",
                                "nnsight": "0.9.1", "sympy": "1.12"},
                  requires={"torch": ["sympy>=1.13.3", "filelock"]})
        assert m.unmet("mechbench-compute", "torch") == [
            'nnsight<0.9,>=0.7; extra == "torch" (0.9.1 installed)',
            "sympy>=1.13.3 (1.12 installed)", "filelock (absent)"]
