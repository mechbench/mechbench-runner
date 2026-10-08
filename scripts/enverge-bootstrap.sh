#!/bin/bash
# Turns the completed dedicated-cuda.sh on the clipboard (from
# prepare-box-credentials.sh) into a startup script under Enverge's 16 KB
# limit: the six settings, then a download of dedicated-cuda.sh from the
# public repository at a pinned commit, checked by SHA-256, written out with
# the settings in place and run. The result replaces the clipboard; nothing
# secret is printed.
set -euo pipefail

COMMIT="9914fd9f789e7733130629a1186ad2d4627ac6d6"
SHA256="d31621a4cb25e89c48c9852fb6a3138431f365d11cfd73e41b1380aacf3b02aa"

full="$(pbpaste)"
case "$full" in
  *"MECHBENCH_CREDENTIALS_HOST="*) ;;
  *) echo "the clipboard does not hold the completed startup script; run prepare-box-credentials.sh again" >&2; exit 1 ;;
esac

settings="$(printf '%s\n' "$full" | awk -F= '/^(MECHBENCH_CREDENTIALS_HOST|MECHBENCH_CREDENTIALS_PEER|MECHBENCH_TOKEN|MECHBENCH_TOKEN_PEER|HF_TOKEN|PEER_HOST)="/ && !seen[$1]++')"
[ "$(printf '%s\n' "$settings" | wc -l | tr -d ' ')" = 6 ] || { echo "expected six settings on the clipboard" >&2; exit 1; }

cat <<EOF | pbcopy
#!/bin/bash
# mechbench: the DGX Spark session's startup. Downloads scripts/dedicated-cuda.sh
# from github.com/mechbench/mechbench-runner at a pinned commit, checks it, writes
# it with these settings in place, and runs it.
$settings
COMMIT="$COMMIT"
SHA256="$SHA256"
umask 077
F="\$HOME/mechbench-startup.sh"
for i in 1 2 3 4 5; do
  curl -fsSL "https://raw.githubusercontent.com/mechbench/mechbench-runner/\$COMMIT/scripts/dedicated-cuda.sh" -o "\$F.src" && break
  sleep 15
done
echo "\$SHA256  \$F.src" | sha256sum -c - || { echo "dedicated-cuda.sh failed its checksum; stopping" >&2; exit 1; }
export MECHBENCH_CREDENTIALS_HOST MECHBENCH_CREDENTIALS_PEER MECHBENCH_TOKEN MECHBENCH_TOKEN_PEER HF_TOKEN PEER_HOST
python3 - "\$F.src" "\$F" <<'PY'
import os, sys
names = ("MECHBENCH_CREDENTIALS_HOST", "MECHBENCH_CREDENTIALS_PEER", "MECHBENCH_TOKEN",
         "MECHBENCH_TOKEN_PEER", "HF_TOKEN", "PEER_HOST")
with open(sys.argv[1]) as src, open(sys.argv[2], "w") as out:
    for line in src:
        name = line.split("=", 1)[0]
        if name in names and line.startswith(name + '=""'):
            line = f'{name}="{os.environ[name]}"\n'
        out.write(line)
PY
rm -f "\$F.src"
exec bash "\$F"
EOF
echo "copied: $(pbpaste | wc -c | tr -d ' ') bytes (limit 16384); dedicated-cuda.sh at ${COMMIT:0:7}"
