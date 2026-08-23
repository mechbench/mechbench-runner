"""`doctor` (task 000285).

What is worth testing is the verdicts and the advice attached to them:
doctor exists so a person reads one line instead of a stack trace, and a
check that says FAIL without saying what to do has not done its job.
"""

from __future__ import annotations

import sys
from collections import namedtuple

from mechbench_runner import doctor
from mechbench_runner.api_client import ApiError
from mechbench_runner.config import Config
from mechbench_runner.doctor import FAIL, OK, WARN, Check

# shutil.disk_usage and sys.version_info both return named tuples, and
# the code reads them by attribute; stand-ins have to do the same.
Usage = namedtuple("Usage", "total used free")
Version = namedtuple("Version", "major minor micro releaselevel serial")


def config(**kw) -> Config:
    base = {
        "api_base_url": "http://localhost:3000",
        "api_key": "mbk_x",
        "poll_interval_seconds": 2.0,
        "warm_model_id": None,
        "from_stored_credentials": True,
    }
    base.update(kw)
    return Config(**base)


class FakeApi:
    def __init__(self, result=None, error=None):
        self.result, self.error = result, error

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def whoami(self):
        if self.error:
            raise self.error
        return self.result


class TestRendering:
    def test_advice_is_shown_for_a_failure(self):
        line = Check("x", FAIL, "broken", "do the thing").render()
        assert "✗" in line
        assert "→ do the thing" in line

    def test_advice_is_withheld_when_there_is_nothing_to_fix(self):
        line = Check("x", OK, "fine", "do the thing").render()
        assert "→" not in line


class TestMachineChecks:
    def test_python_version(self):
        assert doctor._python().status == OK  # noqa: SLF001

    def test_an_old_python_says_which_version_is_needed(self, monkeypatch):
        monkeypatch.setattr(sys, "version_info", Version(3, 9, 0, "final", 0))
        check = doctor._python()  # noqa: SLF001
        assert check.status == FAIL
        assert "3.11" in check.detail

    def test_no_backend_names_the_platforms_that_would_work(self, monkeypatch):
        import importlib.util

        real = importlib.util.find_spec
        monkeypatch.setattr(
            importlib.util,
            "find_spec",
            lambda n, *a, **k: None if n.split(".")[0] == "mlx" else real(n, *a, **k),
        )
        check = doctor._backend()  # noqa: SLF001
        assert check.status == FAIL
        assert "Apple Silicon" in (check.fix or "")

    def test_a_backend_reports_its_version(self):
        check = doctor._backend()  # noqa: SLF001
        assert check.status == OK
        assert "MLX" in check.detail


class TestAccountChecks:
    def test_not_signed_in_says_how_to_sign_in(self, monkeypatch):
        monkeypatch.setattr(doctor.credentials, "load", lambda: None)
        checks = doctor._account(config(api_key=None))  # noqa: SLF001
        assert checks[0].status == FAIL
        assert "login" in (checks[0].fix or "")

    def test_a_revoked_key_is_named_as_such(self, monkeypatch):
        monkeypatch.setattr(doctor.credentials, "load", lambda: None)
        monkeypatch.setattr(
            doctor, "ApiClient", lambda _c: FakeApi(error=ApiError(401, "no"))
        )
        checks = doctor._account(config())  # noqa: SLF001
        api = checks[-1]
        assert api.status == FAIL
        assert "signed out" in (api.fix or "").lower()

    def test_a_plain_key_is_a_warning_not_a_failure(self, monkeypatch):
        # Jobs still work with a hand-minted key; it just has no machine
        # behind it, so the website cannot show it.
        monkeypatch.setattr(doctor.credentials, "load", lambda: None)
        monkeypatch.setattr(
            doctor, "ApiClient", lambda _c: FakeApi(error=ApiError(400, "nope"))
        )
        assert doctor._account(config())[-1].status == WARN  # noqa: SLF001

    def test_an_unreachable_api_names_the_url_it_tried(self, monkeypatch):
        monkeypatch.setattr(doctor.credentials, "load", lambda: None)
        monkeypatch.setattr(
            doctor, "ApiClient", lambda _c: FakeApi(error=OSError("refused"))
        )
        check = doctor._account(config(api_base_url="http://nope:1"))[-1]  # noqa: SLF001
        assert check.status == FAIL
        assert "nope:1" in (check.fix or "")

    def test_a_healthy_account_reports_machine_and_scope(self, monkeypatch):
        monkeypatch.setattr(doctor.credentials, "load", lambda: None)
        monkeypatch.setattr(
            doctor,
            "ApiClient",
            lambda _c: FakeApi(
                result={
                    "runner": {"name": "office desktop"},
                    "account": {"handle": "benji"},
                    "scopeLabel": "benji (personal)",
                }
            ),
        )
        check = doctor._account(config())[-1]  # noqa: SLF001
        assert check.status == OK
        assert "office desktop" in check.detail
        assert "benji" in check.detail


class TestDisk:
    def test_plenty_is_fine(self, monkeypatch):
        monkeypatch.setattr(
            doctor.shutil, "disk_usage", lambda _p: Usage(0, 0, 500_000_000_000)
        )
        assert doctor._disk().status == OK  # noqa: SLF001

    def test_a_nearly_full_disk_fails_before_a_download_does(self, monkeypatch):
        monkeypatch.setattr(
            doctor.shutil, "disk_usage", lambda _p: Usage(0, 0, 5_000_000_000)
        )
        check = doctor._disk()  # noqa: SLF001
        assert check.status == FAIL
        assert "10-25 GB" in (check.fix or "")

    def test_room_for_one_more_is_a_warning(self, monkeypatch):
        monkeypatch.setattr(
            doctor.shutil, "disk_usage", lambda _p: Usage(0, 0, 40_000_000_000)
        )
        assert doctor._disk().status == WARN  # noqa: SLF001


class TestExitCode:
    def _quiet_service(self, monkeypatch):
        from mechbench_runner import service

        monkeypatch.setattr(service, "status",
                            lambda: service.ServiceStatus(
                                False, False, False, None, "not installed"))

    def test_a_failure_exits_non_zero(self, monkeypatch, capsys):
        self._quiet_service(monkeypatch)
        monkeypatch.setattr(doctor.credentials, "load", lambda: None)
        code = doctor.run(config(api_key=None))
        assert code == 1
        assert "problem" in capsys.readouterr().out

    def test_a_healthy_machine_exits_zero(self, monkeypatch, capsys):
        self._quiet_service(monkeypatch)
        monkeypatch.setattr(doctor.credentials, "load", lambda: None)
        monkeypatch.setattr(
            doctor,
            "ApiClient",
            lambda _c: FakeApi(
                result={
                    "runner": {"name": "m"},
                    "account": {"handle": "b"},
                    "scopeLabel": "b (personal)",
                }
            ),
        )
        monkeypatch.setattr(
            doctor.shutil, "disk_usage", lambda _p: Usage(0, 0, 500_000_000_000)
        )
        assert doctor.run(config()) == 0
        assert "Ready to run jobs" in capsys.readouterr().out


class TestServiceCheck:
    """A disabled background item is silent otherwise.

    macOS names background items after whoever signed the executable —
    "Ned Deily" for any python.org-derived interpreter. Turning that off
    in Login Items stops the runner, and before this check `doctor`
    would still have reported a perfectly healthy machine.
    """

    def _status(self, monkeypatch, **kw):
        from pathlib import Path

        from mechbench_runner import service

        base = {"installed": True, "loaded": True, "running": True,
                "path": Path("/x.plist"), "detail": "running (pid 1)"}
        base.update(kw)
        monkeypatch.setattr(service, "status", lambda: service.ServiceStatus(**base))

    def test_running_is_fine(self, monkeypatch):
        self._status(monkeypatch)
        assert doctor._service().status == OK  # noqa: SLF001

    def test_installed_but_stopped_warns_and_names_the_cause(self, monkeypatch):
        self._status(monkeypatch, running=False, detail="loaded, not running")
        check = doctor._service()  # noqa: SLF001
        assert check.status == WARN
        # The fix is only findable if the warning says whose name to look for.
        assert "Ned Deily" in (check.fix or "")
        assert "Login Items" in (check.fix or "")

    def test_not_installed_is_not_a_problem(self, monkeypatch):
        self._status(monkeypatch, installed=False, loaded=False, running=False,
                     detail="not installed")
        check = doctor._service()  # noqa: SLF001
        assert check.status == OK
        assert "install-service" in (check.fix or "")
