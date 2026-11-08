# Container images

Dockerfiles for the images this project builds and publishes to
`ghcr.io/dokimi-io/<image-name>`, pinned by digest and signed with provenance
attestation (ADR-05-04, ADR-05-08).

| Image | Base | Status |
| --- | --- | --- |
| [`orchestrator`](orchestrator/Dockerfile) | `python:3.14-slim`, built with `uv sync --locked` against the repository-root lockfile | M1-02 (adrs#37) |
| [`iperf2`](iperf2/Dockerfile) | `alpine:3.24.2`, pinned by digest, with `iperf=2.2.1-r0` and `python3=3.14.10-r1` for its exporter | M1-09 (adrs#44); the exporter with M1-10 (adrs#45) |

Version floors for the base image and the build toolchain are fixed by
[ADR-01-09](https://github.com/dokimi-io/dokimi-adrs/blob/main/adrs/01-09-implementation-dependency-version-floors.md),
not here — this table records which image uses which base, not what the floor should be.

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

- **`python:3.14-slim`, not Alpine.** A deliberate departure from ADR-05-04's base for
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

## `iperf2`

The test workload for both roles of an `iperf2` pair (ADR-05-04): one image, because the
client and the server are the same binary.

```console
$ docker build -f images/iperf2/Dockerfile -t iperf2:dev .
$ docker run --rm iperf2:dev iperf -v
```

The build context is the repository root here too, so that one `.dockerignore` governs
both images; it admits the two role scripts and nothing else from `images/`.

- **Alpine pinned by digest, `iperf` pinned by exact version.** iperf2's output is what the
  exporter parses, so the tool version is part of the measurement's identity, not a detail.
- **The client is [`dokimi-iperf2-client.sh`](iperf2/dokimi-iperf2-client.sh).** It starts
  idle and listens on a trigger port, so both pods can pass the readiness gate before any
  traffic is sent (ADR-05-07). `start()` connects and sends the server's pod IP, which must
  be an IPv4 address. The client then runs the discarded warm-up pass from source port
  5103, runs the measurement with `-y C` from source port 5102, writes the final summary
  line to its termination message, and exits with `iperf`'s status. iperf2 exits `0` even
  when it never connected, so a measurement without a summary line exits `69` instead. The
  orchestrator reads all of that from the pod's status, so the Job needs no `pods/exec`.
- **The server is [`dokimi-iperf2-server.sh`](iperf2/dokimi-iperf2-server.sh).** It runs
  `iperf -s -y C` with its reports in the results volume, because the server is the pod
  that sees packet loss and jitter. Both scripts write an `iperf2.meta` naming the
  measurement's source port, so the exporter can tell the measurement from the warm-up
  ([exporters/README.md](../exporters/README.md)).
- **Both readiness probes use `netstat`, not a `tcpSocket` probe.** A probe that connected
  to the trigger port would itself start the client. Once started, the client stays
  `Ready` on a marker file rather than turning NotReady for its whole measurement.
- **Runs as uid 65532.** It uses the pod network only, so nothing needs a privileged port
  or raw sockets (ADR-05-03).

CI builds it, checks the uid and the tool version, and runs a real server and client on a
Docker network with the same probe commands the pods use, so the whole pod contract is
exercised on every change without a cluster.

## Publishing

`.github/workflows/release.yml`, on a `v*` tag. It builds and pushes both images for
`linux/amd64` and `linux/arm64`, signs each with keyless cosign, attests build provenance,
and only then writes the digests it got back into `src/dokimi/_image_digest.py` and
builds the distribution — so a released CLI always carries the digests of the images built
from its own commit (ADR-05-08, ADR-05-04).

`workflow_dispatch` with `publish` left off rehearses the build without pushing or
signing anything.
