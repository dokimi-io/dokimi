# syntax=docker/dockerfile:1

# The orchestrator's own image: the one-shot in-cluster Job a run submits (ADR-01-06).
#
# Base is `python:3.12-slim`, deliberately not the Alpine base ADR-05-04 picks for
# test-workload images: the Python ecosystem's wheels are predominantly built against
# glibc, and musl means packages compiled from source or missing wheels entirely
# (ADR-05-08).
#
# Everything is installed from the repository-root `uv.lock` with `uv sync --locked`,
# which fails rather than re-resolving if the lockfile does not match `pyproject.toml`.
# The same lockfile builds the CLI distribution, so the two cannot drift apart.

ARG PYTHON_VERSION=3.12
ARG UV_VERSION=0.9.7

FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv

FROM python:${PYTHON_VERSION}-slim AS build

COPY --from=uv /uv /usr/local/bin/uv

ENV UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/orchestrator

WORKDIR /src

# Dependencies first, from the lockfile alone, so editing source does not re-resolve or
# re-download anything.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --extra robot --no-install-project

# The `robot` extra above is not a contradiction of ADR-01-02's no-Robot rule. The core
# library must stay importable without Robot Framework, and CI proves it does; but phase
# 6 of a run executes the rendered suite through Robot's own runner inside this Job
# (ADR-01-08), so this image is one of the places Robot genuinely is a runtime
# dependency. The CLI distribution installs without it.

COPY README.md LICENSE ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --extra robot --no-editable


FROM python:${PYTHON_VERSION}-slim AS runtime

LABEL org.opencontainers.image.source="https://github.com/Test-Orchestrator/test-orchestrator" \
      org.opencontainers.image.description="Test Orchestrator: one-shot in-cluster orchestrator Job" \
      org.opencontainers.image.licenses="Apache-2.0"

# Non-root, as ADR-05-04 requires of every image this project publishes. The orchestrator
# talks to the API server and reads exporter endpoints; it has no reason to be root, and
# the host-network exception ADR-05-03 carves out is for test workloads, not for this.
RUN groupadd --gid 65532 orchestrator \
 && useradd --uid 65532 --gid 65532 --no-create-home --shell /usr/sbin/nologin orchestrator

COPY --from=build --chown=root:root /opt/orchestrator /opt/orchestrator

# Robot writes `output.xml` per pair and `rebot` merges them (ADR-01-08, phase 9a), so
# the Job needs somewhere writable that is not the read-only application directory.
RUN install -d -o 65532 -g 65532 /work

ENV PATH="/opt/orchestrator/bin:${PATH}" \
    PYTHONUNBUFFERED=1

USER 65532:65532
WORKDIR /work

ENTRYPOINT ["test-orchestrator"]
