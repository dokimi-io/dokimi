# Container images

Dockerfiles for the images this project builds and publishes to
`ghcr.io/test-orchestrator/<image-name>`, pinned by digest and signed with provenance
attestation (ADR-05-04, ADR-05-08).

| Image | Base | Arrives with |
| --- | --- | --- |
| `orchestrator` | `python:3.12-slim`, built with `uv sync --locked` against the repository-root lockfile | M1-02 (#37) |
| One per test type, tool plus its exporter | slim Alpine, with a glibc exception where a tool needs it | M1-10 (#45) |

Empty for now by design: issue #36 fixes the toolchain and layout only.
