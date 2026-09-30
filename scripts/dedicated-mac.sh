#!/bin/bash
# Enverge startup script for the mechbench-exploration Mac Studio (rental pass 1).
# Paste this into Enverge's "add startup script". Replace the token below with a
# single-use registration token minted at https://mechbench.ai/download
# (it starts with mbr_ and can be used once; nothing else in this file is secret).
#
# Enverge runs startup scripts as the `user` account in a login shell (not root),
# caps them at one hour, and keeps their output at /var/log/enverge/startup.log
# (enverge.ai/docs, written for their Linux boxes; the Mac product is newer than
# the docs). Everything below fits well inside the hour: the install takes a few
# minutes, the first calibration a few more (it pulls Gemma 4 E2B, ~10 GB).

MECHBENCH_TOKEN="mbr_PASTE_THE_TOKEN_HERE"
RUNNER_NAME="enverge-m5-ultra"
CALIBRATION_PROJECT="benjismith/calibration"

LOG="$HOME/mechbench-startup.log"
exec > >(tee -a "$LOG") 2>&1
set -u
echo "=== mechbench startup $(date -u +%Y-%m-%dT%H:%M:%SZ) as $(id -un) on $(hostname)"

# 1. What this machine is.
sw_vers
sysctl -n machdep.cpu.brand_string
sysctl -n hw.memsize | awk '{printf "memory %.0f GB\n", $1/1024/1024/1024}'

# 2. Never sleep; come back after a power cut. (Automatic login for the user
#    needs the account password, so that step is done once by hand if the
#    image does not already log the user in.)
sudo -n pmset -a sleep 0 disksleep 0 autorestart 1 2>/dev/null || echo "pmset: no passwordless sudo; run it by hand"
fdesetup status 2>/dev/null || true

# 3. uv, then the runner as a uv tool (its own Python; no system Python touched).
if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi
export PATH="$HOME/.local/bin:$PATH"
uv tool install --managed-python mechbench
mechbench --version || true

# 4. Pair the machine with the account and keep it running as a service.
mechbench login --token "$MECHBENCH_TOKEN" --name "$RUNNER_NAME"
mechbench install-service
sleep 5
mechbench service-status
launchctl print "user/$(id -u)/ai.mechbench.runner" 2>/dev/null | grep -E 'state =|spawn type' \
  || launchctl print "gui/$(id -u)/ai.mechbench.runner" 2>/dev/null | grep -E 'state =|spawn type'
mechbench status

# 5. First calibration on an idle box, stored on the platform under the
#    calibration project so home and box sit side by side.
mechbench calibrate --push --into "$CALIBRATION_PROJECT" --out "$HOME/calibration-first-boot.json"

echo "=== done $(date -u +%Y-%m-%dT%H:%M:%SZ); log at $LOG"
