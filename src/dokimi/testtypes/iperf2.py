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

**Each pod writes its own view** ([ADR-02-02]). The server is the one that sees packet
loss and jitter, so it writes an ``iperf -y C`` line per connection to its results
directory too. The warm-up and the measurement send from two fixed source ports, and
both pods record the measurement's in an ``iperf2.meta`` file next to their output, so
the exporter picks the measured connection by its port rather than by its position.

**The warm-up pass runs by default** ([ADR-04-04]): same path, same parameters, output
discarded, for :data:`~dokimi.k8s.deadlines.DEFAULT_WARMUP_SECONDS`. It is never silent —
``warmup_duration_seconds`` is in the workload's run metadata, ``0`` when disabled — and
the ``warmup`` parameter disables it per test.

**The probe** ([ADR-05-07]) is the same pair with one argument more: the client, started
by the same trigger, makes a one-second exchange with the server and resolves
:data:`PROBE_DNS_NAME`, instead of warming up and measuring. The run-level preflight
deploys it for the first planned pair (:mod:`dokimi.testtypes.probe`).
"""

from __future__ import annotations

import copy
import dataclasses
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
    ADDRESS_FAMILY,
    EXPORTER_PORT_NAME,
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
    WorkloadImage,
    WorkloadSpec,
)
from dokimi.testtypes.lifecycle import NotReadyError, WorkloadLostError, pod_is_ready
from dokimi.testtypes.probe import ProbeCheck, ProbeResult

__all__ = [
    "CONTAINER_NAME",
    "IMAGE_NAME",
    "IPERF_PORT",
    "MEASUREMENT_SOURCE_PORT",
    "REPETITION",
    "PROBE_DNS_NAME",
    "TRIGGER_PORT",
    "WARMUP_SOURCE_PORT",
    "Completion",
    "Iperf2",
    "Trigger",
    "UnexpectedTargetError",
    "tcp_trigger",
]

IMAGE_NAME = "iperf2"
#: The tool's container in both pods. The exporter (M1-10) runs next to it.
CONTAINER_NAME = "iperf2"
#: ``iperf``'s own default port.
IPERF_PORT = 5001
#: Where the idle client listens for :meth:`Iperf2.start`.
TRIGGER_PORT = 5101
#: The client's source port for the measurement, which the server's report names.
MEASUREMENT_SOURCE_PORT = 5102
#: The client's source port for the discarded warm-up pass.
WARMUP_SOURCE_PORT = 5103
#: The name the probe resolves: the API server's Service, which every cluster has, by its
#: short name so that the pod's search path supplies whatever the cluster domain is.
PROBE_DNS_NAME = "kubernetes.default.svc"
#: Present once the client has been started; its readiness probe passes on it from then.
_STARTED_MARKER = "/tmp/dokimi-started"
#: The tool writes its native output here, and the exporter parses it from here.
RESULTS_DIRECTORY = "/results"
#: The uid the image runs as, shared with the orchestrator image ([ADR-05-04]).
_UID = 65532
#: Milestone-01 runs each pair once. ``repetition`` is a required label already
#: ([ADR-02-07]); repetitions themselves arrive with Phase 4.
REPETITION = 1

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


class UnexpectedTargetError(RuntimeError):
    """The client measured against an address other than the server pod's.

    The trigger port accepts one connection from whoever reaches it first, and the client
    measures against whatever that connection sent. A summary naming another destination
    is a result about some other path, and must not be read as this pair's.
    """


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
        # iperf2 exits 0 when it never connected at all. The script turns that into a
        # non-zero exit; this holds without relying on it: no summary is never a success.
        return self.exit_code == 0 and bool(self.summary)

    @property
    def destination(self) -> str | None:
        """The address the summary says was measured against, or ``None`` without one.

        A ``-y C`` line starts ``timestamp,source_ip,source_port,destination_ip,...``.
        """
        fields = self.summary.split(",")
        return fields[3] if len(fields) > 3 else None


class Iperf2:
    """The ``iperf2`` test type."""

    name = IMAGE_NAME

    def __init__(
        self, *, image: WorkloadImage | str | None = None, trigger: Trigger | None = None
    ) -> None:
        """*image* is the image to run instead of the release default ([ADR-05-04]).

        A run passes the :class:`WorkloadImage` it resolved at submission ([ADR-04-08]),
        which keeps where the reference came from. A plain reference is a library caller
        choosing one itself, and is recorded as a run override. Either way it is used
        exactly as given.
        """
        self._image = image
        self._trigger = trigger if trigger is not None else tcp_trigger

    # -- declaration ------------------------------------------------------------------------

    def images(self) -> ImagePair:
        # One image for both roles: the same binary is client and server.
        if isinstance(self._image, WorkloadImage):
            image = self._image
        else:
            image = resolve_workload_image(
                IMAGE_NAME, _image_digest.IPERF2_IMAGE_DIGEST, self._image
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
            port_name=EXPORTER_PORT_NAME,
        )

    # -- workload ---------------------------------------------------------------------------

    def workload(self, params: TestParameters, placement: Placement) -> WorkloadSpec:
        images = self.images()
        protocol = params.get("protocol", Protocol)
        warmup = params.get("warmup", Duration)
        pods = {
            Role.SERVER: self._pod(
                placement,
                Role.SERVER,
                images.server.reference,
                protocol,
                self._server(protocol, warmup),
            ),
            Role.CLIENT: self._pod(
                placement,
                Role.CLIENT,
                images.client.reference,
                protocol,
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
            warmup_seconds=float(warmup.seconds),
            run_metadata={
                "test_type": IMAGE_NAME,
                "protocol": protocol.value,
                "target_bandwidth": params.written["targetBandwidth"],
                "duration": params.written["duration"],
                # ADR-04-04's field name. Never absent: 0 is how a disabled pass shows.
                "warmup_duration_seconds": _plain(warmup.seconds),
                # ADR-05-03: the mechanism and the address family, recorded with the result
                # from the first run. The family is the one DeployedWorkload.pod_ip() picks.
                "connectivity_mode": "podNetwork",
                "address_family": ADDRESS_FAMILY,
                "workload_image": image.reference,
                "workload_image_digest": image.digest or "",
                "workload_image_source": image.source.value,
            },
        )

    def probe(self, params: TestParameters, placement: Placement) -> WorkloadSpec:
        """The pair's own workload, with its client in probe mode ([ADR-05-07]).

        Built by :meth:`workload` and changed in two ways only, so the preflight schedules,
        pulls and connects exactly what the test will: the client gets one argument more,
        and both pods run the tool alone. The exporter goes, because a probe is not a test:
        its pods would otherwise be scraped, and the run's Prometheus would keep a "test"
        that never completed. The image is still pulled, by the tool's container.
        """
        spec = self.workload(params, placement)
        pods = {}
        for role, pod in spec.pods.items():
            probe_pod = copy.deepcopy(pod)
            containers = [c for c in probe_pod["spec"]["containers"] if c["name"] == CONTAINER_NAME]
            if role is Role.CLIENT:
                for container in containers:
                    container["args"] = [*container["args"], "--probe-dns", PROBE_DNS_NAME]
            probe_pod["spec"]["containers"] = containers
            pods[role] = probe_pod
        # The probe skips the warm-up pass, so its workload does not claim one.
        return dataclasses.replace(spec, pods=pods, warmup_seconds=0)

    def _server(self, protocol: Protocol, warmup: Duration) -> dict[str, Any]:
        return {
            # The script ships in the image (images/iperf2/dokimi-iperf2-server.sh).
            "command": ["dokimi-iperf2-server"],
            "args": [
                "--protocol",
                protocol.value,
                "--port",
                str(IPERF_PORT),
                "--source-port",
                str(MEASUREMENT_SOURCE_PORT),
                "--warmup",
                _plain(warmup.seconds),
                "--results",
                RESULTS_DIRECTORY,
            ],
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
                "--source-port",
                str(MEASUREMENT_SOURCE_PORT),
                "--warmup-source-port",
                str(WARMUP_SOURCE_PORT),
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
            # Ready means "listening for start()", which is exactly what the gate asks, and
            # from then on "started": a client that is measuring is not an unwell one.
            "readinessProbe": _listening(TRIGGER_PORT, or_exists=_STARTED_MARKER),
            # The completion marker, readable from the pod's status without exec.
            "terminationMessagePath": "/dev/termination-log",
            "terminationMessagePolicy": "File",
        }

    def _pod(
        self,
        placement: Placement,
        role: Role,
        image: str,
        protocol: Protocol,
        container: dict[str, Any],
    ) -> Manifest:
        node = placement.node_for(role)
        return {
            "apiVersion": POD.api_version,
            "kind": POD.kind,
            "metadata": {
                "name": _pod_name(placement, role),
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
                        "securityContext": _container_security(),
                        "volumeMounts": [
                            {"name": "results", "mountPath": RESULTS_DIRECTORY},
                            {"name": "tmp", "mountPath": "/tmp"},
                        ],
                    },
                    self._exporter_container(placement, image, protocol),
                ],
                "volumes": [
                    # Shared with the exporter, which parses from here.
                    {"name": "results", "emptyDir": {}},
                    {"name": "tmp", "emptyDir": {}},
                ],
            },
        }

    def _exporter_container(
        self, placement: Placement, image: str, protocol: Protocol
    ) -> dict[str, Any]:
        """The exporter, next to the tool in every pod ([ADR-02-02]).

        A regular container, not a native sidecar: Kubernetes stops a native sidecar once
        the pod's other containers have exited, and the exporter has to keep serving the
        completed result after iperf2 has ([ADR-03-01]). So a finished client's pod stays
        ``Running`` until the run removes it. Completion is read from the ``iperf2``
        container's own status, which this does not change.

        Every required label but the two addresses is known now, and given in its
        environment; the exporter refuses to start without them ([ADR-02-07]).
        """
        spec = self.exporter()
        labels = {
            "DOKIMI_CLUSTER": placement.cluster,
            "DOKIMI_RUN_ID": placement.run_id,
            "DOKIMI_TEST_ID": placement.test_id,
            "DOKIMI_REPETITION": str(REPETITION),
            "DOKIMI_TEST_TYPE": IMAGE_NAME,
            "DOKIMI_SOURCE_NODE": placement.source_node,
            "DOKIMI_TARGET_NODE": placement.target_node,
            "DOKIMI_SOURCE_POD": _pod_name(placement, Role.CLIENT),
            "DOKIMI_TARGET_POD": _pod_name(placement, Role.SERVER),
            "DOKIMI_PROTOCOL": protocol.value,
        }
        return {
            "name": spec.container_name,
            "image": image,
            # The exporter ships in the image (images/iperf2/dokimi_iperf2_exporter.py).
            "command": ["dokimi-iperf2-exporter"],
            "args": ["--results", spec.results_directory, "--port", str(spec.port)],
            "env": [
                *({"name": name, "value": value} for name, value in labels.items()),
                # The pod's own address, before any traffic shows the pair's.
                {"name": "DOKIMI_POD_IP", "valueFrom": {"fieldRef": {"fieldPath": "status.podIP"}}},
            ],
            "ports": [{"name": spec.port_name, "containerPort": spec.port, "protocol": "TCP"}],
            "readinessProbe": {
                "httpGet": {"path": spec.path, "port": spec.port_name},
                "periodSeconds": 2,
            },
            "resources": {
                "requests": {"cpu": "10m", "memory": "32Mi"},
                "limits": {"memory": "64Mi"},
            },
            "securityContext": _container_security(),
            # It reads the tool's output and writes nothing.
            "volumeMounts": [
                {"name": "results", "mountPath": spec.results_directory, "readOnly": True}
            ],
        }

    # -- execution --------------------------------------------------------------------------

    def start(self, deployed: DeployedWorkload) -> None:
        # A hard precondition ([ADR-05-07]). The gate establishes it, and this reads the
        # snapshot the gate returned, so the two cannot disagree. It guards against a
        # DeployedWorkload built some other way: start() sends traffic, so it must refuse.
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
        terminated = _client_terminated(deployed)
        if terminated is None:
            return None
        completion = Completion(
            exit_code=int(terminated.get("exitCode", -1)),
            summary=(terminated.get("message") or "").strip(),
            reason=terminated.get("reason") or "",
        )
        server = deployed.pod_ip(Role.SERVER)
        if completion.summary and completion.destination != server:
            raise UnexpectedTargetError(
                f"{deployed.spec.test_id}: the client measured against "
                f"{completion.destination}, not the server pod at {server}. Something "
                f"other than start() reached its trigger port {TRIGGER_PORT} first"
            )
        return completion

    def probe_result(self, deployed: DeployedWorkload) -> ProbeResult | None:
        """How the probe ended, or ``None`` while it is still running.

        Read from the report the client writes to its termination message, not from its
        exit status alone: the report also names the server it was sent to, which is
        checked as :meth:`completion` checks a summary's destination. Every failure is a
        :class:`~dokimi.testtypes.probe.ProbeResult`, never an exception.
        """
        terminated = _client_terminated(deployed)
        if terminated is None:
            return None
        message = terminated.get("message") or ""
        report = dict(line.split("=", 1) for line in message.splitlines() if "=" in line)
        if "connectivity" not in report:
            return ProbeResult(
                ProbeCheck.CONNECTIVITY,
                f"the probe client exited {terminated.get('exitCode', '?')} "
                f"({terminated.get('reason') or 'no reason given'}) without reporting",
            )
        server = deployed.pod_ip(Role.SERVER)
        if report.get("server") != server:
            # What completion() raises as UnexpectedTargetError. Here it is a finding: the
            # preflight cannot vouch for a path it did not probe.
            return ProbeResult(
                ProbeCheck.CONNECTIVITY,
                f"the probe client was sent {report.get('server')!r}, not the server pod at "
                f"{server}: something other than start() reached its trigger port "
                f"{TRIGGER_PORT} first",
            )
        protocol = str(deployed.spec.run_metadata.get("protocol", "")).upper()
        path = f"{protocol} to the server pod at {server}:{IPERF_PORT}"
        if report["connectivity"] != "ok":
            return ProbeResult(ProbeCheck.CONNECTIVITY, f"no {path} arrived")
        if report.get("dns") != "ok":
            return ProbeResult(
                ProbeCheck.DNS,
                f"{PROBE_DNS_NAME} did not resolve inside the client pod, although {path} arrived",
            )
        return ProbeResult(None, f"{path} arrived, and {PROBE_DNS_NAME} resolved")

    def cleanup(self, deployed: DeployedWorkload) -> None:
        """Nothing beyond generic pod deletion: iperf2 leaves no state outside its pods."""


def _container_security() -> dict[str, Any]:
    """Both containers of a pod: nothing to escalate to, nothing to write but volumes."""
    return {
        "allowPrivilegeEscalation": False,
        "readOnlyRootFilesystem": True,
        "capabilities": {"drop": ["ALL"]},
    }


def _pod_name(placement: Placement, role: Role) -> str:
    return f"run-{placement.run_id}-{placement.test_id}-{role.value}"


def _client_terminated(deployed: DeployedWorkload) -> dict[str, Any] | None:
    """The client container's ``terminated`` state, or ``None`` while it runs."""
    name = deployed.spec.pod_name(Role.CLIENT)
    try:
        pod = deployed.cluster.get(POD, deployed.namespace, name)
    except NotFoundError:
        raise WorkloadLostError(f"the client pod {name} disappeared before it finished") from None
    for status in (pod.get("status") or {}).get("containerStatuses") or []:
        if status.get("name") == CONTAINER_NAME:
            terminated: dict[str, Any] | None = (status.get("state") or {}).get("terminated")
            return terminated
    return None


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


def _listening(port: int, *, or_exists: str | None = None) -> dict[str, Any]:
    """A readiness probe: something listens on *port*, or the file *or_exists* exists.

    ``netstat`` rather than a ``tcpSocket`` probe, because a probe that connected to the
    client's trigger port would *be* a trigger. UDP has no connect-probe at all.
    """
    check = f"netstat -ln | grep -q ':{port} '"
    if or_exists is not None:
        check += f" || test -e {or_exists}"
    return {
        "exec": {"command": ["sh", "-c", check]},
        "periodSeconds": 2,
        "failureThreshold": 1,
    }


def _plain(value: Decimal) -> str:
    """A decimal without an exponent or trailing zeros: what iperf2's arguments accept."""
    normalised = value.normalize()
    if normalised == normalised.to_integral_value():
        normalised = normalised.quantize(Decimal(1))
    return format(normalised, "f")
