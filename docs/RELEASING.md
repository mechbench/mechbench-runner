# Releasing mechbench and mechbench-compute

A runner installs only what the platform's signed release manifest
names, with `--require-hashes`, and nothing from PyPI's "newest". So a
release has two halves: the packages reach PyPI, and then a manifest
naming them is signed and published. Both halves run in GitHub Actions;
nothing is uploaded or signed from a laptop.

- **PyPI** publishes through trusted publishing: the `release` workflow
  in each repository, started by a tag, proves who it is to PyPI over
  OIDC. There is no PyPI token anywhere.
- **The release key** (Ed25519) signs the manifest. Its private half is
  a secret in mechbench-api's `release` environment and nowhere else.
  Its public half ships inside the runner
  (`mechbench_runner/release_key.pub`) and inside the API
  (`RELEASE_PUBLIC_KEY` in `src/lib/release_manifest.ts`), which refuses
  to store a manifest that does not verify.

## Day to day

Claude does all of this; nobody types an OTP.

1. **Bump and gate.** On a clean `main` that is up to date with origin:
   bump `version` in `pyproject.toml`, add the `CHANGELOG.md` entry (both
   headings), commit, push, and let the `test` workflow go green. The
   gate can be run locally first: `python scripts/release.py --check`.
2. **Tag.** `git tag v<version> && git push origin v<version>`. The tag
   must name `pyproject.toml`'s version and sit on `main`; the workflow
   refuses anything else. When a runner release needs a new compute,
   tag compute first and wait for it to publish.
3. **CI publishes.** The `release` workflow runs the same gate
   (`scripts/release.py --check`), builds, and publishes with
   `pypa/gh-action-pypi-publish`. Watch it:
   `gh run watch --repo mechbench/<repo> $(gh run list --repo mechbench/<repo> --workflow release.yml --limit 1 --json databaseId -q '.[0].databaseId')`.
4. **Wait for PyPI to serve the files.** The CDN lags one to five
   minutes: poll `https://pypi.org/pypi/<dist>/<version>/json` until it
   answers 200 for both packages the manifest will name.
5. **Publish the manifest.** Dry run first, then for real:

   ```bash
   gh workflow run publish-release-manifest.yml --repo mechbench/mechbench-api \
     -f runner=0.53.0 -f compute=0.175.0 -f apply=false
   gh workflow run publish-release-manifest.yml --repo mechbench/mechbench-api \
     -f runner=0.53.0 -f compute=0.175.0
   ```

   The workflow (`src/bin/publish-release-manifest.ts`) reads each
   wheel's sha256 from PyPI's JSON API, locks the pair's whole dependency
   closure with `uv pip compile --universal --generate-hashes` (Python
   3.11 and up, every platform), checks the lock against PyPI's hashes,
   signs, and posts it to `POST /releases/manifest`. Check the result:
   `curl -s https://api.mechbench.ai/releases/manifest | jq '{runner: .runner.version, compute: .compute.version, published_at}'`.
6. **Runners follow.** Under `upgrades.compute: auto` a runner installs
   the manifest within the hour; `update` from the site (or
   `mechbench update` on the machine) installs it at once. A runner that
   fails its self-check afterwards restores the install before it.

A release that has to be withdrawn: publish a manifest naming the
earlier versions with `-f allow_downgrade=true`. Runners on the newer
versions follow it back; without the flag they refuse an older manifest.

The old path, `scripts/release.py --upload` (twine from this Mac), stays
for exactly one release, the one that carries the release key (0.53.0),
and is then removed.

## What the runner checks

- The manifest's signature, against the key in its own package. A
  missing or bad signature means no upgrade and one log line.
- The shape: strict versions, https wheel URLs whose file names match
  the distribution and version, 64-hex sha256s, markers that are
  markers, and no second entry for `mechbench` or `mechbench-compute`.
- Each wheel's sha256 after download, before anything is installed.
- An older version than it has only with `allow_downgrade: true`.
- An `update` command naming a version is carried out only when the
  current manifest names that same version.

It then installs with `uv pip install --require-hashes -r <file>`, where
the file pins every locked dependency to its hashes and our two wheels to
the downloaded files and the manifest's sha256.

## One-time setup (Benji)

1. **Two-factor authentication on PyPI.** On the account that owns
   `mechbench` and `mechbench-compute`: pypi.org → Account settings →
   Two factor authentication, with a security key or an authenticator
   app. Save the recovery codes in the password manager.
2. **A trusted publisher for each project.** pypi.org → Your projects →
   `mechbench` → Settings → Publishing → Add a new publisher → GitHub:
   - Owner `mechbench`, Repository name `mechbench-runner`, Workflow name
     `release.yml`, Environment name `pypi`.

   The same for `mechbench-compute`: Owner `mechbench`, Repository name
   `mechbench-compute`, Workflow name `release.yml`, Environment name
   `pypi`.
3. **A `pypi` environment in each repository.** github.com/mechbench/mechbench-runner
   → Settings → Environments → New environment `pypi`, and the same in
   mechbench-compute. Recommended: Deployment branches and tags →
   Selected → tag pattern `v*`; Required reviewers → yourself, if you
   want to approve each publish.
4. **The release key.** On your machine, in a directory outside every
   repository:

   ```bash
   uv run --with cryptography python -c "import os; from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey as K; from cryptography.hazmat.primitives import serialization as s; os.umask(0o077); k = K.generate(); open('release_key.pem', 'wb').write(k.private_bytes(s.Encoding.PEM, s.PrivateFormat.PKCS8, s.NoEncryption())); open('release_key.pub', 'wb').write(k.public_key().public_bytes(s.Encoding.PEM, s.PublicFormat.SubjectPublicKeyInfo))"
   ```

   - `release_key.pub` is public: hand it to Claude, who commits it as
     `mechbench-runner/mechbench_runner/release_key.pub` and as
     `RELEASE_PUBLIC_KEY` in mechbench-api's `src/lib/release_manifest.ts`.
     The runner's gate refuses to release a build without it.
   - `release_key.pem` is the private half. Create mechbench-api →
     Settings → Environments → New environment `release` (Required
     reviewers → yourself, recommended), then store it there:
     `gh secret set MECHBENCH_RELEASE_KEY --repo mechbench/mechbench-api --env release < release_key.pem`.
     Put a copy in the password manager and delete the file.
5. **The API key the manifest workflow posts with.** Mint an API key on
   your (platform admin) account named `release manifest`, and store it:
   `gh secret set MECHBENCH_RELEASE_API_KEY --repo mechbench/mechbench-api --env release`.
6. **After the first CI publish of each package works,** delete the PyPI
   API token twine used (pypi.org → Account settings → API tokens) and
   remove it from this Mac (`~/.pypirc`, or the keyring entry).
7. **Optional: the manifest follows the tag without the PM.** Add a
   fine-grained token with Actions: write on mechbench/mechbench-api as
   the secret `MECHBENCH_API_DISPATCH_TOKEN` in mechbench-runner and
   mechbench-compute, and uncomment the `manifest` job in their
   `release.yml`.

## The changeover

1. Benji generates the key (step 4 above); Claude commits the public
   half to the runner and the API and deploys the API.
2. 0.53.0 is released the old way, `scripts/release.py --upload`, once.
3. The first manifest is published for 0.53.0 and the compute of the
   day (the `release` environment's secrets must exist by then).
4. Runners before 0.53.0 still pick PyPI's newest on their own hourly
   check, which brings them to 0.53.0 or later; from 0.53.0 on they
   follow only the manifest.
5. Every release after that goes through the tag. The `--upload` path
   is removed in the first of them.

## Rotating the key

A runner trusts only the key it shipped with. To rotate: commit the new
public half, release a runner carrying it, publish that runner with a
manifest signed by the old key, and only then replace
`MECHBENCH_RELEASE_KEY` and `RELEASE_PUBLIC_KEY`. A runner that skipped
the carrying release is reinstalled by hand
(`uv tool install --reinstall mechbench`).
