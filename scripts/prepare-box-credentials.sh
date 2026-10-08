#!/bin/bash
# Register the DGX Spark session's two runners from this Mac, now, and put a
# completed dedicated-cuda.sh on the clipboard for Enverge's "add startup
# script" form. Registration tokens expire 15 minutes after they are minted, and
# the box starts whenever Enverge frees a slot, so the box cannot register
# itself; it restores these saved credentials instead.
#
#   scripts/prepare-box-credentials.sh [path/to/dedicated-cuda.sh]
#
# Asks (nothing is echoed) for two registration tokens from
# https://mechbench.ai/settings/runners ("Generate a token") (mbr_…, used within 15 minutes) and, optionally,
# a Hugging Face read token that has accepted Gemma's licence. Each runner is
# registered by `mechbench login --token - --name enverge-spark-{host,peer}`
# with HOME set to a fresh temporary directory: login writes only
# $HOME/.mechbench/config.toml there, finds no service under that HOME and,
# with no terminal on its stdin, installs none; this Mac's own ~/.mechbench,
# its runner and its launchd service are untouched. Each config.toml holds the
# runner's key and the CLI key registration mints for the verbs. They are
# base64-embedded in the script, which goes to pbcopy only (never to the
# terminal, a file or a log); the temporary directories are deleted.
#
# After the session: revoke both runners at https://mechbench.ai/settings/runners
# (their CLI keys go with them) and the Hugging Face token. A registered runner
# that never connects just sits idle in the list.
set -euo pipefail
unset MECHBENCH_API_KEY MECHBENCH_REGISTRATION_TOKEN

SCRIPT="${1:-$(cd "$(dirname "$0")" && pwd)/dedicated-cuda.sh}"
[ -r "$SCRIPT" ] || { echo "cannot read $SCRIPT" >&2; exit 1; }
for tool in mechbench base64 pbcopy awk; do
  command -v "$tool" > /dev/null || { echo "needs $tool" >&2; exit 1; }
done

TMPS=()
cleanup() { for d in "${TMPS[@]+"${TMPS[@]}"}"; do rm -rf "$d"; done; }
trap cleanup EXIT

ask() {  # ask PROMPT VAR: read a secret without echoing it
  local got
  IFS= read -r -s -p "$1" got
  echo >&2
  printf -v "$2" '%s' "$got"
}

ask "registration token for enverge-spark-host (mbr_…): " TOKEN_HOST
ask "registration token for enverge-spark-peer (mbr_…, Enter to skip the peer): " TOKEN_PEER
ask "Hugging Face read token (Enter to skip): " HF
IFS= read -r -p "the peer Spark's address as the host sees it (Enter if unknown: the host finds it; not secret): " PEER
case "$TOKEN_HOST" in mbr_*) ;; *) echo "the host token must start mbr_" >&2; exit 1 ;; esac
case "$TOKEN_PEER" in ""|mbr_*) ;; *) echo "the peer token must start mbr_" >&2; exit 1 ;; esac
case "$PEER" in *[!A-Za-z0-9.@:_-]*) echo "the peer address has unexpected characters" >&2; exit 1 ;; esac
case "$HF" in *[!A-Za-z0-9_-]*) echo "the Hugging Face token has unexpected characters" >&2; exit 1 ;; esac

register() {  # register NAME TOKEN HOME -> prints the base64 of its config.toml
  local name="$1" token="$2" home="$3"
  if ! printf '%s\n' "$token" | HOME="$home" mechbench login --token - --name "$name" \
       > /dev/null 2> "$home/login.err"; then
    echo "registering $name failed:" >&2
    cat "$home/login.err" >&2
    return 1
  fi
  local extra
  extra="$(cd "$home" && find . -mindepth 1 -maxdepth 1 ! -name .mechbench ! -name login.err)"
  [ -z "$extra" ] || echo "note: login also wrote $extra under its temporary HOME (deleted)" >&2
  [ -s "$home/.mechbench/config.toml" ] || { echo "$name: no credential file written" >&2; return 1; }
  grep -q '^cli_key = ' "$home/.mechbench/config.toml" \
    || echo "note: $name's credentials carry no CLI key; the box mints one on first use" >&2
  base64 < "$home/.mechbench/config.toml" | tr -d '\n'
}

HOME_HOST="$(mktemp -d -t mechbench-box)"; TMPS+=("$HOME_HOST")
HOME_PEER="$(mktemp -d -t mechbench-box)"; TMPS+=("$HOME_PEER")
REGISTERED="enverge-spark-host"
CRED_HOST="$(register enverge-spark-host "$TOKEN_HOST" "$HOME_HOST")"
CRED_PEER=""
if [ -n "$TOKEN_PEER" ]; then
  CRED_PEER="$(register enverge-spark-peer "$TOKEN_PEER" "$HOME_PEER")"
  REGISTERED="$REGISTERED, enverge-spark-peer"
fi
unset TOKEN_HOST TOKEN_PEER

OUT="$(CH="$CRED_HOST" CP="$CRED_PEER" HFT="$HF" PH="$PEER" awk '
  /^MECHBENCH_CREDENTIALS_HOST=/ { print "MECHBENCH_CREDENTIALS_HOST=\"" ENVIRON["CH"] "\""; next }
  /^MECHBENCH_CREDENTIALS_PEER=/ { print "MECHBENCH_CREDENTIALS_PEER=\"" ENVIRON["CP"] "\""; next }
  /^MECHBENCH_TOKEN=/            { print "MECHBENCH_TOKEN=\"\""; next }
  /^MECHBENCH_TOKEN_PEER=/       { print "MECHBENCH_TOKEN_PEER=\"\""; next }
  /^HF_TOKEN=/                   { print "HF_TOKEN=\"" ENVIRON["HFT"] "\""; next }
  /^PEER_HOST=/                  { print "PEER_HOST=\"" ENVIRON["PH"] "\""; next }
  { print }' "$SCRIPT")"
unset CRED_HOST CRED_PEER HF
printf '%s\n' "$OUT" | pbcopy
echo "copied: $(printf '%s\n' "$OUT" | wc -c | tr -d ' ') bytes, runners registered: $REGISTERED"
