# Building

Two artifacts come out of this repository, and they are built from the same `uv.lock` so
they cannot resolve different dependency versions:

- the **CLI distribution**, a wheel and an sdist;
- the **orchestrator image**, which is what a submitted Job runs.

## The distribution

```console
$ uv build
$ ls dist/
dokimi-0.1.0-py3-none-any.whl
dokimi-0.1.0.tar.gz
```

Hatchling builds it from `src/dokimi`. The wheel installs anywhere with Python
3.14 or newer:

```console
$ uv tool install dist/dokimi-0.1.0-py3-none-any.whl
```

## The orchestrator image

```console
$ docker build -f images/orchestrator/Dockerfile -t orchestrator:dev .
```

The build context is the repository root, not `images/orchestrator/`. The image is built
from `pyproject.toml`, `uv.lock` and `src/` — the same three inputs as the distribution.

Three properties, each a decision rather than a habit:

- **`python:3.14-slim`, not Alpine.** A deliberate departure from the Alpine base used for
  test-workload images: Python wheels are predominantly built against glibc, and musl means
  packages compiled from source or missing wheels entirely.
- **Installed with `uv sync --locked`**, which fails rather than re-resolving if the
  lockfile does not match `pyproject.toml`. "Reproducible" means exactly that — one
  lockfile commit yields one dependency set, in the image and in the distribution alike.
  It is not a claim of bit-identical layers.
- **Robot Framework is installed**, via the `robot` extra, even though the core library
  must stay importable without it. The run's execution phase drives Robot's own runner
  *inside this Job*, so this is one of the two places Robot genuinely is a
  runtime dependency.

The image runs as uid 65532 with `/work` as a writable working directory, which is where
Robot writes its per-pair `output.xml` before `rebot` merges them.

Checking a local build:

```console
$ docker run --rm orchestrator:dev --version
$ docker run --rm --entrypoint id orchestrator:dev -u        # 65532, never 0
```

To run it against a local cluster, see [Setup](setup.md#a-cluster-to-submit-runs-against).

## Releasing

`.github/workflows/release.yml`, triggered by a `v*` tag. The order is the decision, not
an implementation detail:

1. Build and push every image dokimi builds, the orchestrator and `iperf2`, for
   `linux/amd64` and `linux/arm64`. They are one matrix job, `images`, one entry each.
2. Sign each with keyless cosign and attest its build provenance, pushing the attestation
   to the registry.
3. Write the digests the registry returned into `src/dokimi/_image_digest.py`, one
   `<NAME>_IMAGE_DIGEST` per image. A release whose images and constants differ stops here.
4. Verify that each baked-in default now resolves to exactly that digest.
5. `uv build`, then create or update the GitHub release and attach the distribution.

Adding an image dokimi builds is one entry in the matrix, with its name, Dockerfile and
registry path, plus its line in `src/dokimi/_image_digest.py`.

The image comes first because a released CLI must ship the digest of the image built from
its own commit. The consequence, accepted on purpose, is that the
CLI and the image cannot be released independently.

```console
$ git tag v0.1.0 && git push origin v0.1.0
```

`workflow_dispatch` with `publish` left off rehearses the build without pushing or signing
anything.

`_image_digest.py` is generated. It is `None` in a source checkout, which is how a
development build knows it has no published image to submit. Do not set it by hand — the
workflow rewrites one specific line, and `tests/test_image_build.py` checks that the line
it looks for still exists.

## CI

`.github/workflows/ci.yml`, on every pull request and every push to `main`:

| Job | What it proves |
| --- | --- |
| Lint, format and type-check | `ruff check`, `ruff format --check`, `mypy --strict` |
| Tests | the suite, with all extras |
| Core library without Robot Framework | the library works with Robot absent — installs without extras, asserts the distribution really is missing, runs the suite |
| Orchestrator image | the image builds, its entry point runs, it is not root, and Robot Framework *is* present in it |

`.github/workflows/test-env.yml` provisions the [test environment](../test-env/README.md): every
profile, each checked and torn down, and the two tools' default profiles provisioned a second
time to prove teardown leaves nothing behind. It runs when `test-env/` changes, weekly, and on
demand, rather than on every pull request, because seven clusters cost minutes that an
unrelated change gains nothing from. Its job summary states, on every run, that the
environment validates orchestration and not measurement.

Every action in all three workflows is pinned to a commit SHA, with its version in a trailing
comment, such as `actions/checkout@11d5960a… # v4.4.0`. A tag can be moved to different code,
and the release workflow runs with permission to push images and sign them.
`tests/test_workflows.py` fails on any action that isn't pinned. To upgrade one, replace both
the SHA and the comment.

The third job in `ci.yml` is the one that keeps an architectural rule real rather than aspirational.
It deliberately does not test `import robot`: this repository has a `robot/` directory at
its root, which Python treats as a namespace package, so that import succeeds with Robot
Framework absent and proves nothing. It asks the installed distribution instead.
