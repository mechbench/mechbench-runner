from __future__ import annotations

import dataclasses
import sys

from . import credentials
from .api_client import ApiClient, ApiError
from .config import Config


def for_verbs(config: Config) -> Config:
    if not config.from_stored_credentials:
        return config
    stored = credentials.load()
    if stored is None:
        return config
    key = stored.cli_key or _heal(config, stored)
    return dataclasses.replace(config, api_key=key) if key else config


def _heal(config: Config, stored: credentials.StoredCredentials) -> str | None:
    try:
        with ApiClient(config) as api:
            got = api.request_cli_key()
    except ApiError as exc:
        if exc.status == 409:
            print("this machine's CLI key was issued already and is not stored "
                  "here; run `mechbench login` for a new one, or set "
                  "MECHBENCH_API_KEY", file=sys.stderr)
        return None
    except Exception:  # noqa: BLE001
        return None
    key = got.get("cliKey")
    if not isinstance(key, str) or not key:
        return None
    path = credentials.save(dataclasses.replace(stored, cli_key=key))
    print(f"[mechbench] this machine held only its runner's key; stored its CLI "
          f"key ({got.get('name') or 'cli'}) in {path}", file=sys.stderr)
    return key
