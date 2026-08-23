"""Job-runner subsystem: poll mechbench-api, execute, report back.

Claim a job via `GET /jobs/next`, execute it in-process, and post the
result bytes with their sha256.

Intentionally synchronous and single-tenant: one job at a time, on one
machine. Running more than one machine is what the registry is for, and
scaling within a machine is a question for when a single one is the
bottleneck.
"""

from __future__ import annotations

import hashlib
import signal
import time
import traceback
from contextlib import suppress
from types import FrameType
from typing import Any

from mechbench_compute.protocol import ProtocolExecutor, ProtocolSpec
from mechbench_schema import dump_canonical

from .api_client import ApiClient, ApiError
from .channel import LiveChannel
from .config import Config
from .control import ControlServer, RunnerState, probe, socket_path
from .exits import EXIT_CRASH, EXIT_OK
from .watchdog import Watchdog

BACKOFF_MAX_SECONDS = 30.0


def _binding_model(spec: dict[str, Any]) -> str | None:
    """The model a pipeline run bound, for display only.

    Run bindings fill the graph's holes; conventionally the model one is
    called "model". This is a label for the board, never an input to
    execution — the graph decides what actually loads.
    """
    bindings = spec.get("bindings")
    if not isinstance(bindings, dict):
        return None
    value = bindings.get("model")
    return value if isinstance(value, str) and value else None


class JobRunner:
    def __init__(self, config: Config) -> None:
        self.config = config
        self._shutdown = False
        # Set while a job is in flight so the download callbacks, which the
        # compute layer calls with no idea a job exists, can report against it.
        self._active_job: str | None = None
        self._active_api: ApiClient | None = None
        self._last_byte_report = 0.0
        self._executor = ProtocolExecutor(
            on_download=self._announce_download,
            on_download_bytes=self._announce_download_bytes,
        )
        try:
            from . import __version__ as runner_version
        except ImportError:  # version is optional metadata, not a dependency
            runner_version = "unknown"
        self.state = RunnerState(version=runner_version, api_url=config.api_base_url)
        self._control = ControlServer(self.state)
        # The live channel is best-effort by construction: it dials out on
        # its own thread and a runner with no channel at all claims and
        # finishes jobs exactly as before (task 000289).
        self._channel = LiveChannel(config, self.state)
        # Nothing outside this process can tell a wedged forward pass from
        # a slow one, so it has to notice for itself (task 000294).
        self._watchdog = Watchdog(
            stall_seconds=config.watchdog_seconds,
            on_stall=self._announce_stall,
            exit_code=EXIT_CRASH,
        )

    def install_signal_handlers(self) -> None:
        """Stop claiming, finish what is in flight, exit 0.

        SIGTERM is how a supervisor stops a service, so it has to mean
        the same deliberate thing SIGINT does — an exit code of 0, which
        under `KeepAlive{SuccessfulExit: false}` is what keeps a stopped
        runner stopped instead of instantly restarted.
        """

        def _handler(signum: int, _frame: FrameType | None) -> None:
            self._shutdown = True
            name = signal.Signals(signum).name
            print(f"\n[runner] {name} received; exiting after the current job.")

        signal.signal(signal.SIGINT, _handler)
        signal.signal(signal.SIGTERM, _handler)

    def run(self) -> int:
        """The poll loop. Returns the process's exit code — see exits.py."""
        self.install_signal_handlers()
        if not self.config.api_key:
            # Not a crash. A supervisor restarts a crash, and a machine
            # that is merely not signed in would spin against the
            # throttle forever instead of waiting quietly for `login`.
            print(
                "[runner] this machine is not signed in; nothing to do.\n"
                "[runner] Run `mechbench-runner login` to connect it."
            )
            return EXIT_OK
        self._claim_control_socket()
        self._control.start()
        print(f"[runner] control socket at {self._control.path}")
        self._channel.start()
        self._watchdog.start()
        # The compute layer emits intermediate pipeline objects through
        # mechbench_compute.bench, which otherwise reads credentials from
        # the environment. Ours live in ~/.mechbench/config.toml since
        # `login`, so hand them over explicitly rather than exporting a
        # key into the process environment (task 000284 follow-up).
        self._configure_bench()
        warm = self.config.warm_model_id
        if warm:
            print("[runner] loading model (first call is slow)...")
            # Warm the configured model (MECHBENCH_WARM_MODEL_ID, which
            # may carry a @revision pin) so the first claimed job does not
            # pay cold-start cost. Pin it: an unpinned warm-up resolves
            # upstream's current revision, which drifts out from under the
            # local mlx stack.
            self.state.model_loading(warm)
            self._executor._model_loaded(warm)  # noqa: SLF001
            self.state.model_loaded(warm)
            print("[runner] model loaded; polling.")
        else:
            print("[runner] no warm model set; the first job will load its own.")
            self.state.set_phase("idle")

        with ApiClient(self.config) as api:
            backoff = self.config.poll_interval_seconds
            while not self._shutdown:
                # Every trip round is progress — including an empty poll,
                # which is how an idle runner proves it is alive rather
                # than stuck.
                self._watchdog.stamp()
                asked = self.state.exit_requested
                if asked is not None:
                    code, reason = asked
                    print(f"[runner] exiting ({reason}); code {code}")
                    self._stop_channel()
                    self._watchdog.stop()
                    return code
                if self.state.paused:
                    time.sleep(self.config.poll_interval_seconds)
                    continue
                try:
                    job = api.claim_next_job()
                except ApiError as e:
                    if e.status == 401:
                        # Revoked, or pointed at an account that no longer
                        # knows this machine. Backing off would just hide it.
                        self._signed_out()
                        self._stop_channel()
                        # Deliberate, not a fault: a revoked key does not
                        # start working again, and a restart loop against
                        # it would bury the reason.
                        return EXIT_OK
                    print(f"[runner] /jobs/next error ({e}); "
                          f"retrying in {backoff:.0f}s")
                    time.sleep(backoff)
                    backoff = min(backoff * 2, BACKOFF_MAX_SECONDS)
                    continue
                except Exception as e:  # noqa: BLE001 — surface + keep looping
                    print(f"[runner] API unreachable ({e}); "
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
                    self.state.job_finished(job["id"])
                except Exception as exc:  # noqa: BLE001 — report + continue
                    traceback.print_exc()
                    self.state.job_failed(job.get("id", "?"), str(exc))
                    self._report_error(api, job, exc)

        self._stop_channel()
        self._watchdog.stop()
        return EXIT_OK

    def _configure_bench(self) -> None:
        try:
            from mechbench_compute import bench

            bench.configure(
                api_url=self.config.api_base_url,
                api_key=self.config.api_key,
            )
        except Exception as exc:  # noqa: BLE001 — older compute, or no backend
            print(f"[runner] could not configure the bench emitter: {exc}")

    def _announce_stall(self, idle: float) -> None:
        """Say so on the way down, so the board shows a restart rather
        than a machine that simply went quiet."""
        self.state.set_phase("wedged")
        self.state.emit(
            "runner.wedged",
            {"idle_seconds": round(idle, 1), "job": self._active_job},
        )
        time.sleep(0.25)  # give the channel a moment to flush

    def _stop_channel(self) -> None:
        # Teardown must never mask the reason we are exiting.
        with suppress(Exception):
            self._channel.stop()

    def _signed_out(self) -> None:
        source = (
            "the stored credential"
            if self.config.from_stored_credentials
            else "MECHBENCH_API_KEY"
        )
        message = (
            f"{self.config.api_base_url} rejected {source}. "
            "This machine has been signed out."
        )
        self.state.signed_out(message)
        print(f"\n[runner] {message}")
        if self.config.from_stored_credentials:
            print("[runner] Run `mechbench-runner login` to reconnect.")

    def _handle(self, api: ApiClient, job: dict[str, Any]) -> None:
        job_id = job["id"]
        kind = job["protocolKind"]
        spec_dict = job.get("spec") or {}
        prompt = spec_dict.get("prompt") or ""
        # Where the model is named depends on the shape of the job.
        #
        # A flat job (layer_ablation, decision_distribution) carries
        # `spec.modelId`. A **pipeline** does not: its graph names models
        # per node, as `params.model`, usually as a `$model` hole the run
        # bindings fill — so the executor resolves it and this layer must
        # not demand it. Requiring `spec.modelId` of every kind rejected
        # every protocol run ever queued from the website.
        #
        # No fallback either way: a protocol that does not name its model
        # cannot be executed reproducibly, and a result that cannot say
        # which weights produced it is worse than no result.
        model_id = spec_dict.get("modelId") or self.config.warm_model_id
        if kind == "pipeline":
            # Only for display — the graph is authoritative.
            model_id = model_id or _binding_model(spec_dict)
        elif not model_id:
            raise ValueError(
                f"job {job_id}: spec.modelId is missing and this runner has no "
                f"MECHBENCH_WARM_MODEL_ID to fall back on. A protocol has to "
                f"name the model it runs against."
            )

        if kind == "layer_ablation" and not prompt:
            raise ValueError(f"job {job_id}: spec.prompt missing or empty")

        print(f"[runner] running {job_id} kind={kind}")
        self.state.job_claimed(job_id, kind, model_id)
        # The download callbacks come from the compute layer, which knows
        # nothing about jobs; this is how they find the one to report against.
        self._active_job = job_id
        self._active_api = api
        self._last_byte_report = 0.0
        # What getting ready will involve, declared before any of it happens.
        # Whether the weights need fetching is not known until the hub is
        # asked, so that step starts pending and becomes active only if a
        # download actually begins.
        weights_label = (
            f"Weights for {model_id.split('@')[0]}" if model_id else "Weights"
        )
        self._report_plan(api, job_id, [
            {"key": "weights", "label": weights_label, "status": "pending"},
            {"key": "load", "label": "Load model into memory", "status": "pending"},
        ])
        spec = ProtocolSpec(kind=kind, prompt=prompt, model_id=model_id,
                              extra={**spec_dict,
                                     "resultPath": job.get("resultPath")})
        # Secret lifecycle (000266): claim-delivered credentials are
        # held in memory, passed explicitly, and disposed in the
        # finally below — never env, never specs, never logs.
        secrets = job.get("integrations") or {}

        # Flips on the first progress report, which is also what carries
        # the job from "preparing" to "running". The claim put it in
        # preparing; nothing else ever takes it out.
        promoted = False

        def on_progress(done: int, total: int) -> None:
            nonlocal promoted
            # Throttle: report every 5th unit and the final one. Progress
            # is cosmetic — a failed PATCH must never fail the job.
            # The control surface gets every tick; only the API is throttled.
            self.state.job_progress(done, total)
            # The promotion itself is not cosmetic and is not throttled:
            # until it lands the board still says "preparing", and the
            # first tick can easily be one the throttle would drop.
            if promoted and done % 5 != 0 and done != total:
                return
            try:
                api.report_progress(
                    job_id, done, total,
                    unit=_unit_for(kind),
                    status=None if promoted else "running",
                )
                # Only on success: a failed first report must leave the
                # promotion owed, not silently spent.
                promoted = True
            except Exception as e:  # noqa: BLE001 — best-effort by design
                print(f"[runner] progress report failed ({e}); continuing")

        try:
            payload = self._executor.run(spec, on_progress=on_progress,
                                       secrets=secrets)
            if hasattr(payload, "model_dump"):
                payload = payload.model_dump(mode="json")

            cbor_bytes = dump_canonical(payload)
            digest = hashlib.sha256(cbor_bytes).hexdigest()
            api.complete_job_cbor(job_id, cbor_bytes, f"sha256:{digest}")
            print(f"[runner] {job_id} done ({len(cbor_bytes)} CBOR bytes)")
        finally:
            self._active_job = None
            self._active_api = None
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
            print(f"[runner] failed to report failure for {job_id}")

    def _claim_control_socket(self) -> None:
        """Refuse to start beside another runner; adopt a dead one's socket.

        A crashed runner leaves its socket file behind. Treating that as
        "already running" would mean a machine could never start a runner
        again after one crash, so the file alone is not the test — whether
        anything answers on it is.
        """
        existing = probe()
        if existing is not None:
            raise SystemExit(
                f"[runner] another runner (pid {existing.get('pid')}) is already "
                f"listening at {socket_path()}. Stop it first, or ask it what it "
                f"is doing with `mechbench-runner status`."
            )
        path = socket_path()
        if path.exists():
            print(f"[runner] replacing stale socket at {path}")
            path.unlink()

    def _report_plan(self, api: ApiClient, job_id: str, steps: list[dict]) -> None:
        try:
            api.declare_preparing(job_id, steps)
        except Exception:  # noqa: BLE001 — display only, never fatal
            pass

    def _report_step(self, step: dict) -> None:
        if not (self._active_job and self._active_api):
            return
        try:
            self._active_api.report_preparing_step(self._active_job, step)
        except Exception:  # noqa: BLE001 — display only, never fatal
            pass

    def _announce_download(self, repo_id: str, revision: str | None) -> None:
        """Weights are about to be fetched — say so, loudly and over the wire.

        A first run against an uncached model is minutes of silence otherwise,
        which reads as a hang. `status --watch` and the Mac app both see this.
        """
        self._watchdog.stamp()
        what = f"{repo_id}@{revision}" if revision else repo_id
        print(f"[runner] downloading {what} (this can take a while)")
        self.state.model_downloading(what)
        self._report_step({"key": "weights", "label": f"Download {repo_id}",
                           "status": "active", "unit": "bytes"})

    def _announce_download_bytes(self, done: int, total: int) -> None:
        """Report download progress against the job that triggered it.

        Throttled to once a second: a multi-gigabyte fetch calls this
        thousands of times, and the board only needs a moving bar.
        """
        self._watchdog.stamp()
        self.state.job_progress(done, total)
        now = time.monotonic()
        if now - self._last_byte_report < 1.0 and done != total:
            return
        self._last_byte_report = now
        self._report_step({"key": "weights", "label": "Download weights",
                           "status": "done" if done >= total else "active",
                           "num": done, "den": max(total, 1), "unit": "bytes"})


def _unit_for(protocol_kind: str) -> str:
    """What this kind's progress numbers count, for the board's label."""
    return {
        "layer_ablation": "layers",
        "decision_distribution": "conditions",
    }.get(protocol_kind, "steps")
