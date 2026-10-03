#!/bin/bash
# Enverge startup script for a Linux box with an NVIDIA GPU, written for the
# 2× DGX Spark GB10 (Grace CPU, aarch64, 128 GB unified memory per Spark,
# DGX OS). Any Linux machine with an NVIDIA GPU runs it the same way. Paste it
# into Enverge's "add startup script". Replace MECHBENCH_TOKEN with a single-use
# registration token minted at https://mechbench.ai/download (it starts with
# mbr_ and can be used once). HF_TOKEN is optional: with a token that has
# accepted Gemma's licence the proof uses Google's checkpoint; without one it
# uses an ungated copy of the same weights.
#
# Enverge runs startup scripts as the `user` account in a login shell (not
# root), caps them at one hour, and keeps their output at
# /var/log/enverge/startup.log. Everything below fits inside the hour: the
# install is a few minutes (torch's CUDA wheel is the big download), the
# checkpoint pull a few more (~9 GB), the architecture kit on the GPU a few
# more. The runner then waits for jobs; the proof is queued from anywhere with
# the command printed at the end.
#
# On a two-Spark bundle the script runs on the host Enverge logs you into; the
# second Spark is reachable over the pair's link and is not used by this
# runner. One machine, one runner.

MECHBENCH_TOKEN="mbr_REPLACE_ME"
RUNNER_NAME="enverge-spark-gb10"
HF_TOKEN=""
KIT_COMMIT=""   # a mechbench-compute commit for the kit; empty = the 0.187.0 release commit

LOG="$HOME/mechbench-startup.log"
exec > >(tee -a "$LOG") 2>&1
set -u
echo "=== mechbench startup $(date -u +%Y-%m-%dT%H:%M:%SZ) as $(id -un) on $(hostname)"

# 1. What this machine is.
uname -m
lsb_release -ds 2>/dev/null || cat /etc/os-release | head -2
lscpu | grep -E 'Model name|^CPU\(s\)' | head -2
free -g | awk 'NR==2 {printf "memory %s GB\n", $2}'
nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader 2>/dev/null \
  || { echo "nvidia-smi: no NVIDIA driver here; this script expects a CUDA box"; }
nvidia-smi 2>/dev/null | grep -oE 'CUDA Version: [0-9.]+' | head -1

# 2. Stay up. A user service only survives logout when the user lingers.
sudo -n loginctl enable-linger "$(id -un)" 2>/dev/null \
  || echo "loginctl: no passwordless sudo; run 'sudo loginctl enable-linger $(id -un)' by hand"

# 3. uv, then the runner as a uv tool (its own Python; no system Python
#    touched), with compute's torch extra so the machine offers the torch
#    backend. torch's aarch64 CUDA wheel comes from PyPI.
if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi
export PATH="$HOME/.local/bin:$PATH"
uv tool install --managed-python --python 3.12 'mechbench>=0.59.0' --with 'mechbench-compute[torch]>=0.187.0'
mechbench --version || true
TOOL_PY="$(dirname "$(readlink -f "$(command -v mechbench)")")/python"
echo "tool python: $TOOL_PY"

# 4. CUDA as compute sees it. advertise() must say accelerator cuda and the
#    torch backend; describe() names any backend that is absent and why.
"$TOOL_PY" - <<'PY'
import torch
print("torch", torch.__version__, "cuda", torch.version.cuda, "available", torch.cuda.is_available())
if torch.cuda.is_available():
    free, total = torch.cuda.mem_get_info()
    print("gpu", torch.cuda.get_device_name(0), f"{total/2**30:.0f} GB total, {free/2**30:.0f} GB free")
import mechbench_compute.backends as b
print("advertise", b.advertise())
print("describe", b.describe())
PY

# 5. Pair the machine with the account and keep it running as a systemd user
#    service (mechbench.service under ~/.config/systemd/user).
mechbench login --token "$MECHBENCH_TOKEN" --name "$RUNNER_NAME"
mechbench install-service
sleep 5
mechbench service-status
systemctl --user --no-pager status mechbench.service 2>/dev/null | head -5 || true
mechbench status
mechbench doctor || true

# 6. Pull the proof's checkpoint now so the first job does not spend its time
#    downloading. The torch backend reads transformers-format weights: Google's
#    gated checkpoint with HF_TOKEN, else an ungated copy of the same weights.
if [ -n "$HF_TOKEN" ]; then
  export HF_TOKEN
  PROOF_MODEL="google/gemma-3-4b-it"
else
  PROOF_MODEL="unsloth/gemma-3-4b-it"
fi
"$TOOL_PY" - "$PROOF_MODEL" <<'PY'
import sys
from huggingface_hub import snapshot_download
path = snapshot_download(sys.argv[1], allow_patterns=["*.json", "*.safetensors", "tokenizer*", "*.model", "*.txt"])
print("pulled", sys.argv[1], "->", path)
PY

# 7. The architecture kit on the GPU: the gate for everything after it. A
#    difference in the bit-identical double run is a finding about CUDA
#    numerics; rerun with CUBLAS_WORKSPACE_CONFIG=:4096:8 before concluding.
KIT_DIR="$HOME/mechbench-compute"
if [ ! -d "$KIT_DIR" ]; then
  git clone --quiet https://github.com/mechbench/mechbench-compute "$KIT_DIR"
fi
cd "$KIT_DIR"
if [ -z "$KIT_COMMIT" ]; then
  KIT_COMMIT="$(git log --format=%H -1 --grep='^0\.187\.0:')"
fi
git checkout --quiet "$KIT_COMMIT" && echo "kit at $(git log --oneline -1 | cut -c1-70)"
uv venv --quiet --python 3.12 .venv
uv pip install --quiet --python .venv/bin/python -e '.[torch,dev]'
MECHBENCH_KIT_DEVICE=cuda .venv/bin/python -m pytest \
  tests/test_architecture_kit.py tests/test_torch_backend.py tests/test_backends.py tests/test_backend_jobs.py \
  -q -p no:cacheprovider 2>&1 | tail -15
cd "$HOME"

# 8. Calibration (the noise floor) runs on MLX only today; nothing to do here.
echo "calibration: MLX-only today; skipped on this box"

cat <<EOF
=== done $(date -u +%Y-%m-%dT%H:%M:%SZ); log at $LOG

The runner is registered as $RUNNER_NAME and claims jobs that name the torch
backend. Queue the proof from any machine with the mechbench CLI:

  mechbench run prt_jy7re3bwaccy4b1h4vhr --backend torch --accelerator cuda \\
    --label 'the dice organism on torch: the thirteen questions and the margin ledger' \\
    --param model=$PROOF_MODEL \\
    --param 'organism={"base":{"hf":"$PROOF_MODEL"},"adapters":[{"bench":"benjismith/training/results/j_d9m3t827h5yyx18kg57k/train"}]}' \\
    --wait

Compare its margins and ledger outputs with the canonical MLX job
j_zbcc0nwn46pwv9xhm2c9. Leave MECHBENCH_WARM_MODEL_ID unset on this box.
EOF
