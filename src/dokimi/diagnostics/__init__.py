"""Post-run diagnostic snapshot: phase 10 (ADR-05-07, M1-15).

Captured once per run, after the last test and **before** cleanup, while everything the
run created still exists. Outside Robot's reports by construction, since the Robot process
has already exited (ADR-01-08), so it has a reporting surface of its own:
``diagnostics.json`` in the run's artifact directory, next to ``log.html``, which reaches
the submitter with the other artifacts (ADR-05-15), and a summary in the Job's log.

**What it records:**

* every run-owned pod: the node it ran on, its phase, readiness and why it ended, and per
  container its state, reason, exit code, restarts and how it last ended. So "which node
  was pod X on" is answerable after the fact, on every run, passing or not;
* every planned pair, consolidated across the matrix: its nodes, its pods, whether it is
  down, and a reference to the findings its test produced (its Robot output), so the
  report is one place to look without copying what has a source of truth elsewhere;
* whether the run namespace and its Prometheus were still reachable.

**What counts as an anomaly** (ADR-05-07's follow-up, decided 2026-10-13, scoped on
2026-10-15): something no single test's error category was in flight to catch.

* a pod of a pair whose pods were deployed, and that no longer exists;
* a test pod or the Prometheus pod evicted, or stopped by its deadline;
* a container of a test pod or of the Prometheus pod that restarted, is crash-looping or
  was OOM-killed;
* the node of a test pod or of the Prometheus pod that is gone or not ``Ready``;
* the namespace or the Prometheus unreachable;
* the snapshot itself incomplete, by error or by its own timeout.

Everything else is informational. A tool container that exited non-zero is the test's own
result, which the test reports; its pair is still recorded as down. A finished client
whose pod stays ``Running`` because its exporter keeps serving is how it should be. The
node-exporter's pods run on every node, also those no test used, so their state is
recorded but never an anomaly.

**An anomaly means the run's resources are retained** (ADR-05-07): the report says
``"retain": true`` and why. Until M1-16 builds retain-on-failure, the test pods are still
removed before the claim is released, as the 2026-10-07 sync decided, so that nothing of a
run keeps sending; their state is in this snapshot. Nothing else of a run is deleted
before M1-16, except the node-exporter it deployed (2026-10-07), and M1-16 honours
``retain``.

**It never blocks cleanup.** It has its own timeout, :data:`SNAPSHOT_TIMEOUT_SECONDS`, well
inside the Job's ``diagnostics`` budget term, and it never raises: each step that fails is
recorded as ``snapshot_incomplete`` and the others still run. What a step still running at
the timeout finds afterwards never reaches the report that was returned.
"""

from __future__ import annotations

import copy
import json
import threading
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from dokimi.execution.artifacts import RunArtifacts
from dokimi.k8s.identity import COMPONENT_LABEL
from dokimi.k8s.ownership import (
    PROMETHEUS_COMPONENT,
    TEST_ID_LABEL,
    format_timestamp,
    run_selector,
)
from dokimi.k8s.resources import POD, Manifest, ResourceClient
from dokimi.planning import Plan, PlannedPair
from dokimi.run_identity import RunId
from dokimi.testtypes.interface import Role

__all__ = [
    "SCHEMA_VERSION",
    "SNAPSHOT_TIMEOUT_SECONDS",
    "Anomaly",
    "AnomalyKind",
    "ContainerSnapshot",
    "Diagnostics",
    "PairSnapshot",
    "PodSnapshot",
    "capture",
    "write_report",
]

#: How long the snapshot may take, all of it. Well inside the Job's ``diagnostics``
#: budget term, so it can never be what runs the Job out of time.
SNAPSHOT_TIMEOUT_SECONDS = 30.0
#: ``diagnostics.json``'s schema. Raised when a field changes meaning or goes away.
SCHEMA_VERSION = 1
REPORT_NAME = "diagnostics.json"

#: The pods whose health is the run's: a pair's two, and the run's Prometheus. Every
#: other run-owned pod, the node-exporter's, is recorded and informational.
_CHECKED_COMPONENTS = frozenset({*(role.value for role in Role), PROMETHEUS_COMPONENT})


class AnomalyKind(StrEnum):
    """What went wrong, by a name that stays stable in ``diagnostics.json``."""

    POD_MISSING = "pod_missing"
    POD_EVICTED = "pod_evicted"
    POD_DEADLINE_EXCEEDED = "pod_deadline_exceeded"
    CONTAINER_RESTARTED = "container_restarted"
    CONTAINER_CRASH_LOOPING = "container_crash_looping"
    CONTAINER_OOM_KILLED = "container_oom_killed"
    NODE_NOT_READY = "node_not_ready"
    NAMESPACE_UNREACHABLE = "namespace_unreachable"
    PROMETHEUS_UNREACHABLE = "prometheus_unreachable"
    SNAPSHOT_INCOMPLETE = "snapshot_incomplete"


#: A pod ended by the cluster, not by its own containers.
_ABNORMAL_POD_REASONS = {
    "Evicted": AnomalyKind.POD_EVICTED,
    "DeadlineExceeded": AnomalyKind.POD_DEADLINE_EXCEEDED,
}


@dataclass(frozen=True, slots=True)
class Anomaly:
    kind: AnomalyKind
    #: What it is about: a pod, a node, a pair, or the run.
    subject: str
    detail: str


@dataclass(frozen=True, slots=True)
class ContainerSnapshot:
    name: str
    #: ``running``, ``waiting``, ``terminated`` or ``unknown``.
    state: str
    reason: str
    exit_code: int | None
    restarts: int
    #: How it ended the last time, before a restart: ``terminated`` or ``""``.
    last_state: str = ""
    last_reason: str = ""
    last_exit_code: int | None = None


@dataclass(frozen=True, slots=True)
class PodSnapshot:
    name: str
    component: str
    test_id: str
    node: str
    phase: str
    ready: bool
    ip: str
    containers: tuple[ContainerSnapshot, ...]
    #: Why the cluster ended it, such as ``Evicted``, and what it said: ``""`` if it didn't.
    reason: str = ""
    message: str = ""

    @property
    def checked(self) -> bool:
        """Whether its health is the run's (a test pod or the Prometheus pod)."""
        return self.component in _CHECKED_COMPONENTS


@dataclass(frozen=True, slots=True)
class PairSnapshot:
    test_id: str
    source_node: str
    target_node: str
    #: Whether its pods were deployed: only then are they expected to exist.
    started: bool
    #: Each role's pod name, ``None`` where it does not exist.
    pods: Mapping[str, str | None]
    #: The test's own findings, relative to the artifact directory: its Robot output.
    findings: str | None
    down: bool


@dataclass(slots=True)
class Diagnostics:
    """The snapshot, as :func:`capture` returns it."""

    run_id: str
    namespace: str
    captured_at: str
    #: What the operator called the cluster at submission, as every result is labelled.
    cluster: str = ""
    #: Whether the run deployed a Prometheus of its own, which is then checked.
    prometheus_deployed: bool = False
    namespace_reachable: bool | None = None
    #: ``None`` where it was not checked: none deployed, or the snapshot stopped first.
    prometheus_reachable: bool | None = None
    pods: list[PodSnapshot] = field(default_factory=list)
    pairs: list[PairSnapshot] = field(default_factory=list)
    anomalies: list[Anomaly] = field(default_factory=list)
    complete: bool = False

    @property
    def retain(self) -> bool:
        """Whether the run's resources are retained: any anomaly at all (ADR-05-07)."""
        return bool(self.anomalies)

    def to_json(self) -> str:
        document: dict[str, Any] = {
            "schema": SCHEMA_VERSION,
            "run_id": self.run_id,
            "cluster": self.cluster,
            "namespace": self.namespace,
            "captured_at": self.captured_at,
            "complete": self.complete,
            "retain": self.retain,
            "anomalies": [asdict(anomaly) for anomaly in self.anomalies],
            "prometheus_deployed": self.prometheus_deployed,
            "reachable": {
                "namespace": self.namespace_reachable,
                "prometheus": self.prometheus_reachable,
            },
            "pairs": [asdict(pair) for pair in self.pairs],
            "pods": [asdict(pod) for pod in self.pods],
        }
        return json.dumps(document, indent=2, default=str) + "\n"

    def summary(self) -> list[str]:
        """The Job log's lines: what was seen, and what is wrong with it.

        One line per test pod and for the Prometheus; the node-exporter's pods, one per
        node, are counted, not listed.
        """
        down = [pair.test_id for pair in self.pairs if pair.down]
        lines = [
            f"phase 10 (diagnostics): {len(self.pods)} pod(s), {len(self.pairs)} pair(s), "
            + (f"down: {', '.join(down)}" if down else "none down")
            + ("" if self.complete else ", snapshot incomplete")
        ]
        lines += [
            f"  {pod.name} on {pod.node or '(no node)'}: {pod.phase}"
            for pod in self.pods
            if pod.checked
        ]
        others: dict[str, int] = {}
        for pod in self.pods:
            if not pod.checked:
                others[pod.component or "other"] = others.get(pod.component or "other", 0) + 1
        lines += [f"  {count} {component} pod(s)" for component, count in sorted(others.items())]
        if self.anomalies:
            lines.append(f"  {len(self.anomalies)} anomaly(ies): the run's resources are retained")
            lines += [
                f"    {anomaly.kind} {anomaly.subject}: {anomaly.detail}"
                for anomaly in self.anomalies
            ]
        return lines


class _Recorder:
    """The worker's report, until the caller stops waiting for it.

    Every step commits what it found under the lock. Once the caller has taken its copy,
    nothing more is committed, so the report it returned, printed and wrote never changes
    behind its back, and a late pod list during cleanup cannot add false findings.
    """

    def __init__(self, diagnostics: Diagnostics) -> None:
        self._diagnostics = diagnostics
        self._lock = threading.Lock()
        self._closed = False

    def commit(self, change: Callable[[Diagnostics], None]) -> None:
        with self._lock:
            if not self._closed:
                change(self._diagnostics)

    def close(self) -> Diagnostics:
        """Stop recording, and return a copy of what was recorded so far."""
        with self._lock:
            self._closed = True
            return copy.deepcopy(self._diagnostics)


def capture(
    resources: ResourceClient,
    *,
    run_id: RunId,
    namespace: str,
    plan: Plan,
    started: Collection[str],
    artifacts: RunArtifacts,
    list_nodes: Callable[[], Sequence[Manifest]],
    prometheus_ready: Callable[[], bool] | None = None,
    cluster: str = "",
    timeout: float = SNAPSHOT_TIMEOUT_SECONDS,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> Diagnostics:
    """Take the snapshot. Never raises, and returns within about *timeout* seconds.

    *started* are the pairs whose pods were deployed: only their pods are expected to
    exist.
    """
    recorder = _Recorder(
        Diagnostics(
            str(run_id),
            namespace,
            format_timestamp(now()),
            cluster=cluster,
            prometheus_deployed=prometheus_ready is not None,
        )
    )
    worker = threading.Thread(
        target=_collect,
        args=(recorder, resources),
        kwargs={
            "run_id": run_id,
            "namespace": namespace,
            "plan": plan,
            "started": started,
            "artifacts": artifacts,
            "list_nodes": list_nodes,
            "prometheus_ready": prometheus_ready,
        },
        name="dokimi-diagnostics",
        daemon=True,
    )
    worker.start()
    worker.join(timeout)
    diagnostics = recorder.close()
    # Decided from the copy, not from worker.is_alive(): a worker that finishes between
    # close() and that check has its last commit dropped, and would leave the report
    # incomplete with no reason given. A failed step has already named itself.
    if not diagnostics.complete and not any(
        anomaly.kind is AnomalyKind.SNAPSHOT_INCOMPLETE for anomaly in diagnostics.anomalies
    ):
        # Left to finish or not on its own: the Job ends anyway, and cleanup does not wait.
        diagnostics.anomalies.append(
            Anomaly(
                AnomalyKind.SNAPSHOT_INCOMPLETE,
                "run",
                f"not finished after {timeout:g}s; recorded what it had",
            )
        )
    return diagnostics


def write_report(diagnostics: Diagnostics, artifacts_directory: Path) -> Path:
    """Write ``diagnostics.json`` into the run's artifact directory, and return its path."""
    artifacts_directory.mkdir(parents=True, exist_ok=True)
    path = artifacts_directory / REPORT_NAME
    path.write_text(diagnostics.to_json(), encoding="utf-8")
    return path


# -- collecting ----------------------------------------------------------------------------


def _collect(
    recorder: _Recorder,
    resources: ResourceClient,
    *,
    run_id: RunId,
    namespace: str,
    plan: Plan,
    started: Collection[str],
    artifacts: RunArtifacts,
    list_nodes: Callable[[], Sequence[Manifest]],
    prometheus_ready: Callable[[], bool] | None,
) -> None:
    """Each step on its own: one that fails is recorded, and the rest still run."""
    failed = False

    def step(name: str, run: Callable[[], None]) -> None:
        nonlocal failed
        try:
            run()
        except Exception as error:  # a diagnostic step must never become the failure
            failed = True
            incomplete = Anomaly(
                AnomalyKind.SNAPSHOT_INCOMPLETE, name, f"{type(error).__name__}: {error}"
            )
            recorder.commit(lambda d: d.anomalies.append(incomplete))

    pods: list[PodSnapshot] = []
    reached = False

    def read_pods() -> None:
        nonlocal reached
        try:
            manifests = resources.list(POD, run_selector(run_id), namespace)
        except Exception as error:
            unreachable = Anomaly(AnomalyKind.NAMESPACE_UNREACHABLE, namespace, str(error))

            def record_unreachable(d: Diagnostics) -> None:
                d.namespace_reachable = False
                d.anomalies.append(unreachable)

            recorder.commit(record_unreachable)
            return
        reached = True
        pods.extend(sorted((_pod(manifest) for manifest in manifests), key=lambda p: p.name))
        found = [anomaly for pod in pods for anomaly in _pod_anomalies(pod)]

        def record_pods(d: Diagnostics) -> None:
            d.namespace_reachable = True
            d.pods.extend(pods)
            d.anomalies.extend(found)

        recorder.commit(record_pods)

    def read_nodes() -> None:
        every, ready = _nodes(list_nodes())
        checked = {pod.node for pod in pods if pod.node and pod.checked}
        found = [
            Anomaly(
                AnomalyKind.NODE_NOT_READY,
                node,
                "not Ready" if node in every else "gone from the cluster",
            )
            for node in sorted(checked - ready)
        ]
        recorder.commit(lambda d: d.anomalies.extend(found))

    def check_prometheus(check: Callable[[], bool]) -> None:
        reachable = _reachable(check)

        def record(d: Diagnostics) -> None:
            d.prometheus_reachable = reachable
            if not reachable:
                d.anomalies.append(
                    Anomaly(AnomalyKind.PROMETHEUS_UNREACHABLE, "prometheus", "not ready")
                )

        recorder.commit(record)

    def consolidate_pairs() -> None:
        missing: list[Anomaly] = []
        snapshots = [
            (pair, _pair(pair, pods, started, artifacts, reached, missing))
            for wave in plan.waves
            for pair in wave
        ]

        def record(d: Diagnostics) -> None:
            d.anomalies.extend(missing)
            # Against every anomaly recorded so far: its pods', its nodes' and its own.
            d.pairs.extend(_with_anomalies(snap, pair, d.anomalies) for pair, snap in snapshots)

        recorder.commit(record)

    step("pods", read_pods)
    step("nodes", read_nodes)
    if prometheus_ready is not None:
        step("prometheus", lambda: check_prometheus(prometheus_ready))
    step("pairs", consolidate_pairs)
    if not failed:
        recorder.commit(lambda d: setattr(d, "complete", True))


def _pod(manifest: Manifest) -> PodSnapshot:
    metadata = manifest.get("metadata") or {}
    labels = metadata.get("labels") or {}
    spec = manifest.get("spec") or {}
    status = manifest.get("status") or {}
    return PodSnapshot(
        name=metadata.get("name", ""),
        component=labels.get(COMPONENT_LABEL, ""),
        test_id=labels.get(TEST_ID_LABEL, ""),
        node=spec.get("nodeName") or "",
        phase=status.get("phase") or "Unknown",
        ready=any(
            condition.get("type") == "Ready" and condition.get("status") == "True"
            for condition in status.get("conditions") or []
        ),
        ip=status.get("podIP") or "",
        containers=tuple(_container(each) for each in status.get("containerStatuses") or []),
        reason=status.get("reason") or "",
        message=status.get("message") or "",
    )


def _container(status: Mapping[str, Any]) -> ContainerSnapshot:
    name = status.get("name", "")
    restarts = int(status.get("restartCount") or 0)
    last = (status.get("lastState") or {}).get("terminated") or {}
    last_fields: dict[str, Any] = (
        {
            "last_state": "terminated",
            "last_reason": last.get("reason") or "",
            "last_exit_code": _exit_code(last),
        }
        if last
        else {}
    )
    state = status.get("state") or {}
    for current in ("running", "waiting", "terminated"):
        if current in state and state[current] is not None:
            detail = state[current] or {}
            return ContainerSnapshot(
                name=name,
                state=current,
                reason=detail.get("reason") or "",
                exit_code=_exit_code(detail),
                restarts=restarts,
                **last_fields,
            )
    return ContainerSnapshot(name, "unknown", "", None, restarts, **last_fields)


def _exit_code(detail: Mapping[str, Any]) -> int | None:
    code = detail.get("exitCode")
    return int(code) if code is not None else None


def _pod_anomalies(pod: PodSnapshot) -> list[Anomaly]:
    if not pod.checked:
        return []
    found: list[Anomaly] = []
    if pod.reason in _ABNORMAL_POD_REASONS:
        found.append(
            Anomaly(_ABNORMAL_POD_REASONS[pod.reason], pod.name, pod.message or pod.reason)
        )
    for container in pod.containers:
        subject = f"{pod.name}/{container.name}"
        if container.restarts > 0:
            found.append(
                Anomaly(
                    AnomalyKind.CONTAINER_RESTARTED, subject, f"restarted {container.restarts}x"
                )
            )
        if container.reason == "CrashLoopBackOff":
            found.append(Anomaly(AnomalyKind.CONTAINER_CRASH_LOOPING, subject, container.reason))
        if "OOMKilled" in (container.reason, container.last_reason):
            found.append(Anomaly(AnomalyKind.CONTAINER_OOM_KILLED, subject, "OOMKilled"))
    return found


def _nodes(manifests: Sequence[Manifest]) -> tuple[set[str], set[str]]:
    """Every node's name, and the Ready ones', to tell "gone" from "not Ready"."""
    every: set[str] = set()
    ready: set[str] = set()
    for manifest in manifests:
        name = (manifest.get("metadata") or {}).get("name", "")
        every.add(name)
        if any(
            condition.get("type") == "Ready" and condition.get("status") == "True"
            for condition in (manifest.get("status") or {}).get("conditions") or []
        ):
            ready.add(name)
    return every, ready


def _reachable(check: Callable[[], bool]) -> bool:
    try:
        return bool(check())
    except Exception:
        return False


def _pair(
    pair: PlannedPair,
    pods: Sequence[PodSnapshot],
    started: Collection[str],
    artifacts: RunArtifacts,
    reached: bool,
    missing: list[Anomaly],
) -> PairSnapshot:
    """The pair as the pods show it; :func:`_with_anomalies` adds what else makes it down."""
    test_id = pair.test_id
    own = [pod for pod in pods if pod.test_id == test_id]
    by_role = {
        role.value: next((pod.name for pod in own if pod.component == role.value), None)
        for role in Role
    }
    is_started = test_id in started
    if is_started and reached:
        missing.extend(
            Anomaly(
                AnomalyKind.POD_MISSING,
                f"{test_id}/{role}",
                "its pods were deployed, and this one no longer exists",
            )
            for role, name in by_role.items()
            if name is None
        )
    # A container that exited non-zero, such as a client that couldn't reach its server,
    # is the test's own finding, not an anomaly; the pair is still down.
    exited = any(
        container.exit_code not in (None, 0) for pod in own for container in pod.containers
    )
    output = artifacts.pair_output(test_id)
    return PairSnapshot(
        test_id=test_id,
        source_node=pair.source.name,
        target_node=pair.target.name,
        started=is_started,
        pods=by_role,
        findings=output.relative_to(artifacts.directory).as_posix() if output.is_file() else None,
        down=is_started and (not reached or None in by_role.values() or exited),
    )


def _with_anomalies(
    snapshot: PairSnapshot, pair: PlannedPair, anomalies: Sequence[Anomaly]
) -> PairSnapshot:
    if snapshot.down or not snapshot.started:
        return snapshot
    own = {name for name in snapshot.pods.values() if name}
    nodes = {pair.source.name, pair.target.name}
    affected = any(
        anomaly.subject.split("/", 1)[0] in own or anomaly.subject in nodes for anomaly in anomalies
    )
    return replace(snapshot, down=True) if affected else snapshot
