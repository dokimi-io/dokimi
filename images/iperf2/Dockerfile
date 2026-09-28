# syntax=docker/dockerfile:1

# The iperf2 test-workload image: both the client and the server role (ADR-05-04).
#
# Slim Alpine, as ADR-05-04 decides for test workloads: iperf2 is a C program with no
# glibc dependency, and Alpine keeps a shell and busybox's nc and netstat, which the
# client's start trigger and both readiness probes use. The exporter sidecar joins this
# image with M1-10, so the parser always matches the tool version it parses.
#
# Both the base and the package are pinned. The base by digest, because a tag is a
# pointer someone else can move; the package by exact version, because iperf2's output
# format is what the exporter parses, and the exporter is written against one version.

ARG ALPINE_VERSION=3.24.2
ARG ALPINE_DIGEST=sha256:294b683cb724975bec92580e1e685676bd4b50bda910ddb8c51d4cabeaec77e6

FROM alpine:${ALPINE_VERSION}@${ALPINE_DIGEST}

ARG IPERF_VERSION=2.2.1-r0

LABEL org.opencontainers.image.source="https://github.com/dokimi-io/dokimi" \
      org.opencontainers.image.description="dokimi: iperf2 test workload" \
      org.opencontainers.image.licenses="Apache-2.0"

RUN apk add --no-cache "iperf=${IPERF_VERSION}"

COPY --chmod=0755 images/iperf2/dokimi-iperf2-client.sh /usr/local/bin/dokimi-iperf2-client

# Non-root (ADR-05-04). Pod network only in Milestone-01 (ADR-05-03), so nothing here
# needs a privileged port, raw sockets or host networking. The same uid as the
# orchestrator image, and the pod spec states it too.
USER 65532:65532
WORKDIR /tmp
