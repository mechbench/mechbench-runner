from __future__ import annotations

import hashlib
import inspect
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import traceback
from collections.abc import Mapping
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from types import FrameType
from typing import Any

from mechbench_compute.protocol import ProtocolExecutor, ProtocolSpec
from mechbench_schema import dump_canonical

from . import supervisor as supervisor_mod
from .api_client import ApiClient, ApiError
from .channel import LiveChannel
from .config import Config
from .control import ControlServer, RunnerState, probe, socket_path
from .exits import EXIT_CRASH, EXIT_OK
from .paths import limits_path, spool_dir
from .spend import SharedLimiter, SpendLedger
from .spool import JobSpool
from .watchdog import Watchdog

BACKOFF_MAX_SECONDS = 30.0
RECONCILE_SECONDS = 300.0
LEGACY_STALE_SECONDS = 900.0
MAX_TRANSPORT_RESUMES = 2
PRESIGN_THRESHOLD_BYTES = 8 * 1024 * 1024


def _presign_threshold() -> int:
    raw = os.environ.get("MECHBENCH_PRESIGN_THRESHOLD_BYTES")
    if raw is None or not raw.strip():
        return PRESIGN_THRESHOLD_BYTES
    try:
        return max(0, int(raw))
    except ValueError:
        return PRESIGN_THRESHOLD_BYTES


TRANSPORT_INTERRUPT_PREFIX = "the API was unreachable while storing a result"

TERMINAL_SERVER_STATUS = ("done", "done_with_missing", "failed", "cancelled")


def _spool_result(job_id: str, cbor_bytes: bytes, digest: str,
                  claim_token: str | None = None,
                  missing: Mapping[str, Any] | None = None) -> None:
    d = spool_dir() / job_id
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = d / "result.cbor.tmp"
    tmp.write_bytes(cbor_bytes)
    os.replace(tmp, d / "result.cbor")
    (d / "result.sha256").write_text(digest)
    if missing:
        (d / "result.missing.json").write_text(json.dumps(missing))
    if claim_token:
        tok = d / "claim.token"
        tok.write_text(claim_token)
        tok.chmod(0o600)


MAX_MISSING_NODES = 64
MAX_MISSING_REASON = 2000


def _missing_of(payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, Mapping):
        return None
    inner = payload.get("payload")
    obj = inner if isinstance(inner, Mapping) else payload
    missing = obj.get("nodes_missing")
    if not isinstance(missing, Mapping) or not missing:
        return None
    out: dict[str, Any] = {}
    for nid, why in list(missing.items())[:MAX_MISSING_NODES]:
        entry = why if isinstance(why, Mapping) else {"reason": str(why)}
        source = entry.get("source")
        out[str(nid)[:64]] = {
            "reason": str(entry.get("reason", ""))[:MAX_MISSING_REASON],
            **({"source": [str(s)[:64] for s in source][:64]}
               if isinstance(source, (list, tuple)) else {}),
        }
    return out


def _spooled_missing(job_id: str) -> dict[str, Any] | None:
    f = spool_dir() / job_id / "result.missing.json"
    if not f.is_file():
        return None
    with suppress(Exception):
        loaded = json.loads(f.read_text())
        if isinstance(loaded, dict) and loaded:
            return loaded
    return None


def _spooled_claim_token(job_id: str) -> str | None:
    tok = spool_dir() / job_id / "claim.token"
    return tok.read_text().strip() if tok.is_file() else None


def _claim_token_of(api: Any, job_id: str) -> str | None:
    store = getattr(api, "claim_tokens", None)
    return store.get(job_id) if isinstance(store, dict) else None


def _remember_claim_token(api: Any, job_id: str, token: str) -> None:
    store = getattr(api, "claim_tokens", None)
    if isinstance(store, dict):
        store.setdefault(job_id, token)


def _spooled_result(job_id: str) -> tuple[bytes, str] | None:
    d = spool_dir() / job_id
    blob, sha = d / "result.cbor", d / "result.sha256"
    if not (blob.is_file() and sha.is_file()):
        return None
    return blob.read_bytes(), sha.read_text().strip()


def _clear_spool(job_id: str) -> None:
    shutil.rmtree(spool_dir() / job_id, ignore_errors=True)


def _disown_spool(job_id: str, why: str) -> Path | None:
    d = spool_dir() / job_id
    blob = d / "result.cbor"
    if not blob.is_file():
        return None
    kept = d / "result.cbor.disowned"
    os.replace(blob, kept)
    (d / "disowned.txt").write_text(
        f"{datetime.now(UTC).isoformat()}\n{why}\n"
        "This result was never accepted by the server. Nothing retries it.\n"
    )
    return kept


def _standing_refusal(exc: BaseException) -> str | None:
    if not isinstance(exc, ApiError) or exc.status not in (403, 409):
        return None
    body = exc.body if isinstance(exc.body, dict) else {}
    code = str(body.get("code") or "")
    if code not in ("BAD_CLAIM_TOKEN", "NOT_CLAIMANT", "BAD_STATE"):
        return None
    return str(body.get("error") or code)


def _spooled_job_ids() -> list[str]:
    root = spool_dir()
    return sorted(
        p.name for p in root.iterdir()
        if p.is_dir() and (p / "result.cbor").is_file()
    )


def _seconds_since(iso: object) -> float:
    from datetime import datetime

    if not isinstance(iso, str) or not iso:
        return 0.0
    try:
        then = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    return (datetime.now(UTC) - then).total_seconds()


def _binding_model(spec: dict[str, Any]) -> str | None:
    bindings = spec.get("bindings")
    if not isinstance(bindings, dict):
        return None
    value = bindings.get("model")
    return value if isinstance(value, str) and value else None


class JobRunner:
    def __init__(self, config: Config) -> None:
        self.config = config
        self._shutdown = False
        self._active_job: str | None = None
        self._active_api: ApiClient | None = None
        self._last_byte_report = 0.0
        self._last_node: dict[str, Any] | None = None
        self._last_scalar: tuple[int, int] = (0, 0)
        self._spool: JobSpool | None = None
        self._disowned: set[str] = set()
        self._caffeinate: subprocess.Popen | None = None
        self._transport_interrupts: dict[str, int] = {}
        self._limiter = SharedLimiter(limits_path())
        self._spend: SpendLedger | None = None
        hooks = dict(
            on_download=self._announce_download,
            on_download_bytes=self._announce_download_bytes,
            on_node_start=self._spool_node_start,
            on_spool_item=self._spool_item,
            on_checkpoint=self._spool_checkpoint,
            on_node_done=self._spool_node_done,
            limiter=self._limiter,
        )
        try:
            self._executor = ProtocolExecutor(on_node_kept=self._spool_node_kept, **hooks)
        except TypeError:
            try:
                self._executor = ProtocolExecutor(**hooks)
            except TypeError:
                self._executor = ProtocolExecutor(
                    on_download=self._announce_download,
                    on_download_bytes=self._announce_download_bytes,
                )
        try:
            from . import __version__ as runner_version
        except ImportError:
            runner_version = "unknown"
        try:
            from mechbench_compute import __version__ as compute_version
        except ImportError:
            compute_version = ""
        self.state = RunnerState(version=runner_version, api_url=config.api_base_url,
                                 compute_version=compute_version)
        self.state.limits_snapshot = self._limiter.snapshot
        self._control = ControlServer(self.state)
        self._channel = LiveChannel(config, self.state)
        self._watchdog = Watchdog(
            stall_seconds=config.watchdog_seconds,
            on_stall=self._announce_stall,
            exit_code=EXIT_CRASH,
        )

    def install_signal_handlers(self) -> None:
        def _handler(signum: int, _frame: FrameType | None) -> None:
            self._shutdown = True
            name = signal.Signals(signum).name
            print(f"\n[runner] {name} received; exiting after the current job.")

        signal.signal(signal.SIGINT, _handler)
        signal.signal(signal.SIGTERM, _handler)

    def _sleep(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while not self._shutdown:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(1.0, remaining))

    def run(self) -> int:
        self.install_signal_handlers()
        if not self.config.api_key:
            print(
                "[runner] this machine is not signed in; nothing to do.\n"
                "[runner] Run `mechbench login` to connect it."
            )
            return EXIT_OK
        self._claim_control_socket()
        self._control.start()
        print(f"[runner] control socket at {self._control.path}")
        self._channel.start()
        self._watchdog.start()
        self._configure_bench()
        self._sweep_cache(None)
        warm = self.config.warm_model_id
        if warm:
            print("[runner] loading model (first call is slow)...")
            self.state.model_loading(warm)
            self._executor._model_loaded(warm)  # noqa: SLF001
            self.state.model_loaded(warm)
            print("[runner] model loaded; polling.")
        else:
            print("[runner] no warm model set; the first job will load its own.")
            self.state.set_phase("idle")

        with ApiClient(self.config) as api:
            backoff = self.config.poll_interval_seconds
            self._reconcile_jobs(api)
            last_reconcile = time.monotonic()
            while not self._shutdown:
                self._watchdog.stamp()
                if supervisor_mod.orphaned():
                    print("[runner] the supervisor that started this runner is "
                          "gone; exiting so a fresh one can take the socket.",
                          flush=True)
                    self._stop_channel()
                    return EXIT_OK
                if time.monotonic() - last_reconcile >= RECONCILE_SECONDS:
                    self._reconcile_jobs(api)
                    last_reconcile = time.monotonic()
                asked = self.state.exit_requested
                if asked is not None:
                    code, reason = asked
                    print(f"[runner] exiting ({reason}); code {code}")
                    self._stop_channel()
                    self._watchdog.stop()
                    return code
                if self.state.paused:
                    self._sleep(self.config.poll_interval_seconds)
                    continue
                try:
                    job = api.claim_next_job()
                except ApiError as e:
                    if e.status == 401:
                        self._signed_out()
                        self._stop_channel()
                        return EXIT_OK
                    print(f"[runner] /jobs/next error ({e}); "
                          f"retrying in {backoff:.0f}s")
                    self._sleep(backoff)
                    backoff = min(backoff * 2, BACKOFF_MAX_SECONDS)
                    continue
                except Exception as e:  # noqa: BLE001
                    print(f"[runner] API unreachable ({e}); "
                          f"retrying in {backoff:.0f}s")
                    self._sleep(backoff)
                    backoff = min(backoff * 2, BACKOFF_MAX_SECONDS)
                    continue

                backoff = self.config.poll_interval_seconds

                if job is None:
                    self._sleep(self.config.poll_interval_seconds)
                    continue

                self._sweep_cache(job)

                try:
                    self._handle(api, job)
                    self.state.job_finished(job["id"])
                    self._transport_interrupts.pop(job["id"], None)
                except Exception as exc:  # noqa: BLE001
                    traceback.print_exc()
                    if self._is_transport(exc):
                        self.state.job_interrupted(job.get("id", "?"), str(exc))
                    else:
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
        except Exception as exc:  # noqa: BLE001
            print(f"[runner] could not configure the bench emitter: {exc}")

    def _flush_spool(self, api: ApiClient) -> None:
        try:
            spooled = _spooled_job_ids()
        except OSError:
            return
        for job_id in spooled:
            if job_id == self._active_job:
                continue
            found = _spooled_result(job_id)
            if found is None:
                continue
            cbor_bytes, digest = found
            tok = _spooled_claim_token(job_id)
            if tok:
                _remember_claim_token(api, job_id, tok)
            try:
                j = api.get_job(job_id)
            except Exception as e:  # noqa: BLE001
                print(f"[runner] spool: could not look up {job_id} ({e})")
                continue
            status = j.get("status")
            if status in TERMINAL_SERVER_STATUS or status == "queued":
                _clear_spool(job_id)
                continue
            claimed_by = j.get("claimedByRunnerId")
            if claimed_by is not None and claimed_by != self.config.runner_id:
                continue
            try:
                if status in ("preparing", "running"):
                    api.interrupt_job(
                        job_id,
                        "the result was ready but its upload never landed; "
                        "delivered from this machine's spool",
                        timeout=15.0,
                    )
                self._upload_result(api, job_id, cbor_bytes, digest,
                                    _spooled_missing(job_id))
            except Exception as e:  # noqa: BLE001
                standing = _standing_refusal(e)
                if standing is None:
                    print(f"[runner] spool: {job_id} not accepted yet ({e})")
                    continue
                kept = _disown_spool(job_id, standing)
                print(f"[runner] spool: {job_id} will never be accepted — "
                      f"{standing}. Not retrying"
                      + (f"; the result is kept at {kept}" if kept else ""))
                self.state.emit("job.disowned",
                                {"id": job_id, "reason": standing})
                continue
            _clear_spool(job_id)
            print(f"[runner] {job_id} delivered from spool "
                  f"({len(cbor_bytes)} CBOR bytes)")
            self.state.emit("job.delivered_late", {"id": job_id})

    def _reconcile_jobs(self, api: ApiClient) -> None:
        self._flush_spool(api)
        try:
            listed = api.list_jobs()
        except Exception as e:  # noqa: BLE001
            print(f"[runner] reconcile skipped ({e})")
            return
        for j in listed:
            if j.get("status") not in ("preparing", "running"):
                continue
            job_id = j.get("id")
            if not isinstance(job_id, str) or job_id == self._active_job:
                continue
            if job_id in self._disowned:
                continue
            claimed_by = j.get("claimedByRunnerId")
            if claimed_by is not None:
                if (self.config.runner_id is None
                        or claimed_by != self.config.runner_id):
                    continue
                reason = (
                    "this machine holds the claim on this job but is not "
                    "executing it — interrupted by a crash or restart; "
                    "it resumes with this process"
                )
                try:
                    api.interrupt_job(job_id, reason, timeout=15.0)
                except Exception as e:  # noqa: BLE001
                    self._note_unreconciled(job_id, e)
                    continue
                print(f"[runner] reconciled {job_id}: interrupted ({reason})")
                self.state.emit("job.reconciled", {"id": job_id, "reason": reason})
                continue
            else:
                if self._active_job is not None:
                    continue
                if _seconds_since(j.get("updatedAt")) < LEGACY_STALE_SECONDS:
                    continue
                reason = (
                    "no runner holds a claim on this job and it has "
                    f"reported nothing for {LEGACY_STALE_SECONDS / 60:.0f}+ "
                    "minutes — orphaned before claims were attributed, "
                    "repaired by reconciliation"
                )
            try:
                api.fail_job(job_id, reason, timeout=15.0)
            except Exception as e:  # noqa: BLE001
                self._note_unreconciled(job_id, e)
                continue
            print(f"[runner] reconciled {job_id}: failed ({reason})")
            self.state.emit("job.reconciled", {"id": job_id, "reason": reason})

    def _note_unreconciled(self, job_id: str, exc: BaseException) -> None:
        standing = _standing_refusal(exc)
        if standing is None:
            print(f"[runner] could not reconcile {job_id}: {exc}")
            return
        self._disowned.add(job_id)
        print(f"[runner] {job_id} is not this process's to repair — "
              f"{standing}. Leaving it to the server; not asking again.")
        self.state.emit("job.disowned", {"id": job_id, "reason": standing})

    def _sweep_cache(self, job: dict[str, Any] | None) -> None:
        try:
            from . import budget

            protect: set[str] = set()
            if self.config.warm_model_id:
                protect.add(budget.repo_of(self.config.warm_model_id))
            if job is not None:
                spec = job.get("spec") or {}
                model = spec.get("modelId") or _binding_model(spec)
                if isinstance(model, str) and model:
                    budget.record_use(model)
                    protect.add(budget.repo_of(model))
            budget.sweep(
                protect=protect,
                say=lambda m: print(f"[runner] {m}"),
                emit=self.state.emit,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[runner] cache sweep skipped ({exc})")

    def _announce_stall(self, idle: float) -> None:
        self.state.set_phase("wedged")
        self.state.emit(
            "runner.wedged",
            {"idle_seconds": round(idle, 1), "job": self._active_job},
        )
        if self._active_job is not None and self._active_api is not None:
            with suppress(Exception):
                self._active_api.interrupt_job(
                    self._active_job,
                    f"the runner made no progress for {idle:.0f}s and was "
                    f"restarted by its watchdog; the job is interrupted and "
                    f"resumes with the next process",
                    timeout=10.0,
                )
        time.sleep(0.25)

    def _stop_channel(self) -> None:
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
            print("[runner] Run `mechbench login` to reconnect.")

    def _handle(self, api: ApiClient, job: dict[str, Any]) -> None:
        job_id = job["id"]
        kind = job["protocolKind"]
        spec_dict = job.get("spec") or {}
        prompt = spec_dict.get("prompt") or ""
        model_id = spec_dict.get("modelId") or self.config.warm_model_id
        if kind == "pipeline":
            model_id = model_id or _binding_model(spec_dict)
        elif not model_id:
            raise ValueError(
                f"job {job_id}: spec.modelId is missing and this runner has no "
                f"MECHBENCH_WARM_MODEL_ID to fall back on. A protocol has to "
                f"name the model it runs against."
            )

        if kind == "layer_ablation" and not prompt:
            raise ValueError(f"job {job_id}: spec.prompt missing or empty")

        resumed = bool(job.get("resume"))
        if resumed and self._refuse_exhausted_transport_resume(api, job):
            return
        if resumed:
            print(f"[runner] resuming {job_id} (attempt {job.get('resumeCount', '?')})")
        spooled = _spooled_result(job_id)
        if spooled is not None:
            cbor_bytes, digest = spooled
            tok = _spooled_claim_token(job_id)
            if tok:
                _remember_claim_token(api, job_id, tok)
            print(f"[runner] {job_id}: finished result found in spool; delivering")
            self.state.job_claimed(job_id, kind, model_id)
            with suppress(Exception):
                api.report_progress(
                    job_id, 1, 1, unit=_unit_for(kind), status="running",
                    resumed_from={"node": "", "reused": 1},
                )
            self._deliver(api, job_id, cbor_bytes, digest)
            return

        print(f"[runner] running {job_id} kind={kind}")
        self.state.job_claimed(job_id, kind, model_id)
        self._spool = JobSpool(job_id)
        self._hold_awake()
        resume_map: dict = {}
        resume_summary: dict | None = None
        if resumed and "resume" in inspect.signature(self._executor.run).parameters:
            with suppress(Exception):
                resume_map = self._spool.resume_map()
                resume_summary = self._spool.summary() if resume_map else None
            if resume_map:
                print(f"[runner] {job_id}: resuming from spool — "
                      f"{resume_summary}")
        self._active_job = job_id
        self._active_api = api
        self._last_byte_report = 0.0
        self._last_node = None
        self._last_scalar = (0, 0)
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
        secrets = job.get("integrations") or {}
        self._spend = SpendLedger(spec_dict.get("budgetUsd"))

        promoted = False
        reported_node = -1

        def on_progress(done: int, total: int,
                        node: dict | None = None) -> None:
            nonlocal promoted, reported_node
            self._watchdog.stamp()
            self._last_node = dict(node) if node else None
            self._last_scalar = (done, total)
            self.state.job_progress(done, total, node)
            node_index = int(node.get("index", 0)) if node else 0
            if (promoted and done % 5 != 0 and done != total
                    and node_index == reported_node):
                return
            extra: dict[str, Any] = {}
            if resumed and not promoted:
                extra["resumed_from"] = resume_summary or {
                    "node": str((node or {}).get("id", "")), "reused": 0,
                }
            ledger = self._spend
            if ledger is not None and ledger.changed():
                extra["spent_usd"] = ledger.spent_usd
                self.state.job_spend(ledger.spent_usd, ledger.cap_usd)
            try:
                api.report_progress(
                    job_id, done, total,
                    unit=_unit_for(kind),
                    status=None if promoted else "running",
                    node=node,
                    **extra,
                )
                promoted = True
                reported_node = node_index
                if "spent_usd" in extra and self._spend is not None:
                    self._spend.mark_reported()
            except Exception as e:  # noqa: BLE001
                print(f"[runner] progress report failed ({e}); continuing")

        try:
            run_kwargs: dict[str, Any] = {}
            if resume_map:
                run_kwargs["resume"] = resume_map
            if ("budget" in inspect.signature(self._executor.run).parameters
                    and self._spend is not None):
                run_kwargs["budget"] = self._spend.budget
            payload = self._executor.run(spec, on_progress=on_progress,
                                       secrets=secrets, **run_kwargs)
            if hasattr(payload, "model_dump"):
                payload = payload.model_dump(mode="json")

            cbor_bytes = dump_canonical(payload)
            digest = hashlib.sha256(cbor_bytes).hexdigest()
            missing = _missing_of(payload)
            _spool_result(job_id, cbor_bytes, digest,
                          _claim_token_of(api, job_id), missing)
            self._deliver(api, job_id, cbor_bytes, digest, missing)
        finally:
            if self._spend is not None and self._spend.spent_usd > 0:
                with suppress(Exception):
                    api.report_progress(job_id, *self._last_scalar or (0, 1),
                                        spent_usd=self._spend.spent_usd)
            self._spend = None
            self._active_job = None
            self._active_api = None
            self._spool = None
            self._release_awake()
            secrets.clear()
            job.pop("integrations", None)

    def _spool_node_start(self, nid: str, fingerprint: str) -> None:
        if self._spool is not None:
            with suppress(Exception):
                self._spool.node_start(nid, fingerprint)

    def _spool_item(self, nid: str, key: str, item) -> None:
        if self._spool is None:
            return
        try:
            self._spool.item(nid, key, item)
        except Exception as e:  # noqa: BLE001
            n = self._spool.dropped.get(nid, 0) + 1
            self._spool.dropped[nid] = n
            if n == 1:
                print(f"[runner] {self._spool.job_id}: node {nid!r} item "
                      f"{key!r} could not be spooled and will not resume "
                      f"({type(e).__name__}: {str(e)[:120]}); further drops "
                      f"for this node are counted, not logged")

    def _spool_checkpoint(self, nid: str, state) -> None:
        if self._spool is not None:
            with suppress(Exception):
                self._spool.checkpoint(nid, state)

    def _spool_node_done(self, nid: str, path, fingerprint: str) -> None:
        if self._spool is not None:
            with suppress(Exception):
                self._spool.node_done(nid, path, fingerprint)

    def _spool_node_kept(self, nid: str, fingerprint: str, result) -> None:
        if self._spool is None:
            return
        try:
            self._spool.node_kept(nid, fingerprint, result)
        except Exception as exc:  # noqa: BLE001
            print(f"[runner] could not hold {nid}'s result in the spool: {exc}")

    def _hold_awake(self) -> None:
        if self._caffeinate is not None or shutil.which("caffeinate") is None:
            return
        with suppress(Exception):
            self._caffeinate = subprocess.Popen(
                ["caffeinate", "-i", "-w", str(os.getpid())],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

    def _release_awake(self) -> None:
        proc, self._caffeinate = self._caffeinate, None
        if proc is not None:
            with suppress(Exception):
                proc.terminate()

    @staticmethod
    def _upload_result(api: ApiClient, job_id: str,
                       cbor_bytes: bytes, digest: str,
                       missing: Mapping[str, Any] | None = None) -> None:
        content_hash = f"sha256:{digest}"
        grant = None
        if len(cbor_bytes) > _presign_threshold():
            grant = api.request_result_upload(job_id, content_hash, len(cbor_bytes))
        if grant is not None:
            api.upload_to_grant(grant, cbor_bytes)
            api.complete_job_uploaded(job_id, content_hash, missing=missing)
        else:
            api.complete_job_cbor(job_id, cbor_bytes, content_hash)

    def _deliver(self, api: ApiClient, job_id: str,
                 cbor_bytes: bytes, digest: str,
                 missing: Mapping[str, Any] | None = None) -> None:
        try:
            self._upload_result(api, job_id, cbor_bytes, digest, missing)
        except Exception as e:  # noqa: BLE001
            print(f"[runner] {job_id}: result spooled; upload not accepted "
                  f"yet ({e}) — reconciliation retries")
            self.state.emit("job.spooled", {"id": job_id})
            return
        _clear_spool(job_id)
        print(f"[runner] {job_id} done ({len(cbor_bytes)} CBOR bytes)")

    @staticmethod
    def _is_transport(exc: BaseException) -> bool:
        try:
            from mechbench_compute.bench import BenchTransportError
        except Exception:  # noqa: BLE001
            return False
        seen: set[int] = set()
        cur: BaseException | None = exc
        while cur is not None and id(cur) not in seen:
            if isinstance(cur, BenchTransportError):
                return True
            seen.add(id(cur))
            cur = cur.__cause__ or cur.__context__
        return False

    def _transport_resumes(self, job_id: str, job: dict[str, Any]) -> int:
        local = self._transport_interrupts.get(job_id, 0)
        return max(local, int(job.get("resumeCount") or 0))

    def _refuse_exhausted_transport_resume(self, api: ApiClient,
                                           job: dict[str, Any]) -> bool:
        job_id = job.get("id")
        reason = str(job.get("errorMessage") or "")
        if not job_id or not reason.startswith(TRANSPORT_INTERRUPT_PREFIX):
            return False
        resumes = int(job.get("resumeCount") or 0)
        if resumes < MAX_TRANSPORT_RESUMES:
            return False
        message = (
            f"the API would not accept a result after {resumes} resume(s) "
            f"and the per-attempt retries; the last interrupt was: "
            f"{reason[:400]}. A transport failure that repeats at the same "
            f"node is not transient — check the result's size against the "
            f"API's body limit before resuming again.")
        try:
            api.fail_job(job_id, message)
        except Exception:  # noqa: BLE001
            print(f"[runner] failed to report failure for {job_id}")
        self._transport_interrupts.pop(job_id, None)
        print(f"[runner] {job_id}: refusing to resume — transport failure "
              f"repeated {resumes} time(s); failed")
        return True

    def _report_error(
        self, api: ApiClient, job: dict[str, Any], exc: Exception
    ) -> None:
        job_id = job.get("id")
        if not job_id:
            return
        import re as _re
        message = _re.sub(r"hf_[A-Za-z0-9]{8,}", "hf_[redacted]", str(exc))

        if self._is_transport(exc):
            resumes = self._transport_resumes(job_id, job)
            if resumes < MAX_TRANSPORT_RESUMES:
                self._transport_interrupts[job_id] = resumes + 1
                reason = (f"{TRANSPORT_INTERRUPT_PREFIX}, "
                          f"after retries: {message}")
                try:
                    api.interrupt_job(job_id, reason, timeout=15.0)
                except Exception:  # noqa: BLE001
                    print(f"[runner] {job_id}: could not report the interrupt; "
                          f"reconciliation will")
                print(f"[runner] {job_id}: interrupted, not failed — transport "
                      f"({message}); spool kept, resume "
                      f"{resumes + 1}/{MAX_TRANSPORT_RESUMES}")
                return
            self._transport_interrupts.pop(job_id, None)
            message = (
                f"the API would not accept a result after {resumes} "
                f"resume(s) and the per-attempt retries: {message}. A "
                f"transport failure that repeats at the same node is not "
                f"transient — check the result's size against the API's "
                f"body limit before resuming again.")

        try:
            api.fail_job(job_id, message)
        except Exception:  # noqa: BLE001
            print(f"[runner] failed to report failure for {job_id}")
        _clear_spool(job_id)

    def _claim_control_socket(self) -> None:
        existing = probe()
        if existing is not None:
            print(
                f"[runner] another runner (pid {existing.get('pid')}) is already "
                f"listening at {socket_path()}. Stop it first, or ask it what it "
                f"is doing with `mechbench status`.",
                file=sys.stderr, flush=True,
            )
            raise SystemExit(EXIT_OK)
        path = socket_path()
        if path.exists():
            print(f"[runner] replacing stale socket at {path}")
            path.unlink()

    def _report_plan(self, api: ApiClient, job_id: str, steps: list[dict]) -> None:
        try:
            api.declare_preparing(job_id, steps)
        except Exception:  # noqa: BLE001
            pass

    def _report_step(self, step: dict) -> None:
        if not (self._active_job and self._active_api):
            return
        try:
            self._active_api.report_preparing_step(self._active_job, step)
        except Exception:  # noqa: BLE001
            pass

    def _announce_download(self, repo_id: str, revision: str | None) -> None:
        self._watchdog.stamp()
        what = f"{repo_id}@{revision}" if revision else repo_id
        print(f"[runner] downloading {what} (this can take a while)")
        self.state.model_downloading(what)
        self._report_step({"key": "weights", "label": f"Download {repo_id}",
                           "status": "active", "unit": "bytes"})

    def _announce_download_bytes(self, done: int, total: int) -> None:
        self._watchdog.stamp()
        self.state.job_progress(done, total)
        now = time.monotonic()
        if now - self._last_byte_report < 1.0 and done != total:
            return
        self._last_byte_report = now
        if self._last_node is None:
            self._report_step({"key": "weights", "label": "Download weights",
                               "status": "done" if done >= total else "active",
                               "num": done, "den": max(total, 1),
                               "unit": "bytes"})
            return
        if self._active_job is None or self._active_api is None:
            return
        detail = (f"downloading weights · {done / 1e9:.1f} of "
                  f"{total / 1e9:.1f} GB" if total > 0
                  else f"downloading weights · {done / 1e9:.1f} GB")
        sd, st = self._last_scalar
        try:
            self._active_api.report_progress(
                self._active_job, sd, st,
                node={**self._last_node, "detail": detail},
            )
        except Exception as e:  # noqa: BLE001
            print(f"[runner] progress report failed ({e}); continuing")


def _unit_for(protocol_kind: str) -> str:
    return {
        "layer_ablation": "layers",
        "decision_distribution": "conditions",
    }.get(protocol_kind, "steps")
