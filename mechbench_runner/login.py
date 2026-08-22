"""Pairing this machine with an account (task 000284).

The flow, and why it is shaped this way: the website shows a
**short-lived registration token**; this machine trades it for a
durable key and stores that key itself. The token expires in minutes
and is single-use, so a copy left in scrollback is worth nothing
tomorrow — and the durable key it becomes is never displayed, so it
never lands in shell history at all.

`login` with no token prints the link and, at a terminal, waits for a
paste. Piped or scripted, it prints the link and stops rather than
blocking forever on stdin nobody is attached to.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone

from . import credentials, machine
from .api_client import ApiClient, ApiError, register_runner
from .config import Config
from .credentials import StoredCredentials

#: Where the copy-paste block lives. Derived from the API host so that a
#: dev runner points at the dev site without a second setting.
WEB_HOSTS = {
    "api.mechbench.ai": "https://mechbench.ai",
    "localhost:3000": "http://localhost:5173",
    "127.0.0.1:3000": "http://localhost:5173",
}
SETTINGS_PATH = "/settings/runners"


def web_url(api_base_url: str) -> str:
    """The page rendering the registration block, for this API."""
    trimmed = api_base_url.rstrip("/")
    for scheme in ("https://", "http://"):
        if trimmed.startswith(scheme):
            host = trimmed[len(scheme) :]
            break
    else:
        host = trimmed
    base = WEB_HOSTS.get(host)
    if base is None:
        # An unrecognized API host: guess the site by dropping the `api.`
        # label, and say so rather than pretending to be sure.
        base = trimmed.replace("//api.", "//", 1)
    return f"{base}{SETTINGS_PATH}"


def login(
    config: Config,
    *,
    token: str | None = None,
    name: str | None = None,
) -> int:
    url = web_url(config.api_base_url)

    if not token:
        # All on stdout, deliberately: piped, stderr is unbuffered and
        # stdout is not, so splitting these two halves across streams
        # prints the second line first. It is guidance, not an error —
        # the non-zero exit is what says nothing was connected.
        print(f"To connect this machine, open:\n\n    {url}\n")
        if not sys.stdin.isatty():
            print("Then run:\n\n    mechbench-runner login --token mbr_...\n")
            return 1
        try:
            token = input("Paste the registration token: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 1
        if not token:
            print("no token given.", file=sys.stderr)
            return 1

    # Read now, report later: announcing a replacement before the token
    # has been validated tells someone their credential is gone when a
    # failed login has in fact left it exactly where it was.
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
        print(f"registration failed: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 — a bad URL should read as one
        print(f"could not reach {config.api_base_url}: {exc}", file=sys.stderr)
        return 1

    runner = result.get("runner") or {}
    path = credentials.save(
        StoredCredentials(
            api_url=config.api_base_url,
            api_key=result["apiKey"],
            runner_id=runner.get("id"),
            name=runner.get("name"),
            registered_at=datetime.now(timezone.utc)
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
    _offer_agent()
    return 0


def _offer_agent() -> None:
    """Offer to start automatically, so the promise really is one command.

    A runner you have to remember to start is one you have to touch
    again. Declining leaves a perfectly good manual runner.
    """
    from . import agent

    try:
        existing = agent.status()
    except agent.UnsupportedPlatformError:
        print("\nStart working:\n\n    mechbench-runner run\n")
        return

    if existing.installed:
        # It exited deliberately when the key was revoked, so the
        # supervisor is correctly leaving it alone. Nothing else will
        # bring it back.
        if agent.kickstart():
            print("\nRestarted the background service with the new credentials.")
        else:
            print("\nA background service is installed; restart it to pick up "
                  "the new credentials:\n\n    mechbench-runner install-agent\n")
        return

    if not sys.stdin.isatty():
        print(
            "\nStart working:\n\n    mechbench-runner run\n\n"
            "Or have it start automatically and stay running:\n\n"
            "    mechbench-runner install-agent\n"
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
        st = agent.install()
        print(f"  {st.detail}  ({st.path})")
        hint = agent.linger_hint()
        if hint:
            print(f"\n{hint}")
    else:
        print("\nStart working:\n\n    mechbench-runner run\n\n"
              "Change your mind with `mechbench-runner install-agent`.\n")


def logout(config: Config) -> int:
    """Forget the stored credential — and revoke it, when we can reach
    the API. A key that only stops being *used* is still a live key."""
    stored = credentials.load()
    if not stored:
        print("this machine is not signed in.")
        return 0

    if stored.runner_id:
        try:
            with ApiClient(config) as api:
                api.revoke_runner(stored.runner_id)
            print(f"Revoked {stored.name or stored.runner_id} on {stored.api_url}.")
        except Exception as exc:  # noqa: BLE001 — local forget still proceeds
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
            "this machine is not signed in. Run `mechbench-runner login`.",
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
                "Run `mechbench-runner login` to reconnect.",
                file=sys.stderr,
            )
            return 1
        if exc.status == 400:
            print(
                "this credential is a plain API key, not a registered runner.\n"
                "Run `mechbench-runner login` to register this machine.",
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
        print("status   REVOKED — run `mechbench-runner login` to reconnect")
    return 0
