from __future__ import annotations

import sys
from contextlib import suppress
from datetime import UTC, datetime

from . import credentials, machine
from .api_client import ApiClient, ApiError, register_runner
from .config import DEFAULT_API_URL, Config
from .credentials import StoredCredentials

WEB_HOSTS = {
    "api.mechbench.ai": "https://mechbench.ai",
    "localhost:3000": "http://localhost:5173",
    "127.0.0.1:3000": "http://localhost:5173",
}
SETTINGS_PATH = "/settings/runners"


def web_url(api_base_url: str) -> str:
    trimmed = api_base_url.rstrip("/")
    for scheme in ("https://", "http://"):
        if trimmed.startswith(scheme):
            host = trimmed[len(scheme) :]
            break
    else:
        host = trimmed
    base = WEB_HOSTS.get(host)
    if base is None:
        base = trimmed.replace("//api.", "//", 1)
    return f"{base}{SETTINGS_PATH}"


def login(
    config: Config,
    *,
    token: str | None = None,
    name: str | None = None,
) -> int:
    if not token:
        return _login_via_browser(config, name)
    return _login_with_token(config, token, name)


def _login_with_token(config: Config, token: str, name: str | None) -> int:
    url = web_url(config.api_base_url)

    existing = credentials.load()

    try:
        from . import __version__ as runner_version
    except ImportError:
        runner_version = "unknown"

    try:
        result = register_runner(
            config.api_base_url,
            token=token,
            name=name or machine.default_name(),
            hostname=machine.hostname(),
            platform=machine.describe_platform(),
            runner_version=runner_version,
        )
    except ApiError as exc:
        if exc.status == 401:
            print(
                "That registration token is not valid — they are single-use "
                "and expire after 15 minutes.\n"
                f"Get a fresh one at {url}",
                file=sys.stderr,
            )
            return 1
        if exc.status == 404:
            print(
                f"{config.api_base_url} does not have a runner registration "
                f"endpoint.\n"
                f"That is usually the wrong address — check MECHBENCH_API_URL, "
                f"or drop it to use {DEFAULT_API_URL}.",
                file=sys.stderr,
            )
            return 1
        print(
            f"registration failed against {config.api_base_url}: {exc}",
            file=sys.stderr,
        )
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"could not reach {config.api_base_url}: {exc}", file=sys.stderr)
        return 1

    runner = result.get("runner") or {}
    path = credentials.save(
        StoredCredentials(
            api_url=config.api_base_url,
            api_key=result["apiKey"],
            runner_id=runner.get("id"),
            name=runner.get("name"),
            registered_at=datetime.now(UTC)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z"),
        )
    )
    if existing:
        print(
            f"Replaced the credential for {existing.name or 'this machine'} "
            f"at {existing.api_url}.\n"
            "The previous runner stays registered until it is removed on the "
            "website.\n"
        )
    print(
        f"Connected as \"{runner.get('name')}\" ({runner.get('id')}).\n"
        f"Credentials written to {path} (mode 0600)."
    )
    _offer_service()
    return 0


def _offer_service() -> None:
    from . import service

    try:
        existing = service.status()
    except service.UnsupportedPlatformError:
        print("\nStart working:\n\n    mechbench run\n")
        return

    if existing.installed:
        if service.kickstart():
            print("\nRestarted the background service with the new credentials.")
        else:
            print("\nA background service is installed; restart it to pick up "
                  "the new credentials:\n\n    mechbench install-service\n")
        return

    if not sys.stdin.isatty():
        print(
            "\nStart working:\n\n    mechbench run\n\n"
            "Or have it start automatically and stay running:\n\n"
            "    mechbench install-service\n"
        )
        return

    try:
        answer = input(
            "\nStart automatically at login and keep running? [Y/n] "
        ).strip().lower()
    except (EOFError, KeyboardInterrupt):
        answer = "n"
        print()

    if answer in {"", "y", "yes"}:
        st = service.install()
        print(f"  {st.detail}  ({st.path})")
        hint = service.linger_hint()
        if hint:
            print(f"\n{hint}")
    else:
        print("\nStart working:\n\n    mechbench run\n\n"
              "Change your mind with `mechbench install-service`.\n")


def logout(config: Config) -> int:
    stored = credentials.load()
    if not stored:
        print("this machine is not signed in.")
        return 0

    if stored.runner_id:
        try:
            with ApiClient(config) as api:
                api.revoke_runner(stored.runner_id)
            print(f"Revoked {stored.name or stored.runner_id} on {stored.api_url}.")
        except Exception as exc:  # noqa: BLE001
            print(
                f"warning: could not revoke the key on {stored.api_url} ({exc}).\n"
                f"         Remove this runner at {web_url(stored.api_url)} to be "
                "sure it is dead.",
                file=sys.stderr,
            )

    credentials.clear()
    print("Signed out; credentials removed.")
    return 0


def whoami(config: Config) -> int:
    if not config.api_key:
        print(
            "this machine is not signed in. Run `mechbench login`.",
            file=sys.stderr,
        )
        return 1
    try:
        with ApiClient(config) as api:
            data = api.whoami()
    except ApiError as exc:
        if exc.status == 401:
            print(
                "signed out: this credential has been revoked.\n"
                "Run `mechbench login` to reconnect.",
                file=sys.stderr,
            )
            return 1
        if exc.status == 400:
            print(
                "this credential is a plain API key, not a registered runner.\n"
                "Run `mechbench login` to register this machine.",
                file=sys.stderr,
            )
            return 1
        print(f"whoami failed: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"could not reach {config.api_base_url}: {exc}", file=sys.stderr)
        return 1

    runner = data.get("runner") or {}
    account = data.get("account") or {}
    print(f"machine  {runner.get('name')} ({runner.get('id')})")
    print(f"account  {account.get('handle')}")
    print(f"scope    {data.get('scopeLabel')}")
    print(f"api      {config.api_base_url}")
    print(f"host     {runner.get('hostname')}  {runner.get('platform')}")
    if runner.get("signedOut"):
        print("status   REVOKED — run `mechbench login` to reconnect")
    return 0

def _login_via_browser(config: Config, name: str | None) -> int:
    import time
    import webbrowser

    from .api_client import poll_device_auth, start_device_auth

    try:
        from . import __version__ as runner_version
    except ImportError:
        runner_version = "unknown"

    machine_name = name or machine.default_name()
    try:
        started = start_device_auth(
            config.api_base_url,
            name=machine_name,
            hostname=machine.hostname(),
            platform=machine.describe_platform(),
            runner_version=runner_version,
        )
    except ApiError as exc:
        if exc.status == 404:
            print(
                f"{config.api_base_url} does not support browser sign-in.\n"
                f"Try `mechbench login --token mbr_...` instead.",
                file=sys.stderr,
            )
            return 1
        print(f"could not start sign-in against {config.api_base_url}: {exc}",
              file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"could not reach {config.api_base_url}: {exc}", file=sys.stderr)
        return 1

    verification = started.get("verificationUri") or web_url(config.api_base_url)
    interval = float(started.get("intervalSeconds") or 3)

    print(f'Connecting this machine as "{machine_name}".', flush=True)
    print(f"\nApprove it at:\n\n    {verification}\n", flush=True)

    if sys.stdin.isatty():
        try:
            input("Press ENTER to open your browser (or open the link yourself)…")
        except (EOFError, KeyboardInterrupt):
            print()
            return 1
        with suppress(Exception):
            webbrowser.open(verification)

    print("Waiting for approval… (^C to cancel)", flush=True)
    deadline = time.time() + _seconds_until(started.get("expiresAt"))
    try:
        while time.time() < deadline:
            time.sleep(interval)
            try:
                answer = poll_device_auth(config.api_base_url, started["deviceCode"])
            except Exception:  # noqa: BLE001
                continue
            status = answer.get("status")
            if status == "approved":
                return _store_and_finish(config, answer)
            if status == "denied":
                print("\nThat request was declined.", file=sys.stderr)
                return 1
            if status == "expired":
                break
    except KeyboardInterrupt:
        print("\ncancelled; nothing was connected.")
        return 1

    print(
        "\nThe request expired before it was approved. Run "
        "`mechbench login` again.",
        file=sys.stderr,
    )
    return 1


def _seconds_until(iso: object) -> float:
    if not isinstance(iso, str):
        return 15 * 60
    try:
        when = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return 15 * 60
    return max(0.0, (when - datetime.now(UTC)).total_seconds())


def _store_and_finish(config: Config, answer: dict) -> int:
    runner = answer.get("runner") or {}
    existing = credentials.load()
    path = credentials.save(
        StoredCredentials(
            api_url=config.api_base_url,
            api_key=answer["apiKey"],
            runner_id=runner.get("id"),
            name=runner.get("name"),
            registered_at=datetime.now(UTC)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z"),
        )
    )
    rname = runner.get("name")
    rid = runner.get("id")
    print(f'\nConnected as "{rname}" ({rid}).')
    if existing:
        print(
            f"Replaced the credential for {existing.name or 'this machine'} "
            f"at {existing.api_url}."
        )
    print(f"Credentials written to {path} (mode 0600).")
    _offer_service()
    return 0
