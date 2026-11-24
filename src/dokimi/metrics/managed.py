"""The managed Prometheus: deployed by the run, scraping the run's exporters, kept after it.

[ADR-03-02] ``managed`` mode, which is what Milestone-01 runs. What that means here:

* **One Prometheus per run**, in the run's namespace: a ``ConfigMap`` holding its
  configuration, a ``Deployment`` and a ``Service``, all labelled as owned by the run
  (``app.kubernetes.io/component: prometheus``).
* **Kept by default.** ADR-03-02 removes a managed Prometheus only on an explicit flag,
  so that "the run's history outlives cleanup" holds in every mode. It therefore carries
  no ownerReference — deleting the run's owner must not cascade to it — and
  :func:`~dokimi.k8s.ownership.delete_run_owned` keeps it unless ``remove_kept`` is set.
  It never blocks a later run: exclusivity reads run owners only ([ADR-05-13]). What it
  keeps is bounded: :data:`RETENTION_SIZE` on disk, in a volume of :data:`VOLUME_SIZE`.
* **Off the test's nodes where it can be.** A Prometheus scraping and compacting on the
  source or target node competes with the measurement for CPU and network, so it
  prefers any other node. Preferred, not required: a two-node cluster still runs.
* **It scrapes this run's exporters only**, discovered as pods carrying this run's
  ``run-id`` label with a container port named :data:`EXPORTER_PORT_NAME`. A kept
  Prometheus from an earlier run therefore never records a later run's results.
* **The scrape interval is ours**, :data:`SCRAPE_INTERVAL_SECONDS`, so it is known and
  recorded in the run metadata ([ADR-03-01]).
* **An already-present node-exporter or kube-state-metrics is reused**, never duplicated.
* **A node-exporter is deployed when none is present**, as ADR-03-02 asks: a DaemonSet
  owned by the run, so cleanup removes it with the run. kube-state-metrics is not: it
  describes the cluster's objects, not the nodes a test measures on, and its absence is
  recorded instead. See :func:`provide_node_exporter` for what the DaemonSet may do.

Prometheus's own RBAC — reading pods, services and endpoint slices for discovery — is an
installation step in ``deploy/rbac.yaml``, not something a run creates: the Job would
otherwise need to grant permissions it does not itself hold.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import yaml

from dokimi.k8s.identity import MANAGED_BY, MANAGED_BY_LABEL, RUN_ID_LABEL
from dokimi.k8s.manifests import resource_name
from dokimi.k8s.ownership import COMPONENT_LABEL, PROMETHEUS_COMPONENT, RunOwnership
from dokimi.k8s.resources import (
    CONFIG_MAP,
    DAEMON_SET,
    DEPLOYMENT,
    SERVICE,
    Kind,
    Manifest,
    NotFoundError,
    ResourceClient,
)
from dokimi.model import ConfigError, MonitoringMode
from dokimi.testtypes.interface import EXPORTER_PORT_NAME

__all__ = [
    "EXPORTER_JOB",
    "EXPORTER_PORT_NAME",
    "NODE_EXPORTER_COLLECTORS",
    "NODE_EXPORTER_COMPONENT",
    "NODE_EXPORTER_IMAGE",
    "NODE_EXPORTER_PORT",
    "NODE_EXPORTER_PORT_NAME",
    "PROMETHEUS_IMAGE",
    "PROMETHEUS_PORT",
    "PROMETHEUS_SERVICE_ACCOUNT",
    "RETENTION_SIZE",
    "SCRAPE_INTERVAL_SECONDS",
    "VOLUME_SIZE",
    "ManagedPrometheus",
    "MonitoringSelection",
    "NodeExporter",
    "SupportingExporter",
    "detect_supporting_exporters",
    "find_deployed_node_exporter",
    "managed_manifests",
    "node_exporter_manifest",
    "node_exporter_status",
    "open_command",
    "provide_node_exporter",
    "provision_managed_prometheus",
    "removal_command",
    "remove_node_exporter",
    "render_config",
    "select_monitoring",
]

#: Upstream Prometheus, pinned by digest ([ADR-05-04]'s pinning rule). Not an image this
#: project builds: it is infrastructure, not a test workload, and upstream publishes it.
#: Upstream does not sign it, so the digest is what makes it the image we reviewed. The
#: digest is the multi-architecture index, identical on quay.io and Docker Hub.
PROMETHEUS_IMAGE = (
    "quay.io/prometheus/prometheus:v3.15.0"
    "@sha256:efd719c99d83b060d9daefdcf00360461adf279f45ef5391f8d111892118753e"
)
PROMETHEUS_PORT = 9090
#: Pre-provisioned by ``deploy/rbac.yaml``, with read access for service discovery.
PROMETHEUS_SERVICE_ACCOUNT = "dokimi-prometheus"
#: Ours to choose in managed mode, and short, because a Milestone-01 test lasts seconds:
#: ADR-03-01's scrape confirmation waits ``3 × interval``, at least 60 s, which this keeps
#: at the minimum.
SCRAPE_INTERVAL_SECONDS = 5
#: The scrape job for the run's exporters.
EXPORTER_JOB = "dokimi-exporters"
#: A kept Prometheus is never removed by a later run, so what each one holds is bounded:
#: Prometheus drops its oldest blocks past this size, and the volume's limit sits above it
#: with room for the write-ahead log. A Milestone-01 run writes a few megabytes.
RETENTION_SIZE = "512MB"
VOLUME_SIZE = "1Gi"

#: Upstream node-exporter, pinned by digest like Prometheus and, like it, unsigned. The
#: digest is the multi-architecture index of v1.12.1.
NODE_EXPORTER_IMAGE = (
    "quay.io/prometheus/node-exporter:v1.12.1"
    "@sha256:1b4e4438faca4dd7e001dd445d161a4a2091b0fededa84093b3a8dfeae1f1be0"
)
#: On the host's network, so the port is taken on every node. Not 9100, which an
#: undetected node-exporter may already hold, and not 9101, the test exporters' port.
#: Prometheus's port allocation list has no free number left in its range; 9137 belongs
#: to an exporter for a home TV tuner, which a cluster node does not run.
NODE_EXPORTER_PORT = 9137
#: Not :data:`EXPORTER_PORT_NAME`: the run's exporter job would then scrape it too.
NODE_EXPORTER_PORT_NAME = "node-metrics"
NODE_EXPORTER_COMPONENT = "node-exporter"
#: What a measurement on a node is explained by: CPU, memory and the network interfaces.
#: All three read the node's own ``/proc`` from a pod on the host's network, so the
#: DaemonSet needs no host path, no host PID namespace and no privilege.
NODE_EXPORTER_COLLECTORS = ("cpu", "meminfo", "netdev")

#: ``app.kubernetes.io/name`` values that identify a supporting exporter, by what they
#: are: the kube-prometheus stack, the prometheus-community Helm charts, and plain installs.
_SUPPORTING_NAMES: dict[str, tuple[str, ...]] = {
    "node-exporter": ("node-exporter", "prometheus-node-exporter"),
    "kube-state-metrics": ("kube-state-metrics",),
}
_UID = 65534  # the upstream images' "nobody"


@dataclass(frozen=True, slots=True)
class MonitoringSelection:
    """Which mode a run actually uses, and why, for the prominent log ADR-03-02 asks for."""

    requested: MonitoringMode
    selected: MonitoringMode
    reason: str

    @property
    def deploys(self) -> bool:
        """Whether the run deploys its own Prometheus, and a node-exporter if none is found."""
        return self.selected is MonitoringMode.MANAGED

    def describe(self) -> str:
        if self.requested is self.selected:
            return f"{self.selected.value} (requested)"
        return f"{self.selected.value} ({self.requested.value} requested; {self.reason})"


def select_monitoring(requested: MonitoringMode) -> MonitoringSelection:
    """Resolve the definition's monitoring mode to the one Milestone-01 runs.

    ``auto`` prefers an existing Prometheus; detecting one is not built yet, so it falls
    back to ``managed`` — exactly its documented fallback, and said so in the log.
    ``existing`` cannot fall back without contradicting the user, so it is refused
    before anything is deployed: a ``config_error``. ``validate`` and ``submit`` call
    this too, so the refusal comes before a run is ever created.
    """
    if requested is MonitoringMode.MANAGED:
        return MonitoringSelection(requested, MonitoringMode.MANAGED, "requested")
    if requested is MonitoringMode.AUTO:
        return MonitoringSelection(
            requested,
            MonitoringMode.MANAGED,
            "auto falls back to managed: detecting an existing Prometheus is not "
            "available in this version",
        )
    raise ConfigError(
        "monitoring mode 'existing' is not available in this version; use 'managed' or "
        "'auto', which deploy a Prometheus for the run"
    )


@dataclass(frozen=True, slots=True)
class SupportingExporter:
    """A node-exporter or kube-state-metrics the cluster already runs."""

    kind: str
    namespace: str
    service: str

    def __str__(self) -> str:
        return f"{self.kind} ({self.namespace}/{self.service})"


@dataclass(frozen=True, slots=True)
class NodeExporter:
    """A node-exporter DaemonSet that dokimi deployed, by this run or by an earlier one."""

    namespace: str
    name: str
    #: The run that deployed it, and whose cleanup removes it.
    run_id: str
    by_this_run: bool

    def __str__(self) -> str:
        where = f"{self.namespace}/{self.name}"
        if self.by_this_run:
            return f"deployed by this run ({where})"
        return f"deployed by run {self.run_id}, reused ({where})"


def detect_supporting_exporters(cluster: ResourceClient) -> list[SupportingExporter]:
    """The supporting exporters already present, at most one of each kind.

    Found by their Service's ``app.kubernetes.io/name`` label. Our own objects are never
    candidates, and the first match in namespace/name order wins, so the choice is
    stable across runs on an unchanged cluster.
    """
    found: list[SupportingExporter] = []
    for kind, names in _SUPPORTING_NAMES.items():
        candidates = [
            service
            for name in names
            for service in cluster.list(SERVICE, f"app.kubernetes.io/name={name}")
            if (service["metadata"].get("labels") or {}).get(MANAGED_BY_LABEL) != MANAGED_BY
        ]
        if not candidates:
            continue
        first = min(candidates, key=lambda s: (s["metadata"]["namespace"], s["metadata"]["name"]))
        found.append(
            SupportingExporter(kind, first["metadata"]["namespace"], first["metadata"]["name"])
        )
    return found


def render_config(
    run_id: str,
    namespace: str,
    supporting: Sequence[SupportingExporter] = (),
    node_exporter: NodeExporter | None = None,
) -> str:
    """``prometheus.yml`` for a run's managed Prometheus."""
    interval = f"{SCRAPE_INTERVAL_SECONDS}s"
    exporters: dict[str, Any] = {
        "job_name": EXPORTER_JOB,
        # The exporter's labels are the metric contract (ADR-02-04, ADR-02-07); a target
        # label must never overwrite one of them.
        "honor_labels": True,
        "kubernetes_sd_configs": [
            {
                "role": "pod",
                "namespaces": {"names": [namespace]},
                "selectors": [
                    {
                        "role": "pod",
                        "label": f"{MANAGED_BY_LABEL}={MANAGED_BY},{RUN_ID_LABEL}={run_id}",
                    }
                ],
            }
        ],
        "relabel_configs": [
            {
                "source_labels": ["__meta_kubernetes_pod_container_port_name"],
                "regex": EXPORTER_PORT_NAME,
                "action": "keep",
            }
        ],
    }
    jobs = [exporters]
    for exporter in supporting:
        jobs.append(
            {
                "job_name": exporter.kind,
                "kubernetes_sd_configs": [
                    {"role": "endpointslice", "namespaces": {"names": [exporter.namespace]}}
                ],
                "relabel_configs": [
                    {
                        "source_labels": ["__meta_kubernetes_service_name"],
                        "regex": exporter.service,
                        "action": "keep",
                    }
                ],
            }
        )
    if node_exporter is not None:
        # The same job name as a reused one, so a query reads the same either way. It has
        # no Service, so its pods are discovered directly, by our labels and its port, and
        # by the run that deployed it: a kept Prometheus never scrapes a later run's.
        jobs.append(
            {
                "job_name": NODE_EXPORTER_COMPONENT,
                "kubernetes_sd_configs": [
                    {
                        "role": "pod",
                        "namespaces": {"names": [node_exporter.namespace]},
                        "selectors": [
                            {
                                "role": "pod",
                                "label": f"{MANAGED_BY_LABEL}={MANAGED_BY},"
                                f"{COMPONENT_LABEL}={NODE_EXPORTER_COMPONENT},"
                                f"{RUN_ID_LABEL}={node_exporter.run_id}",
                            }
                        ],
                    }
                ],
                "relabel_configs": [
                    {
                        "source_labels": ["__meta_kubernetes_pod_container_port_name"],
                        "regex": NODE_EXPORTER_PORT_NAME,
                        "action": "keep",
                    },
                    # On the host's network the pod's address is the node's, which says
                    # less to a reader than the node's name.
                    {
                        "source_labels": ["__meta_kubernetes_pod_node_name"],
                        "target_label": "node",
                    },
                ],
            }
        )
    config = {
        "global": {
            "scrape_interval": interval,
            "scrape_timeout": interval,
            "evaluation_interval": interval,
        },
        "scrape_configs": jobs,
    }
    return yaml.safe_dump(config, sort_keys=False)


def _name(run_id: str) -> str:
    return resource_name(run_id, "-prometheus")


def _away_from(nodes: Sequence[str]) -> dict[str, Any]:
    """A preferred node anti-affinity: anywhere but *nodes*, if the cluster has room.

    One term per node: a node *field* selector takes exactly one value with ``NotIn``, and
    the API server refuses a term that lists two.
    """
    if not nodes:
        return {}
    return {
        "affinity": {
            "nodeAffinity": {
                "preferredDuringSchedulingIgnoredDuringExecution": [
                    {
                        "weight": 100,
                        "preference": {
                            "matchFields": [
                                {"key": "metadata.name", "operator": "NotIn", "values": [node]}
                            ]
                        },
                    }
                    for node in sorted(set(nodes))
                ]
            }
        }
    }


def managed_manifests(
    run_id: str,
    namespace: str,
    supporting: Sequence[SupportingExporter] = (),
    *,
    node_exporter: NodeExporter | None = None,
    avoid_nodes: Sequence[str] = (),
    image: str = PROMETHEUS_IMAGE,
) -> list[tuple[Kind, Manifest]]:
    """The run's Prometheus objects, in creation order: config, Deployment, Service.

    *avoid_nodes* are the nodes the run tests on; the Prometheus prefers any other.
    """
    name = _name(run_id)
    selector = {"app.kubernetes.io/name": "prometheus", RUN_ID_LABEL: run_id}
    config: Manifest = {
        "apiVersion": CONFIG_MAP.api_version,
        "kind": CONFIG_MAP.kind,
        "metadata": {"name": name, "namespace": namespace},
        "data": {"prometheus.yml": render_config(run_id, namespace, supporting, node_exporter)},
    }
    deployment: Manifest = {
        "apiVersion": DEPLOYMENT.api_version,
        "kind": DEPLOYMENT.kind,
        "metadata": {"name": name, "namespace": namespace},
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": selector},
            "template": {
                "metadata": {
                    "labels": {
                        **selector,
                        MANAGED_BY_LABEL: MANAGED_BY,
                        COMPONENT_LABEL: PROMETHEUS_COMPONENT,
                    }
                },
                "spec": {
                    "serviceAccountName": PROMETHEUS_SERVICE_ACCOUNT,
                    **_away_from(avoid_nodes),
                    "securityContext": {
                        "runAsNonRoot": True,
                        "runAsUser": _UID,
                        "runAsGroup": _UID,
                        "fsGroup": _UID,
                        "seccompProfile": {"type": "RuntimeDefault"},
                    },
                    "containers": [
                        {
                            "name": "prometheus",
                            "image": image,
                            "args": [
                                "--config.file=/etc/prometheus/prometheus.yml",
                                "--storage.tsdb.path=/prometheus",
                                f"--storage.tsdb.retention.size={RETENTION_SIZE}",
                                f"--web.listen-address=:{PROMETHEUS_PORT}",
                            ],
                            "ports": [{"name": "web", "containerPort": PROMETHEUS_PORT}],
                            "readinessProbe": {
                                "httpGet": {"path": "/-/ready", "port": "web"},
                                "periodSeconds": 5,
                            },
                            "resources": {
                                "requests": {"cpu": "100m", "memory": "256Mi"},
                                "limits": {"memory": "1Gi"},
                            },
                            "securityContext": {
                                "allowPrivilegeEscalation": False,
                                "readOnlyRootFilesystem": True,
                                "capabilities": {"drop": ["ALL"]},
                            },
                            "volumeMounts": [
                                {"name": "config", "mountPath": "/etc/prometheus"},
                                {"name": "data", "mountPath": "/prometheus"},
                            ],
                        }
                    ],
                    "volumes": [
                        {"name": "config", "configMap": {"name": name}},
                        {"name": "data", "emptyDir": {"sizeLimit": VOLUME_SIZE}},
                    ],
                },
            },
        },
    }
    service: Manifest = {
        "apiVersion": SERVICE.api_version,
        "kind": SERVICE.kind,
        "metadata": {"name": name, "namespace": namespace},
        "spec": {
            "selector": selector,
            "ports": [{"name": "web", "port": PROMETHEUS_PORT, "targetPort": "web"}],
        },
    }
    return [(CONFIG_MAP, config), (DEPLOYMENT, deployment), (SERVICE, service)]


# -- the node-exporter -------------------------------------------------------------------


def _node_exporter_name(run_id: str) -> str:
    return resource_name(run_id, "-node-exporter")


def node_exporter_manifest(
    run_id: str, namespace: str, *, image: str = NODE_EXPORTER_IMAGE
) -> Manifest:
    """The DaemonSet a run deploys when the cluster has no node-exporter of its own."""
    selector = {"app.kubernetes.io/name": NODE_EXPORTER_COMPONENT, RUN_ID_LABEL: run_id}
    collectors = [f"--collector.{collector}" for collector in NODE_EXPORTER_COLLECTORS]
    return {
        "apiVersion": DAEMON_SET.api_version,
        "kind": DAEMON_SET.kind,
        "metadata": {"name": _node_exporter_name(run_id), "namespace": namespace},
        "spec": {
            "selector": {"matchLabels": selector},
            "template": {
                "metadata": {
                    "labels": {
                        **selector,
                        MANAGED_BY_LABEL: MANAGED_BY,
                        COMPONENT_LABEL: NODE_EXPORTER_COMPONENT,
                    }
                },
                "spec": {
                    # The node's network namespace is what makes netdev count the node's
                    # interfaces rather than the pod's. It is the one host access asked for.
                    "hostNetwork": True,
                    # Reads /proc only; the API is never called.
                    "automountServiceAccountToken": False,
                    # Every node, including tainted ones a test may run on.
                    "tolerations": [{"operator": "Exists"}],
                    "securityContext": {
                        "runAsNonRoot": True,
                        "runAsUser": _UID,
                        "runAsGroup": _UID,
                        "seccompProfile": {"type": "RuntimeDefault"},
                    },
                    "containers": [
                        {
                            "name": NODE_EXPORTER_COMPONENT,
                            "image": image,
                            "args": [
                                "--collector.disable-defaults",
                                *collectors,
                                f"--web.listen-address=:{NODE_EXPORTER_PORT}",
                            ],
                            "ports": [
                                {
                                    "name": NODE_EXPORTER_PORT_NAME,
                                    "containerPort": NODE_EXPORTER_PORT,
                                }
                            ],
                            # It shares the test's nodes, so it is kept small.
                            "resources": {
                                "requests": {"cpu": "10m", "memory": "32Mi"},
                                "limits": {"memory": "64Mi"},
                            },
                            "securityContext": {
                                "allowPrivilegeEscalation": False,
                                "readOnlyRootFilesystem": True,
                                "capabilities": {"drop": ["ALL"]},
                            },
                        }
                    ],
                },
            },
        },
    }


def find_deployed_node_exporter(cluster: ResourceClient, namespace: str) -> NodeExporter | None:
    """A node-exporter an earlier run deployed and did not remove, because it crashed.

    Reused rather than deployed again: on the host's network a second one could never
    bind its port. Its labels say which run deployed it, and it is left as found.
    """
    found = cluster.list(
        DAEMON_SET,
        f"{MANAGED_BY_LABEL}={MANAGED_BY},{COMPONENT_LABEL}={NODE_EXPORTER_COMPONENT}",
        namespace,
    )
    if not found:
        return None
    first = min(found, key=lambda daemon_set: daemon_set["metadata"]["name"])
    metadata = first["metadata"]
    return NodeExporter(namespace, metadata["name"], metadata["labels"][RUN_ID_LABEL], False)


def provide_node_exporter(
    ownership: RunOwnership,
    namespace: str,
    supporting: Sequence[SupportingExporter] = (),
    *,
    image: str = NODE_EXPORTER_IMAGE,
) -> NodeExporter | None:
    """Make sure a node-exporter runs: the cluster's own, an earlier run's, or a new one.

    ``None`` when the cluster already has one of its own (it is then in *supporting*).
    Otherwise one dokimi deployed earlier and left behind is reused, and only if there is
    none does this run deploy one, owned by the run like its pods. A run removes the one
    it deployed when it ends (:func:`remove_node_exporter`), and leaves one it found.

    The DaemonSet asks for the host's network and nothing else. A namespace that enforces
    the ``baseline`` or ``restricted`` Pod Security level refuses that — not the
    DaemonSet itself, which is created, but each of its pods. The run continues without
    node metrics: they explain a result, they never decide one. :func:`node_exporter_status`
    reports it.
    """
    if any(exporter.kind == NODE_EXPORTER_COMPONENT for exporter in supporting):
        return None
    earlier = find_deployed_node_exporter(ownership.cluster, namespace)
    if earlier is not None:
        return earlier
    run_id = str(ownership.run_id)
    manifest = node_exporter_manifest(run_id, namespace, image=image)
    ownership.create(DAEMON_SET, manifest, component=NODE_EXPORTER_COMPONENT)
    return NodeExporter(namespace, _node_exporter_name(run_id), run_id, True)


def remove_node_exporter(cluster: ResourceClient, exporter: NodeExporter) -> bool:
    """Delete the node-exporter this run deployed. False if it was not this run's.

    A run cleans up what it created and nothing else: the cluster's own node-exporter, or
    one a crashed run left, stays. Its uid is read just before the delete and required by
    it, which covers only the moment between the two calls; the name is the run's own, so
    nothing else is expected under it. Its pods go with it, through the garbage collector.

    Raises:
        ClusterError: if the API server refused the deletion.
    """
    if not exporter.by_this_run:
        return False
    try:
        current = cluster.get(DAEMON_SET, exporter.namespace, exporter.name)
        cluster.delete(
            DAEMON_SET, exporter.namespace, exporter.name, uid=current["metadata"]["uid"]
        )
    except NotFoundError:
        return False
    return True


def node_exporter_status(cluster: ResourceClient, exporter: NodeExporter) -> tuple[bool, str]:
    """Whether the node-exporter's pods run on every node it should, and a line saying so.

    Read from the DaemonSet's status, because a pod Pod Security refuses is never created
    and so cannot be found by listing pods.
    """
    status = cluster.get(DAEMON_SET, exporter.namespace, exporter.name).get("status") or {}
    if "desiredNumberScheduled" not in status:
        return True, "pods not reported yet"
    desired = int(status["desiredNumberScheduled"])
    created = int(status.get("currentNumberScheduled", 0))
    ready = int(status.get("numberReady", 0))
    if created < desired:
        return False, (
            f"only {created} of {desired} pods created; the namespace's Pod Security level "
            "may refuse its host network. The run continues without node metrics"
        )
    return True, f"{ready} of {desired} pods ready"


# -- the Prometheus ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ManagedPrometheus:
    """A provisioned managed Prometheus: where to query it, and what it was set up with."""

    run_id: str
    namespace: str
    name: str
    image: str
    supporting: tuple[SupportingExporter, ...]
    selection: MonitoringSelection
    node_exporter: NodeExporter | None = None

    @property
    def endpoint(self) -> str:
        """The in-cluster URL, reachable from the Job through the Service's DNS name."""
        return f"http://{self.name}.{self.namespace}.svc:{PROMETHEUS_PORT}"

    def run_metadata(self) -> dict[str, str]:
        """What a result from this run cannot be interpreted without ([ADR-03-01])."""
        found: dict[str, str] = {exporter.kind: str(exporter) for exporter in self.supporting}
        if self.node_exporter is not None:
            found[NODE_EXPORTER_COMPONENT] = str(self.node_exporter)
        return {
            "monitoring_mode": self.selection.selected.value,
            "monitoring_mode_requested": self.selection.requested.value,
            "monitoring_mode_reason": self.selection.reason,
            "prometheus_endpoint": self.endpoint,
            "prometheus_image": self.image,
            "prometheus_scrape_interval_seconds": str(SCRAPE_INTERVAL_SECONDS),
            **{
                f"supporting_{kind.replace('-', '_')}": found.get(kind, "")
                for kind in _SUPPORTING_NAMES
            },
        }


def provision_managed_prometheus(
    ownership: RunOwnership,
    namespace: str,
    supporting: Sequence[SupportingExporter] = (),
    *,
    selection: MonitoringSelection | None = None,
    node_exporter: NodeExporter | None = None,
    avoid_nodes: Sequence[str] = (),
    image: str = PROMETHEUS_IMAGE,
) -> ManagedPrometheus:
    """Create the run's Prometheus, kept after the run by default."""
    run_id = str(ownership.run_id)
    manifests = managed_manifests(
        run_id,
        namespace,
        supporting,
        node_exporter=node_exporter,
        avoid_nodes=avoid_nodes,
        image=image,
    )
    for kind, manifest in manifests:
        ownership.create_kept(kind, manifest, component=PROMETHEUS_COMPONENT)
    return ManagedPrometheus(
        run_id,
        namespace,
        _name(run_id),
        image,
        tuple(supporting),
        selection or select_monitoring(MonitoringMode.MANAGED),
        node_exporter,
    )


def open_command(prometheus: ManagedPrometheus) -> str:
    """How to reach a kept Prometheus from the submitter's machine: it is in-cluster only.

    ``dokimi prometheus`` forwards a local port to it, with no ``kubectl`` needed.
    """
    return f"dokimi prometheus {prometheus.run_id} --namespace {prometheus.namespace}"


def removal_command(prometheus: ManagedPrometheus) -> str:
    """How to remove a kept Prometheus by hand: by its labels, never by name alone."""
    return (
        f"kubectl delete deployment,service,configmap -n {prometheus.namespace} "
        f"-l {RUN_ID_LABEL}={prometheus.run_id},{COMPONENT_LABEL}={PROMETHEUS_COMPONENT}"
    )
