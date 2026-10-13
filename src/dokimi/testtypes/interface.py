"""The test-type interface: what every test type declares, and the values it trades in.

[ADR-02-01] makes a test type a plugin behind a stable interface, designed up front and
validated on paper against IRTT in ``designs/test-type-interface.md`` of the ADR
repository. :class:`TestTypePlugin` is that interface in code. It is named ``Plugin``
because :class:`dokimi.model.TestType` is already the enum that *names* a test type in a
definition; the two are deliberately different things — one is data a user writes, the
other is the code that data selects.

Every method exists from Milestone-01. What ADR-02-01 defers is one *use* of the
interface, not a part of it: the orchestrator cross-checking requested criteria against
:meth:`TestTypePlugin.capabilities` before deploying anything is Milestone-02 work. Until
then a mismatched criterion runs, produces no matching metric and surfaces as an
``error``, never a ``pass`` ([ADR-05-05]).

The values that cross the interface are plain, frozen data. Pod specs are manifest
dictionaries in the API's own camelCase shape, the same shape
:mod:`dokimi.k8s.resources` passes around, so a workload can be rendered, inspected in a
test and created without a conversion layer in between.
"""

from __future__ import annotations

import enum
import ipaddress
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Protocol, TypeVar

from dokimi.images import ImageSource
from dokimi.model import ConfigError, Metric

if TYPE_CHECKING:
    from dokimi.k8s.resources import Manifest, ResourceClient

__all__ = [
    "ADDRESS_FAMILY",
    "EXPORTER_PORT_NAME",
    "Capability",
    "ClusterFacts",
    "DeployedWorkload",
    "ExporterSpec",
    "ImagePair",
    "ImageSource",
    "NoIPv4AddressError",
    "Parameter",
    "ParameterSchema",
    "Placement",
    "Role",
    "TestParameters",
    "TestTypePlugin",
    "WorkloadImage",
    "WorkloadSpec",
]

T = TypeVar("T")

#: The address family test traffic uses. [ADR-05-03]: IPv4 in Milestone-01 when both
#: families are available, and the family used is recorded with the result.
ADDRESS_FAMILY = "IPv4"

#: The container port name that marks an exporter. The managed Prometheus scrapes exactly
#: the ports of a run's pods that carry this name ([ADR-03-02]), so every exporter names
#: its port this way, whatever number it serves on.
EXPORTER_PORT_NAME = "metrics"


class NoIPv4AddressError(RuntimeError):
    """A pod that passed the gate has no IPv4 address: an IPv6-only pod network.

    Milestone-01 measures over IPv4 only ([ADR-05-03]). Sending an IPv6 address on
    instead would fail later, inside the pod, with a less useful message.
    """


class Capability(enum.Enum):
    """A metric a test type can produce.

    A small enum that grows as test types are added — IRTT brings ``RTT`` and
    ``ONE_WAY_DELAY`` in Milestone-03. Growing it is data, not a signature change. The
    values spell the criterion metric each capability backs ([ADR-02-05]'s logical
    names), so the Milestone-02 pre-deployment check is a lookup, not a translation
    table.
    """

    THROUGHPUT = Metric.THROUGHPUT.value
    PACKET_LOSS = Metric.PACKET_LOSS.value
    JITTER = Metric.JITTER.value

    @property
    def metric(self) -> Metric:
        """The criterion metric this capability lets a test assert over."""
        return Metric(self.value)


@dataclass(frozen=True, slots=True)
class ClusterFacts:
    """What discovery established about the cluster, for :meth:`TestTypePlugin.capabilities`.

    Empty in Milestone-01, deliberately. The first fact it carries is clock
    synchronization, which IRTT's one-way delay depends on (Milestone-03). ``None`` for
    the whole argument means "assume nothing about the cluster", and a test type that
    needs no facts — ``iperf2`` — ignores it, which is why having it costs nothing now.
    """


class Role(enum.Enum):
    """The two pods of a pair. Traffic flows from the client to the server."""

    CLIENT = "client"
    SERVER = "server"


@dataclass(frozen=True, slots=True)
class WorkloadImage:
    """A resolved image reference a run deploys, and where it came from.

    *source* is :class:`dokimi.images.ImageSource`, the vocabulary the orchestrator's
    own image already records, so every image in the run metadata says the same thing
    the same way: ``run-override``, ``installation-override`` or ``release-default``.
    """

    reference: str
    source: ImageSource

    @property
    def digest(self) -> str | None:
        """The ``sha256:...`` digest this reference pins, or ``None`` if it pins a tag."""
        _, separator, digest = self.reference.partition("@")
        if not separator or not digest.startswith("sha256:") or len(digest) != 71:
            return None
        return digest


@dataclass(frozen=True, slots=True)
class ImagePair:
    """The images for the client and server roles.

    Often the same image: [ADR-05-04] builds one image per test type, and both roles of
    ``iperf2`` run the same binary. A pair still, because the interface must not assume
    that of every tool.
    """

    client: WorkloadImage
    server: WorkloadImage

    def for_role(self, role: Role) -> WorkloadImage:
        return self.client if role is Role.CLIENT else self.server


@dataclass(frozen=True, slots=True)
class Parameter:
    """One accepted parameter: its name, how its text is validated, and its default.

    *parse* turns the written value into a typed one, raising
    :class:`~dokimi.model.ConfigError` on anything malformed — the same error every
    other phase-2 validation raises, so a bad parameter is a ``config_error`` detected
    before anything is deployed ([ADR-04-03], [ADR-04-05]).
    """

    name: str
    parse: Callable[[str], object]
    description: str
    default: str | None = None
    choices: tuple[str, ...] = ()

    @property
    def required(self) -> bool:
        return self.default is None


@dataclass(frozen=True, slots=True)
class TestParameters:
    """A validated parameter set: typed values, plus the text each was written as."""

    values: Mapping[str, object]
    #: The canonical text of every parameter, defaults included, for run metadata.
    written: Mapping[str, str]

    def get(self, name: str, kind: type[T]) -> T:
        """The value of *name*, which must be of *kind*.

        A mismatch is a bug in the test type — its schema produced one type and its code
        expected another — not a user error, so it is not a ``ConfigError``.
        """
        value = self.values[name]
        if not isinstance(value, kind):
            raise TypeError(f"parameter {name!r} is {type(value).__name__}, not {kind.__name__}")
        return value


@dataclass(frozen=True, slots=True)
class ParameterSchema:
    """Every parameter a test type accepts. Unknown names are refused, not ignored.

    Refusing rather than ignoring is [ADR-04-03]'s reasoning, and it matters more for a
    measurement tool than for most: a silently ignored typo is a test that ran at the
    wrong rate and reported success.
    """

    test_type: str
    parameters: tuple[Parameter, ...]

    def __post_init__(self) -> None:
        names = [parameter.name for parameter in self.parameters]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ValueError(f"{self.test_type} declares {duplicates} more than once")

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(parameter.name for parameter in self.parameters)

    def validate(self, raw: Mapping[str, str]) -> TestParameters:
        """Validate *raw* against the schema, filling in defaults."""
        unknown = sorted(set(raw) - set(self.names))
        if unknown:
            raise ConfigError(
                f"{self.test_type} does not accept the parameter(s) {', '.join(unknown)}. "
                f"Accepted parameters are: {', '.join(self.names)}"
            )
        values: dict[str, object] = {}
        written: dict[str, str] = {}
        for parameter in self.parameters:
            text = raw.get(parameter.name, parameter.default)
            if text is None:
                raise ConfigError(f"{self.test_type} requires the parameter {parameter.name!r}")
            if parameter.choices and text not in parameter.choices:
                raise ConfigError(
                    f"{self.test_type} parameter {parameter.name!r} is {text!r}. Accepted "
                    "values are: " + ", ".join(repr(choice) for choice in parameter.choices)
                )
            value = parameter.parse(text)
            values[parameter.name] = value
            written[parameter.name] = value.value if isinstance(value, enum.Enum) else str(value)
        return TestParameters(MappingProxyType(values), MappingProxyType(written))


@dataclass(frozen=True, slots=True)
class ExporterSpec:
    """Where a test type's sidecar exporter serves, and what it reads ([ADR-02-02]).

    The orchestrator reads ``http://<pod-ip>:<port><path>`` directly for the verdict and
    Prometheus scrapes the same endpoint for history ([ADR-03-01]). *results_directory*
    is the volume the tool writes its native output to and the exporter parses — shared
    between the two containers of a pod, which is what lets the exporter keep serving a
    completed result after the tool has exited.
    """

    container_name: str
    port: int
    path: str
    results_directory: str
    #: The container port's name. Prometheus finds an exporter by it, not by its number.
    port_name: str = EXPORTER_PORT_NAME


@dataclass(frozen=True, slots=True)
class WorkloadSpec:
    """The pods one pair needs, built from validated parameters, ready to be created.

    The manifests are unbounded and unowned: deployment adds the deadline
    (:func:`~dokimi.k8s.deadlines.bound_pod`) and the ownership labels and reference
    (:class:`~dokimi.k8s.ownership.RunOwnership`), so a test type cannot forget either.
    """

    namespace: str
    test_id: str
    #: Created in this order. A server first, so it is listening by the time a client
    #: could possibly be started.
    pods: Mapping[Role, Manifest]
    #: What a result from this workload cannot be interpreted without: images and their
    #: digests, the warm-up pass, the connectivity mode ([ADR-05-03], [ADR-04-04]).
    run_metadata: Mapping[str, str] = field(default_factory=dict)
    #: The discarded warm-up pass the workload runs before measuring, ``0`` if none
    #: ([ADR-04-04]). Typed, because the pods' deadline is budgeted from it.
    warmup_seconds: float = 0.0

    def pod_name(self, role: Role) -> str:
        name: str = self.pods[role]["metadata"]["name"]
        return name


@dataclass(frozen=True, slots=True)
class DeployedWorkload:
    """A workload whose pods have both passed the readiness gate.

    Only :func:`~dokimi.testtypes.lifecycle.await_ready` builds one, from the pods as
    the API server reported them at the moment both were ``Ready``. Holding one is the
    evidence :meth:`TestTypePlugin.start` requires.
    """

    spec: WorkloadSpec
    #: The pods as read when the gate passed, by role.
    pods: Mapping[Role, Manifest]
    #: The cluster they run in, for :meth:`TestTypePlugin.is_complete` to re-read them.
    cluster: ResourceClient

    @property
    def namespace(self) -> str:
        return self.spec.namespace

    def pod_ip(self, role: Role) -> str:
        """The pod's IPv4 address, which [ADR-05-03]'s direct pod addressing sends to.

        From ``status.podIPs``, not ``status.podIP``: on a dual-stack cluster whose primary
        family is IPv6, ``podIP`` is the IPv6 address, and ADR-05-03 chooses IPv4 whenever
        both families are available. ``podIP`` is read only when ``podIPs`` is absent.
        """
        status = self.pods[role].get("status") or {}
        candidates = [entry.get("ip") for entry in status.get("podIPs") or []] or [
            status.get("podIP")
        ]
        addresses = [address for address in candidates if address]
        if not addresses:
            raise RuntimeError(f"the {role.value} pod has no IP although it passed the gate")
        for address in addresses:
            if isinstance(ipaddress.ip_address(address), ipaddress.IPv4Address):
                return str(address)
        raise NoIPv4AddressError(
            f"the {role.value} pod {self.spec.pod_name(role)} has no IPv4 address, only "
            f"{', '.join(addresses)}. Milestone-01 measures over IPv4 (ADR-05-03), so it "
            "needs an IPv4 or dual-stack pod network"
        )


@dataclass(frozen=True, slots=True)
class Placement:
    """Where one pair's pods go, and under which identity."""

    namespace: str
    run_id: str
    test_id: str
    source_node: str
    target_node: str

    def node_for(self, role: Role) -> str:
        """The client sends, so it runs on the source; the server on the target."""
        return self.source_node if role is Role.CLIENT else self.target_node


class TestTypePlugin(Protocol):
    """One test type ([ADR-02-01], ``designs/test-type-interface.md``)."""

    #: The name a definition selects it by, :class:`dokimi.model.TestType`'s value.
    name: str

    def images(self) -> ImagePair:
        """Container images required for the client and server roles ([ADR-05-04])."""
        ...

    def parameter_schema(self) -> ParameterSchema:
        """Accepted parameters and their validation rules."""
        ...

    def workload(self, params: TestParameters, placement: Placement) -> WorkloadSpec:
        """Pod specs for client and server, built from validated parameters.

        *placement* is where they go: the pair's nodes, namespace and identity. The paper
        design passes only *params*; a pod spec cannot name its node without the rest,
        and the plugin is the one place that knows which role goes where.
        """
        ...

    def capabilities(
        self, params: TestParameters, environment: ClusterFacts | None = None
    ) -> frozenset[Capability]:
        """The metrics this test type can produce for *params*, in *environment*."""
        ...

    def start(self, deployed: DeployedWorkload) -> None:
        """Trigger test execution. Both pods being ``Ready`` is a hard precondition."""
        ...

    def is_complete(self, deployed: DeployedWorkload) -> bool:
        """Whether the test has finished: process exit, a marker, or elapsed duration."""
        ...

    def exporter(self) -> ExporterSpec:
        """The sidecar exporter that publishes this test type's metrics ([ADR-02-02])."""
        ...

    def cleanup(self, deployed: DeployedWorkload) -> None:
        """Test-type-specific teardown beyond generic pod deletion, if any."""
        ...
