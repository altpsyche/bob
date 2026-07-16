# GPU acceptance (real inference at release time)

The per-PR CI gate is CPU only. Proving a tagged release actually serves tokens on an NVIDIA GPU is done
**locally by the maintainer at release time**, not in CI. This repo is public, and a self-hosted GPU runner
is an unacceptable standing attack surface: a `pull_request` from a fork runs the workflow file from the PR's
own commit, so a hostile fork could target the runner and execute arbitrary code on the box. So there is no
`acceptance-gpu` CI job; the resolver is still guarded in CI by the hermetic `TestManifestContract` (every PR)
and `manifest-contract-live` (scheduled, against the real published manifest).

## The check

`scripts/smoke.py` provisions the running stack, runs real inference (`bob agent "say hi"` + the agent-server
contract), and with the extra flags asserts the engine tier and provenance from `bin/.build-tier.json`:

- `--require-gpu` FAILs if the staged engine is not the `gpu` tier (a silent CPU fallback leaves the GPU idle).
- `--expect-source prebuilt|source` FAILs if the provenance is not what you expect.

## Release-time runbook

Run on your NVIDIA box.

**1. Cut the release** (working tree only, then review, commit, push, tag):
```
bob release 1.2.2
git diff                       # review VERSION + versions.lock + CHANGELOG
git commit -am "release: 1.2.2"
git push
bob release 1.2.2 --tag        # creates v1.2.2 at HEAD (after the commit)
git push origin v1.2.2
```

**2. Wait for the publish jobs** (`publish-engines-*` + `publish-manifest`) to upload the driver-only assets and
`engines.json` to the release. The prebuilt binary must exist before you can verify what users receive.

**3. Verify the published prebuilt on the GPU.** Check out the tag so the commit-match guard resolves the
prebuilt (on `main`, ahead of the tag, it correctly falls back to source):
```
git fetch --tags && git checkout v1.2.2
bob build                      # prebuilt-first: download + SHA-verify + stage; writes the tier marker
bob up -NoOpen
python scripts/smoke.py --up --require-gpu --expect-source prebuilt
```
Green means the exact binary users download serves tokens on the GPU. **If it fails, the release is
broken-on-arrival**: delete the tag, fix, and re-cut. Never mutate a shipped-good tag.

**Optional pre-cut sanity check** (before step 1; proves your GPU and the assertion work without publishing,
but it recompiles CUDA — tens of minutes — and replaces your current engine):
```
bob build --from-source && bob up -NoOpen
python scripts/smoke.py --up --require-gpu --expect-source source
```

Record the smoke output (the `PASS`/`FAIL` lines) with the release notes as the GPU acceptance evidence.

## If you ever want to automate this

Automating GPU acceptance means a self-hosted runner, which on a public repo you must not do without clearing a
real security bar: require approval for all fork-PR workflows (Settings -> Actions -> General), gate the job
behind an environment with a required reviewer, and run the runner ephemerally inside a disposable VM. That was
weighed and declined for this project; the local runbook above is the supported path.
