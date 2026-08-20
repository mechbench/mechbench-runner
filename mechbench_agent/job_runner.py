"""Job-runner subsystem: poll mechbench-api, execute, report back.

Ports `mechbench-experiments/bin/run_local_agent.py` into the agent
package. Operationally the same loop — claim a job via
`GET /jobs/next`, execute in-process, post result bytes with their
sha256 — but now running under `mechbench-agent run` as a supported
subcommand rather than a throwaway `bin/` script.

Intentionally synchronous and single-tenant. Concurrency,
heartbeats, and remote dispatch are deferred (see epic 000178's
Not-in-scope list and mechbench-remote's own scope).
"""

from __future__ import annotations

import hashlib
import signal
import time
import traceback
from types import FrameType
from typing import Any

from mechbench_schema import dump_canonical

from .api_client import ApiClient, ApiError
from .config import Config
from .experiment_runner import ExperimentRunner, ExperimentSpec

BACKOFF_MAX_SECONDS = 30.0


class JobRunner:
    def __init__(self, config: Config) -> None:
        self.config = config
        self._shutdown = False
        self._runner = ExperimentRunner()

    def install_sigint_handler(self) -> None:
        def _handler(_signum: int, _frame: FrameType | None) -> None:
            self._shutdown = True
            print("\n[agent] SIGINT received; exiting after current job.")

        signal.signal(signal.SIGINT, _handler)

    def run(self) -> None:
        self.install_sigint_handler()
        print("[agent] loading model (first call is slow)...")
        # Warm the model so the first claimed job doesn't pay cold-start
        # cost — the CONFIGURED default (MECHBENCH_DEFAULT_MODEL_ID, which
        # may carry a @revision pin), never Model.load()'s unpinned
        # built-in: an unpinned warm-up resolves upstream's current
        # revision, which drifts out from under the local mlx stack.
        self._runner._model_loaded(self.config.default_model_id)  # noqa: SLF001
        print("[agent] model loaded; polling.")

        with ApiClient(self.config) as api:
            backoff = self.config.poll_interval_seconds
            while not self._shutdown:
                try:
                    job = api.claim_next_job()
                except ApiError as e:
                    print(f"[agent] /jobs/next error ({e}); "
                          f"retrying in {backoff:.0f}s")
                    time.sleep(backoff)
                    backoff = min(backoff * 2, BACKOFF_MAX_SECONDS)
                    continue
                except Exception as e:  # noqa: BLE001 — surface + keep looping
                    print(f"[agent] API unreachable ({e}); "
                          f"retrying in {backoff:.0f}s")
                    time.sleep(backoff)
                    backoff = min(backoff * 2, BACKOFF_MAX_SECONDS)
                    continue

                backoff = self.config.poll_interval_seconds

                if job is None:
                    time.sleep(self.config.poll_interval_seconds)
                    continue

                try:
                    self._handle(api, job)
                except Exception as exc:  # noqa: BLE001 — report + continue
                    traceback.print_exc()
                    self._report_error(api, job, exc)

    def _handle(self, api: ApiClient, job: dict[str, Any]) -> None:
        job_id = job["id"]
        kind = job["experimentKind"]
        spec_dict = job.get("spec") or {}
        prompt = spec_dict.get("prompt") or ""
        model_id = spec_dict.get("modelId") or self.config.default_model_id

        if kind == "layer_ablation" and not prompt:
            raise ValueError(f"job {job_id}: spec.prompt missing or empty")

        print(f"[agent] running {job_id} kind={kind}")
        spec = ExperimentSpec(kind=kind, prompt=prompt, model_id=model_id,
                              extra={**spec_dict,
                                     "resultPath": job.get("resultPath")})
        # Secret lifecycle (000266): claim-delivered credentials are
        # held in memory, passed explicitly, and disposed in the
        # finally below — never env, never specs, never logs.
        secrets = job.get("integrations") or {}

        def on_progress(done: int, total: int) -> None:
            # Throttle: report every 5th unit and the final one. Progress
            # is cosmetic — a failed PATCH must never fail the job.
            if done % 5 != 0 and done != total:
                return
            try:
                api.report_progress(job_id, done, total)
            except Exception as e:  # noqa: BLE001 — best-effort by design
                print(f"[agent] progress report failed ({e}); continuing")

        try:
            payload = self._runner.run(spec, on_progress=on_progress,
                                       secrets=secrets)
            if hasattr(payload, "model_dump"):
                payload = payload.model_dump(mode="json")

            cbor_bytes = dump_canonical(payload)
            digest = hashlib.sha256(cbor_bytes).hexdigest()
            api.complete_job_cbor(job_id, cbor_bytes, f"sha256:{digest}")
            print(f"[agent] {job_id} done ({len(cbor_bytes)} CBOR bytes)")
        finally:
            secrets.clear()
            job.pop("integrations", None)

    def _report_error(
        self, api: ApiClient, job: dict[str, Any], exc: Exception
    ) -> None:
        job_id = job.get("id")
        if not job_id:
            return
        import re as _re
        message = _re.sub(r"hf_[A-Za-z0-9]{8,}", "hf_[redacted]", str(exc))
        try:
            api.fail_job(job_id, message)
        except Exception:  # noqa: BLE001 — best-effort
            print(f"[agent] failed to report failure for {job_id}")
