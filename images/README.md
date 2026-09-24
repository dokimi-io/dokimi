# Container images

Dockerfiles for the images this project builds and publishes to
`ghcr.io/test-orchestrator/<image-name>`, pinned by digest and signed with provenance
attestation (ADR-05-04, ADR-05-08).

| Image | Base | Arrives with |
| --- | --- | --- |
| `orchestrator` | `python:3.14-slim`, built with `uv sync --locked` against the repository-root lockfile | M1-02 (adrs#37) |
| One per test type, tool plus its exporter | slim Alpine, with a glibc exception where a tool needs it | M1-10 (adrs#45) |

Empty for now by design: adrs#36 fixes the toolchain and layout only.

Version floors for the base images and the build toolchain are fixed by
[ADR-01-09](https://github.com/Test-Orchestrator/test-orchestrator-adrs/blob/main/adrs/01-09-implementation-dependency-version-floors.md),
not here — this table records which image uses which base, not what the floor should be.
