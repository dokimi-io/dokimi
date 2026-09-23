# Container images

Dockerfiles for the images this project builds and publishes to
`ghcr.io/test-orchestrator/<image-name>`, pinned by digest and signed with provenance
attestation (ADR-05-04, ADR-05-08).

| Image | Base | Status |
| --- | --- | --- |
| [`orchestrator`](orchestrator/Dockerfile) | `python:3.12-slim`, built with `uv sync --locked` against the repository-root lockfile | M1-02 (#37) |
| One per test type, tool plus its exporter | slim Alpine, with a glibc exception where a tool needs it | M1-10 (#45) |

## `orchestrator`

The one-shot in-cluster Job a run is (ADR-01-06): phases 2 to 11 of a run happen inside
this image, and the local CLI only submits it.

```console
$ docker build -f images/orchestrator/Dockerfile -t orchestrator:dev .
$ docker run --rm orchestrator:dev --version
```

Note the build context is the repository root, not this directory — the image is built
from `pyproject.toml`, `uv.lock` and `src/`, the same three inputs the CLI distribution
is built from.

Three properties worth stating, because each is a decision rather than a habit:

- **`python:3.12-slim`, not Alpine.** A deliberate departure from ADR-05-04's base for
  test-workload images: Python wheels are predominantly built against glibc, and musl
  means packages compiled from source or missing wheels entirely (ADR-05-08).
- **Installed with `uv sync --locked`,** which fails rather than re-resolving if the
  lockfile does not match `pyproject.toml`. "Reproducible" here means exactly that — one
  lockfile commit yields one dependency set, in the image and in the CLI distribution
  alike. It is not a claim of bit-identical layers.
- **Robot Framework is installed,** via the `robot` extra, even though the core library
  must stay importable without it. Phase 6 of a run executes the rendered suite through
  Robot's own runner *inside this Job* (ADR-01-08), so this is one of the two places
  Robot genuinely is a runtime dependency. The CLI distribution installs without it, and
  CI proves the library still works when it is absent.

The image runs as uid 65532 with `/work` as a writable working directory, which is where
Robot writes its per-pair `output.xml` before `rebot` merges them (phase 9a).

## Publishing

`.github/workflows/release.yml`, on a `v*` tag. It builds and pushes for `linux/amd64`
and `linux/arm64`, signs the result with keyless cosign, attests build provenance, and
only then writes the digest it got back into `src/test_orchestrator/_image_digest.py` and
builds the distribution — so a released CLI always carries the digest of the image built
from its own commit (ADR-05-08).

`workflow_dispatch` with `publish` left off rehearses the build without pushing or
signing anything.
