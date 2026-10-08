#!/bin/bash
# Startup script for a Linux box with an NVIDIA GPU, written for Enverge's
# 2× DGX Spark GB10 (Grace CPU, aarch64, 128 GB memory shared by CPU and GPU
# per Spark, DGX OS) and the session plan in the meta repo's
# docs/DGX_SPARK_SESSION.md. Any Linux machine with an NVIDIA GPU runs it the
# same way. Paste it into Enverge's "add startup script".
#
# Fill in before pasting, and nowhere else (never commit or share the values):
#   MECHBENCH_TOKEN       a single-use registration token from
#                         https://mechbench.ai/download (mbr_…)
#   MECHBENCH_TOKEN_PEER  a second one, for the second Spark (optional)
#   HF_TOKEN              a Hugging Face read token that has accepted Gemma's
#                         licence (optional: without it the ungated copies are
#                         pulled where they exist)
#   PEER_HOST             the second Spark's address as seen from this one, if
#                         SSH between them works without a password (optional:
#                         otherwise run this script there by hand with
#                         ROLE=peer and the peer's token)
#
# Enverge runs startup scripts as `user` in a login shell (not root), caps them
# at one hour, and logs them to /var/log/enverge/startup.log. This script keeps
# its own log and writes the first gate's facts to ~/mechbench-g0.json. The big
# checkpoint downloads run detached (a systemd user unit), so they continue past
# the hour; their progress is in ~/mechbench-downloads.log and
# ~/mechbench-downloads.json.
#
# Roles: the host (the Spark Enverge logs you into) takes the Gemma 3 models;
# the peer takes Gemma 4 31B, then Qwen3-32B. One runner per Spark; nothing is
# sharded across the two.

MECHBENCH_TOKEN="mbr_REPLACE_ME"
MECHBENCH_TOKEN_PEER=""
HF_TOKEN=""
PEER_HOST=""
ROLE="${ROLE:-host}"
RUNNER_NAME="${RUNNER_NAME:-enverge-spark-$ROLE}"
RUNNER_MIN="0.66.2"
COMPUTE_MIN="0.195.0"
DISK_RESERVE_GB=60

SELF="$0"
LOG="$HOME/mechbench-startup.log"
G0="$HOME/mechbench-g0.json"
exec > >(tee -a "$LOG") 2>&1
set -u
T0=$(date +%s)
step() { echo; echo "=== [$(( $(date +%s) - T0 ))s] $*"; }
step "mechbench startup ($ROLE) $(date -u +%Y-%m-%dT%H:%M:%SZ) as $(id -un) on $(hostname)"

# 1. What this machine is.
step "1. the machine"
uname -m
(lsb_release -ds 2>/dev/null || head -2 /etc/os-release)
lscpu | grep -E 'Model name|^CPU\(s\)' | head -2
free -g | awk 'NR==2 {printf "memory %s GB total, %s GB available\n", $2, $7}'
df -BG --output=avail,size "$HOME" | tail -1 | awk '{printf "disk %s free of %s\n", $1, $2}'
nvidia-smi --query-gpu=name,driver_version,memory.total,power.limit,persistence_mode \
  --format=csv,noheader 2>/dev/null || echo "nvidia-smi: no NVIDIA driver here; this script expects a CUDA box"
nvidia-smi 2>/dev/null | grep -oE 'CUDA Version: [0-9.]+' | head -1

# 2. Stay up: a user service survives logout only when the user lingers.
step "2. linger"
sudo -n loginctl enable-linger "$(id -un)" 2>/dev/null \
  || echo "loginctl: no passwordless sudo; run 'sudo loginctl enable-linger $(id -un)' by hand"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"

# 3. uv, then the runner as a uv tool with compute's torch extra: the newest
#    releases at or above the minimums (the versions installed are recorded).
step "3. install"
if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi
export PATH="$HOME/.local/bin:$PATH"
uv tool install --managed-python --python 3.12 "mechbench>=$RUNNER_MIN" \
  --with "mechbench-compute[torch]>=$COMPUTE_MIN"
mechbench --version || true
TOOL_PY="$(dirname "$(readlink -f "$(command -v mechbench)")")/python"
echo "tool python: $TOOL_PY"
if [ -n "$HF_TOKEN" ]; then
  # Kept in ~/.cache/huggingface/token (mode 600), where the runner's jobs find
  # it; the disk goes with the instance. Revoke the token after the session.
  HF_TOKEN="$HF_TOKEN" "$TOOL_PY" -c 'import os; from huggingface_hub import login; login(token=os.environ["HF_TOKEN"], add_to_git_credential=False)' \
    && echo "huggingface: token stored"
fi

# 4. CUDA as torch sees it, by running kernels rather than asking. If PyPI's
#    wheel has no kernels for this GPU (GB10 is sm_121), reinstall torch from
#    PyTorch's CUDA 13 index and check again.
step "4. CUDA"
cuda_check() {
  "$TOOL_PY" - <<'PY'
import json, sys
out = {"cuda": False}
try:
    import torch
    out.update(torch=torch.__version__, torch_cuda=torch.version.cuda, arch_list=torch.cuda.get_arch_list())
    if torch.cuda.is_available():
        a = torch.randn(2048, 2048, device="cuda", dtype=torch.bfloat16)
        (a @ a).float().sum().item()
        free, total = torch.cuda.mem_get_info()
        out.update(cuda=True, gpu=torch.cuda.get_device_name(0),
                   capability="sm_%d%d" % torch.cuda.get_device_capability(0),
                   memory_total_gb=round(total / 2**30, 1), memory_free_gb=round(free / 2**30, 1))
except Exception as e:  # noqa: BLE001
    out["error"] = f"{type(e).__name__}: {e}"[:300]
print(json.dumps(out))
sys.exit(0 if out["cuda"] else 1)
PY
}
TORCH_SOURCE="pypi"
if ! CUDA_JSON="$(cuda_check)"; then
  echo "torch from PyPI cannot run kernels here: $CUDA_JSON"
  echo "reinstalling torch from https://download.pytorch.org/whl/cu130"
  uv pip install --python "$TOOL_PY" --reinstall-package torch \
    --index-url https://download.pytorch.org/whl/cu130 --extra-index-url https://pypi.org/simple torch
  TORCH_SOURCE="pytorch-cu130"
  CUDA_JSON="$(cuda_check)" || {
    echo "CUDA still unavailable: $CUDA_JSON"
    echo "fallback by hand: NVIDIA's PyTorch container (nvcr.io/nvidia/pytorch, a GB10 build)"
    echo "with steps 3 and 6 run inside it; see docs/DGX_SPARK_SESSION.md, gate G0"
    TORCH_SOURCE="none"
  }
fi
echo "$CUDA_JSON"
"$TOOL_PY" -c 'import mechbench_compute.backends as b; print("advertise", b.advertise()); print("describe", b.describe())'

# 5. The big checkpoints, detached so they outlive this script. Each Spark
#    pulls its own list in order (the first repository of each a|b choice
#    that exists and is readable), stopping short of the disk's reserve. They
#    land in the Hugging Face cache with compute's own file patterns, where the
#    runner's jobs find them and never download them again.
step "5. downloads (detached)"
if [ "$ROLE" = "host" ]; then
  MODELS="google/gemma-3-4b-it|unsloth/gemma-3-4b-it google/gemma-3-27b-it|unsloth/gemma-3-27b-it google/gemma-3-27b-pt|unsloth/gemma-3-27b-pt"
else
  MODELS="google/gemma-3-4b-it|unsloth/gemma-3-4b-it google/gemma-4-31b-it google/gemma-4-31b-pt|google/gemma-4-31b Qwen/Qwen3-32B"
fi
cat > "$HOME/mechbench-downloads.py" <<'PY'
import json, os, shutil, sys, time
from huggingface_hub import HfApi, snapshot_download
from mechbench_compute.hub import ALLOW_PATTERNS

reserve = int(os.environ.get("DISK_RESERVE_GB", "60")) * 2**30
api = HfApi()
rows = []
def save():
    with open(os.path.expanduser("~/mechbench-downloads.json"), "w") as f:
        json.dump(rows, f, indent=1)
for choice in sys.argv[1:]:
    for repo in choice.split("|"):
        row = {"repo": repo, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        try:
            info = api.model_info(repo, files_metadata=True)
            size = sum((s.size or 0) for s in info.siblings if s.rfilename.endswith(".safetensors"))
            free = shutil.disk_usage(os.path.expanduser("~")).free
            row["bytes"] = size
            if free - size < reserve:
                row["skipped"] = f"disk: {free / 2**30:.0f} GB free, needs {size / 2**30:.0f} GB and the reserve"
                rows.append(row); save(); print(row, flush=True)
                break
            t = time.time()
            path = snapshot_download(repo, allow_patterns=list(ALLOW_PATTERNS))
            took = max(time.time() - t, 1e-9)
            row.update(path=path, seconds=round(took, 1), mb_per_s=round(size / 2**20 / took, 1))
            rows.append(row); save(); print(row, flush=True)
            break
        except Exception as e:  # noqa: BLE001
            row["failed"] = f"{type(e).__name__}: {e}"[:300]
            rows.append(row); save(); print(row, flush=True)
print("downloads finished", flush=True)
PY
# shellcheck disable=SC2086
if systemd-run --user --unit=mechbench-downloads --collect \
     --setenv=DISK_RESERVE_GB="$DISK_RESERVE_GB" \
     --property=StandardOutput="append:$HOME/mechbench-downloads.log" \
     --property=StandardError="append:$HOME/mechbench-downloads.log" \
     "$TOOL_PY" "$HOME/mechbench-downloads.py" $MODELS 2>/dev/null; then
  echo "downloads running as the user unit mechbench-downloads"
else
  DISK_RESERVE_GB="$DISK_RESERVE_GB" setsid nohup "$TOOL_PY" "$HOME/mechbench-downloads.py" $MODELS \
    >> "$HOME/mechbench-downloads.log" 2>&1 < /dev/null &
  echo "downloads running detached (pid $!)"
fi

# 6. The runner as a systemd user service, with cuBLAS deterministic
#    (calibration measures what that costs).
step "6. runner"
mechbench login --token "$MECHBENCH_TOKEN" --name "$RUNNER_NAME"
mechbench install-service
DROPIN="$HOME/.config/systemd/user/mechbench.service.d"
mkdir -p "$DROPIN"
cat > "$DROPIN/session.conf" <<'EOF'
[Service]
Environment=CUBLAS_WORKSPACE_CONFIG=:4096:8
Environment=TOKENIZERS_PARALLELISM=false
EOF
systemctl --user daemon-reload 2>/dev/null && systemctl --user restart mechbench.service 2>/dev/null
sleep 5
mechbench service-status
mechbench status
mechbench doctor || true

# 7. The architecture kit on the GPU, at the commit of the compute release
#    just installed. A difference in a bit-identical double run is a finding
#    about CUDA numerics, not a reason to stop.
step "7. architecture kit on CUDA"
CV="$("$TOOL_PY" -c 'import importlib.metadata as m; print(m.version("mechbench-compute"))')"
KIT_DIR="$HOME/mechbench-compute"
KIT_RESULT="skipped"
[ -d "$KIT_DIR" ] || git clone --quiet https://github.com/mechbench/mechbench-compute "$KIT_DIR"
if [ "$TORCH_SOURCE" != "none" ] && cd "$KIT_DIR"; then
  KIT_COMMIT="$(git log --format=%H -1 --grep="^${CV//./\\.}:")"
  git checkout --quiet "${KIT_COMMIT:-HEAD}" && echo "kit at $(git log --oneline -1 | cut -c1-70) (compute $CV)"
  uv venv --quiet --python 3.12 .venv
  uv pip install --quiet --python .venv/bin/python -e '.[torch,dev]'
  if [ "$TORCH_SOURCE" = "pytorch-cu130" ]; then
    uv pip install --quiet --python .venv/bin/python --reinstall-package torch \
      --index-url https://download.pytorch.org/whl/cu130 --extra-index-url https://pypi.org/simple torch
  fi
  KIT_OUT="$(MECHBENCH_KIT_DEVICE=cuda CUBLAS_WORKSPACE_CONFIG=:4096:8 .venv/bin/python -m pytest \
    tests/test_architecture_kit.py tests/test_torch_backend.py tests/test_backends.py tests/test_backend_jobs.py \
    -q -p no:cacheprovider 2>&1 | tail -15)"
  echo "$KIT_OUT"
  KIT_RESULT="$(echo "$KIT_OUT" | grep -E 'passed|failed' | tail -1)"
  cd "$HOME" || true
fi

# 8. Calibration (identity, the GPU's probes, sustained load, the proof's
#    model), where the installed runner calibrates on torch.
step "8. calibration"
CAL_RESULT="skipped: this runner cannot calibrate on torch"
if [ "$TORCH_SOURCE" != "none" ] && mechbench calibrate --help 2>&1 | grep -q -- '--backend'; then
  if mechbench calibrate --backend torch --out "$HOME/calibration-g0-$ROLE.json" \
      --push --into benjismith/calibration > /dev/null; then
    CAL_RESULT="done: $HOME/calibration-g0-$ROLE.json"
  else
    CAL_RESULT="failed (see the log)"
  fi
fi
echo "calibration: $CAL_RESULT"

# 9. The second Spark: this script again, as the peer, if SSH needs no
#    password and this script can read itself.
PEER_RESULT="not attempted"
if [ "$ROLE" = "host" ] && [ -n "$PEER_HOST" ] && [ -n "$MECHBENCH_TOKEN_PEER" ]; then
  step "9. the peer at $PEER_HOST"
  if [ ! -r "$SELF" ]; then
    PEER_RESULT="this script cannot read itself ($SELF); run it on the peer by hand with ROLE=peer"
  elif ssh -o BatchMode=yes -o ConnectTimeout=10 "$PEER_HOST" true 2>/dev/null; then
    sed -e "s|^MECHBENCH_TOKEN=.*|MECHBENCH_TOKEN=\"$MECHBENCH_TOKEN_PEER\"|" \
        -e 's|^MECHBENCH_TOKEN_PEER=.*|MECHBENCH_TOKEN_PEER=""|' -e 's|^PEER_HOST=.*|PEER_HOST=""|' "$SELF" \
      | ssh "$PEER_HOST" "umask 077 && cat > ~/mechbench-startup.sh && ROLE=peer setsid nohup bash -l ~/mechbench-startup.sh > /dev/null 2>&1 < /dev/null &"
    PEER_RESULT="started on $PEER_HOST (its log: ~/mechbench-startup.log there)"
  else
    PEER_RESULT="ssh to $PEER_HOST needs a password; run this script there by hand with ROLE=peer"
  fi
  echo "$PEER_RESULT"
fi

# 10. The first gate's facts, in one file the battery reads.
step "10. gate G0"
ROLE="$ROLE" RUNNER_NAME="$RUNNER_NAME" CV="$CV" TORCH_SOURCE="$TORCH_SOURCE" CUDA_JSON="$CUDA_JSON" \
KIT_RESULT="$KIT_RESULT" CAL_RESULT="$CAL_RESULT" PEER_RESULT="$PEER_RESULT" \
STARTUP_SECONDS="$(( $(date +%s) - T0 ))" "$TOOL_PY" - "$G0" <<'PY'
import json, os, sys
e = os.environ
try:
    cuda = json.loads(e["CUDA_JSON"])
except ValueError:
    cuda = {"raw": e["CUDA_JSON"]}
facts = {"role": e["ROLE"], "runner": e["RUNNER_NAME"], "compute": e["CV"],
         "torch_source": e["TORCH_SOURCE"], "cuda": cuda, "kit": e["KIT_RESULT"],
         "calibration": e["CAL_RESULT"], "peer": e["PEER_RESULT"],
         "startup_seconds": int(e["STARTUP_SECONDS"])}
with open(sys.argv[1], "w") as f:
    json.dump(facts, f, indent=1)
print(json.dumps(facts, indent=1))
PY

cat <<EOF

=== done in $(( $(date +%s) - T0 ))s; log at $LOG, gate facts at $G0
Downloads continue: tail -f ~/mechbench-downloads.log
The runner $RUNNER_NAME claims jobs that name the torch backend. The session's
jobs are queued from the Mac by the battery script (docs/DGX_SPARK_SESSION.md).
EOF
