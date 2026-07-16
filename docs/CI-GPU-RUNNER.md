# Self-hosted GPU runner (real inference in CI)

The `acceptance-gpu` job in [.github/workflows/ci.yml](../.github/workflows/ci.yml) runs real inference on an
NVIDIA GPU at release-tag time. It is the only tier that proves a tagged release actually serves tokens on a
GPU (the per-PR gate is CPU only). CI cannot rent a GPU, so this job runs on a machine you register as a
self-hosted runner. Until one is registered and enabled, the job is skipped, and a tag ships with no GPU proof.

## What the job does

It is a matrix over `engine: [prebuilt, from-source]`:

- **prebuilt** (gates the release, verifies what users receive): `bob build` with no flags routes through
  `lifecycle.ensure_engine` prebuilt-first, so it resolves the row, downloads the published asset, SHA-verifies
  it, and stages it driver-only. Then it serves and runs `scripts/smoke.py --up --require-gpu --expect-source
  prebuilt`. A broken resolver or a binary that will not launch falls back to source, which fails the
  `--expect-source prebuilt` assertion, so the release reds here instead of shipping.
- **from-source** (keeps the reproducible fallback from rotting): `bob build --from-source`, then
  `scripts/smoke.py --up --require-gpu --expect-source source`.

`acceptance-gpu` `needs: [publish-manifest]`, because the prebuilt leg downloads the `engines.json` and asset
that `publish-manifest`/`publish-engines-*` upload on the same tag. It only starts once those assets exist.

## One-time setup

1. **Register the runner** (on the GPU box, an NVIDIA driver installed):
   - GitHub -> repo **Settings -> Actions -> Runners -> New self-hosted runner**, pick Linux x64, and follow
     the `./config.sh` steps it shows (they include the registration token).
   - When it asks for labels, add **`gpu`** (the `self-hosted` label is added automatically). The job targets
     `runs-on: [self-hosted, gpu]`.
   - Run it as a service so it survives reboots: `sudo ./svc.sh install && sudo ./svc.sh start`.
2. **Enable the job**: set the repository variable **`GPU_RUNNER=true`** (Settings -> Secrets and variables ->
   Actions -> Variables -> New repository variable). The job gate is
   `startsWith(github.ref, 'refs/tags/v') && vars.GPU_RUNNER == 'true'`, so both a release tag and this variable
   are required. Unset or set it to anything else to disable the job (for example while the box is offline).
3. **Confirm the toolchain on the box**: an NVIDIA driver is required for both legs; the from-source leg also
   needs a CUDA toolchain (`bob build --from-source` provisions what it can, but a build host needs `nvcc`).
   The prebuilt leg needs only the driver.

## Verifying it locally

On the GPU box, after a prebuilt `bob build` and `bob up`:

```
python scripts/smoke.py --up --require-gpu --expect-source prebuilt
```

`--require-gpu` fails if the staged engine's `bin/.build-tier.json` marker is not the `gpu` tier (a silent CPU
fallback leaves the GPU idle); `--expect-source` fails if the provenance is not the expected `prebuilt` or
`source`.

## Security

Self-hosted runners execute workflow code from the repo. Do **not** enable this runner for untrusted pull
requests from forks (the default for private repos is safe; for public repos, keep fork-PR runs on
GitHub-hosted runners only). The job here is gated to release tags on the repo, not fork PRs.

## Residual (out of scope for 1.2.2)

Windows CUDA is built and published by CI but is **not** verified on a real Windows GPU (no Windows GPU box is
available). It stays a known residual for a later 1.2.x patch.
