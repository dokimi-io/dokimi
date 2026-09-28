"""``iperf2``, the first test type ([ADR-02-01], [ADR-02-06]).

A client/server pod pair over the pod network, addressed by pod IP ([ADR-05-03]). The
client runs on the source node and sends; the server runs on the target node and
receives. Milestone-01 exercises UDP only, but the parameter schema accepts TCP too:
that is where :meth:`Iperf2.capabilities` answers differently, which is the point of
declaring capabilities per parameter set.

**How a test starts without ``pods/exec``.** Both pods must be ``Ready`` before
:meth:`Iperf2.start` ([ADR-05-07]), so the client cannot simply begin measuring when its
container starts. It starts idle instead, listening on a trigger port, and ``start()``
connects to that port and sends the server's pod IP — known only once the server is
``Ready`` ([ADR-05-03]). The alternative, ``exec`` into the client, would add
``pods/exec`` to the Job's grant, and exec is a shell in every pod of the namespace. The
trigger needs nothing the run does not already need: the orchestrator reaches pod IPs
directly anyway, to read the exporter for the verdict ([ADR-03-01]).

**Completion is process exit.** The client script runs the discarded warm-up pass, then
the measurement with ``-y C``, writes the final CSV summary line — the completion marker —
to the container's termination message, and exits with ``iperf``'s own status.
:meth:`Iperf2.is_complete` reads that from the pod's status, again without ``exec``.

**The warm-up pass runs by default** ([ADR-04-04]): same path, same parameters, output
discarded, for :data:`~dokimi.k8s.deadlines.DEFAULT_WARMUP_SECONDS`. It is never silent —
``warmup_duration_seconds`` is in the workload's run metadata, ``0`` when disabled — and
the ``warmup`` parameter disables it per test.
"""

from __future__ import annotations

import socket
import time
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from dokimi import _image_digest
from dokimi.k8s.deadlines import DEFAULT_WARMUP_SECONDS
from dokimi.k8s.resources import POD, Manifest, NotFoundError
from dokimi.model import Bandwidth, ConfigError, Duration, Protocol
from dokimi.testtypes.images import resolve_workload_image
from dokimi.testtypes.interface import (
    Capability,
    ClusterFacts,
    DeployedWorkload,
    ExporterSpec,
    ImagePair,
    Parameter,
    ParameterSchema,
    Placement,
    Role,
    TestParameters,
    WorkloadSpec,
)
from dokimi.testtypes.lifecycle import NotReadyError, WorkloadLostError, pod_is_ready

__all__ = [
    "CONTAINER_NAME",
    "IMAGE_NAME",
    "IPERF_PORT",
    "TRIGGER_PORT",
    "Completion",
    "Iperf2",
    "Trigger",
    "tcp_trigger",
]

IMAGE_NAME = "iperf2"
#: The tool's container in both pods. The exporter sidecar (M1-10) sits next to it.
CONTAINER_NAME = "iperf2"
#: ``iperf``'s own default port.
IPERF_PORT = 5001
#: Where the idle client listens for :meth:`Iperf2.start`.
TRIGGER_PORT = 5101
#: The tool writes its native output here, and the exporter parses it from here.
RESULTS_DIRECTORY = "/results"
#: The uid the image runs as, shared with the orchestrator image ([ADR-05-04]).
_UID = 65532

#: Sends *payload* to ``host:port``. Injected so tests need no network.
Trigger = Callable[[str, int, bytes], None]

_CAPABILITIES: dict[Protocol, frozenset[Capability]] = {
    # Jitter and packet loss are what iperf2's UDP server report adds; TCP has neither.
    Protocol.TCP: frozenset({Capability.THROUGHPUT}),
    Protocol.UDP: frozenset({Capability.THROUGHPUT, Capability.PACKET_LOSS, Capability.JITTER}),
}


def _parse_protocol(text: str) -> Protocol:
    return Protocol.parse(text)


def _parse_warmup(text: str) -> Duration:
    """A duration, or zero to disable the pass. Zero is the only duration allowed to be."""
    if text.strip() in ("0", "0s"):
        return Duration(Decimal(0))
    try:
        return Duration.parse(text)
    except ConfigError as error:
        raise ConfigError(f"warmup: {error}. Use '0s' to disable the warm-up pass") from None


_SCHEMA = ParameterSchema(
    test_type=IMAGE_NAME,
    parameters=(
        Parameter(
            "protocol",
            _parse_protocol,
            "Transport to measure over. UDP adds packet loss and jitter to throughput.",
            choices=(Protocol.UDP.value, Protocol.TCP.value),
        ),
        Parameter(
            "targetBandwidth",
            Bandwidth.parse,
            "The rate the client sends at, with decimal K, M or G prefixes: '1G'.",
        ),
        Parameter("duration", Duration.parse, "How long the measurement runs: '30s'."),
        Parameter(
            "warmup",
            _parse_warmup,
            "The discarded warm-up pass before the measurement ([ADR-04-04]). '0s' disables it.",
            default=f"{DEFAULT_WARMUP_SECONDS:g}s",
        ),
    ),
)


@dataclass(frozen=True, slots=True)
class Completion:
    """How the client finished: ``iperf``'s exit status and its final summary line."""

    exit_code: int
    #: The last CSV line ``iperf -y C`` printed, the completion marker. Empty if the
    #: client failed before producing one.
    summary: str
    reason: str

    @property
    def succeeded(self) -> bool:
        return self.exit_code == 0


class Iperf2:
    """The ``iperf2`` test type."""

    name = IMAGE_NAME

    def __init__(self, *, image: str | None = None, trigger: Trigger | None = None) -> None:
        """*image* overrides the release-default image, for air-gapped or mirrored
        registries ([ADR-05-04]); it is used exactly as given and recorded as such."""
        self._image_override = image
        self._trigger = trigger if trigger is not None else tcp_trigger

    # -- declaration ------------------------------------------------------------------------

    def images(self) -> ImagePair:
        # One image for both roles: the same binary is client and server.
        image = resolve_workload_image(
            IMAGE_NAME, _image_digest.IPERF2_IMAGE_DIGEST, self._image_override
        )
        return ImagePair(client=image, server=image)

    def parameter_schema(self) -> ParameterSchema:
        return _SCHEMA

    def capabilities(
        self, params: TestParameters, environment: ClusterFacts | None = None
    ) -> frozenset[Capability]:
        # Depends on the parameters only; iperf2 needs no fact about the cluster.
        return _CAPABILITIES[params.get("protocol", Protocol)]

    def exporter(self) -> ExporterSpec:
        return ExporterSpec(
            container_name="exporter",
            port=9101,
            path="/metrics",
            results_directory=RESULTS_DIRECTORY,
        )

    # -- workload ---------------------------------------------------------------------------

    def workload(self, params: TestParameters, placement: Placement) -> WorkloadSpec:
        images = self.images()
        protocol = params.get("protocol", Protocol)
        warmup = params.get("warmup", Duration)
        pods = {
            Role.SERVER: self._pod(
                placement, Role.SERVER, images.server.reference, self._server(protocol)
            ),
            Role.CLIENT: self._pod(
                placement,
                Role.CLIENT,
                images.client.reference,
                self._client(
                    protocol,
                    params.get("targetBandwidth", Bandwidth),
                    params.get("duration", Duration),
                    warmup,
                ),
            ),
        }
        image = images.client
        return WorkloadSpec(
            namespace=placement.namespace,
            test_id=placement.test_id,
            pods=pods,
            run_metadata={
                "test_type": IMAGE_NAME,
                "protocol": protocol.value,
                "target_bandwidth": params.written["targetBandwidth"],
                "duration": params.written["duration"],
                # ADR-04-04's field name. Never absent: 0 is how a disabled pass shows.
                "warmup_duration_seconds": _plain(warmup.seconds),
                # ADR-05-03: the mechanism, recorded with the result from the first run.
                "connectivity_mode": "podNetwork",
                "workload_image": image.reference,
                "workload_image_digest": image.digest or "",
                "workload_image_source": image.source.value,
            },
        )

    def _server(self, protocol: Protocol) -> dict[str, Any]:
        command = ["iperf", "-s", "-p", str(IPERF_PORT)]
        if protocol is Protocol.UDP:
            command.append("-u")
        return {
            "command": command,
            "ports": [
                {"name": "iperf", "containerPort": IPERF_PORT, "protocol": protocol.name},
            ],
            "readinessProbe": _listening(IPERF_PORT),
        }

    def _client(
        self, protocol: Protocol, bandwidth: Bandwidth, duration: Duration, warmup: Duration
    ) -> dict[str, Any]:
        return {
            # The script ships in the image (images/iperf2/dokimi-iperf2-client.sh).
            "command": ["dokimi-iperf2-client"],
            "args": [
                "--protocol",
                protocol.value,
                "--port",
                str(IPERF_PORT),
                "--trigger-port",
                str(TRIGGER_PORT),
                "--bandwidth",
                _plain(bandwidth.bits_per_second),
                "--duration",
                _plain(duration.seconds),
                "--warmup",
                _plain(warmup.seconds),
                "--results",
                RESULTS_DIRECTORY,
            ],
            "ports": [{"name": "trigger", "containerPort": TRIGGER_PORT, "protocol": "TCP"}],
            # Ready means "listening for start()", which is exactly what the gate asks.
            "readinessProbe": _listening(TRIGGER_PORT),
            # The completion marker, readable from the pod's status without exec.
            "terminationMessagePath": "/dev/termination-log",
            "terminationMessagePolicy": "File",
        }

    def _pod(
        self, placement: Placement, role: Role, image: str, container: dict[str, Any]
    ) -> Manifest:
        node = placement.node_for(role)
        return {
            "apiVersion": POD.api_version,
            "kind": POD.kind,
            "metadata": {
                "name": f"run-{placement.run_id}-{placement.test_id}-{role.value}",
                "namespace": placement.namespace,
                "labels": {"app.kubernetes.io/name": IMAGE_NAME},
            },
            "spec": {
                # Through the scheduler, not around it: nodeName would skip taints,
                # resource checks and every reason a pod cannot land, and the gate could
                # never report a scheduling error it was never given ([ADR-05-07]).
                "affinity": {
                    "nodeAffinity": {
                        "requiredDuringSchedulingIgnoredDuringExecution": {
                            "nodeSelectorTerms": [
                                {
                                    "matchFields": [
                                        {
                                            "key": "metadata.name",
                                            "operator": "In",
                                            "values": [node],
                                        }
                                    ]
                                }
                            ]
                        }
                    }
                },
                "restartPolicy": "Never",
                # A test workload never talks to the API server.
                "automountServiceAccountToken": False,
                "securityContext": {
                    "runAsNonRoot": True,
                    "runAsUser": _UID,
                    "runAsGroup": _UID,
                    "fsGroup": _UID,
                    "seccompProfile": {"type": "RuntimeDefault"},
                },
                "containers": [
                    {
                        "name": CONTAINER_NAME,
                        "image": image,
                        **container,
                        "securityContext": {
                            "allowPrivilegeEscalation": False,
                            "readOnlyRootFilesystem": True,
                            "capabilities": {"drop": ["ALL"]},
                        },
                        "volumeMounts": [
                            {"name": "results", "mountPath": RESULTS_DIRECTORY},
                            {"name": "tmp", "mountPath": "/tmp"},
                        ],
                    }
                ],
                "volumes": [
                    # Shared with the exporter sidecar M1-10 adds, which parses from here.
                    {"name": "results", "emptyDir": {}},
                    {"name": "tmp", "emptyDir": {}},
                ],
            },
        }

    # -- execution --------------------------------------------------------------------------

    def start(self, deployed: DeployedWorkload) -> None:
        # A hard precondition, checked here as well as by the gate: start() is the call
        # that sends traffic, so it is the one that must refuse ([ADR-05-07]).
        for role in (Role.SERVER, Role.CLIENT):
            if not pod_is_ready(deployed.pods[role]):
                raise NotReadyError(
                    f"refusing to start {deployed.spec.test_id}: the {role.value} pod "
                    f"{deployed.spec.pod_name(role)} is not Ready"
                )
        server_ip = deployed.pod_ip(Role.SERVER)
        self._trigger(deployed.pod_ip(Role.CLIENT), TRIGGER_PORT, f"{server_ip}\n".encode())

    def is_complete(self, deployed: DeployedWorkload) -> bool:
        return self.completion(deployed) is not None

    def completion(self, deployed: DeployedWorkload) -> Completion | None:
        """How the client finished, or ``None`` while it is still running."""
        name = deployed.spec.pod_name(Role.CLIENT)
        try:
            pod = deployed.cluster.get(POD, deployed.namespace, name)
        except NotFoundError:
            raise WorkloadLostError(
                f"the client pod {name} disappeared before it finished"
            ) from None
        for status in (pod.get("status") or {}).get("containerStatuses") or []:
            if status.get("name") != CONTAINER_NAME:
                continue
            terminated = (status.get("state") or {}).get("terminated")
            if terminated is None:
                return None
            return Completion(
                exit_code=int(terminated.get("exitCode", -1)),
                summary=(terminated.get("message") or "").strip(),
                reason=terminated.get("reason") or "",
            )
        return None

    def cleanup(self, deployed: DeployedWorkload) -> None:
        """Nothing beyond generic pod deletion: iperf2 leaves no state outside its pods."""


def tcp_trigger(
    host: str,
    port: int,
    payload: bytes,
    *,
    attempts: int = 5,
    timeout: float = 5.0,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Connect to *host*:*port*, send *payload*, close.

    A failed connection is retried a few times. The client accepts exactly one
    connection and then stops listening, so a retry can only reach a client that was
    not started by an earlier attempt.
    """
    last: OSError | None = None
    for attempt in range(attempts):
        try:
            with socket.create_connection((host, port), timeout=timeout) as connection:
                connection.sendall(payload)
            return
        except OSError as error:
            last = error
            if attempt + 1 < attempts:
                sleep(1.0)
    raise ConnectionError(f"could not start the client at {host}:{port}: {last}")


def _listening(port: int) -> dict[str, Any]:
    """A readiness probe: something is listening on *port*.

    ``netstat`` rather than a ``tcpSocket`` probe, because a probe that connected to the
    client's trigger port would *be* a trigger. UDP has no connect-probe at all.
    """
    return {
        "exec": {"command": ["sh", "-c", f"netstat -ln | grep -q ':{port} '"]},
        "periodSeconds": 2,
        "failureThreshold": 1,
    }


def _plain(value: Decimal) -> str:
    """A decimal without an exponent or trailing zeros: what iperf2's arguments accept."""
    normalised = value.normalize()
    if normalised == normalised.to_integral_value():
        normalised = normalised.quantize(Decimal(1))
    return format(normalised, "f")
