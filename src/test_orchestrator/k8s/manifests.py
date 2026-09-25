"""The manifests a submission creates: an owner object, the definition, and the Job.

Three objects, in this order, because the order is what makes cleanup survive a crash
(ADR-05-02):

1. A **placeholder owner object** — a ConfigMap acting as a tombstone, since no CRD
   exists yet. It is created first and owns everything else.
2. A **ConfigMap carrying the test definition** exactly as authored, mounted into the
   Job. The document travels with the run rather than being re-read from the submitter's
   disk, so the Job is self-contained and a reattaching process needs nothing local.
3. The **Job**, which is the run.

Deleting the owner cascades the cluster's own garbage collector through the other two,
so a run's resources go away even if nothing the framework wrote ever executes.

``restartPolicy: Never`` and ``backoffLimit: 0`` are not preferences. ADR-01-08 makes the
Job's exit code carry the run's verdict, so a Job controller that retries on non-zero
would re-run the whole matrix on a plain ``fail`` — an uncontrolled retry of everything,
against ADR-05-05's rule that a ``fail`` is never re-rolled.
"""

from __future__ import annotations

import copy
from typing import Any

from test_orchestrator.k8s.identity import MANAGED_BY, MANAGED_BY_LABEL, ownership_labels
from test_orchestrator.model import TestDefinition

__all__ = [
    "DEFINITION_MOUNT_PATH",
    "OWNER_COMPONENT",
    "RUN_COMPONENT",
    "bind_owner",
    "deadline_seconds",
    "definition_file_name",
    "namespace_manifest",
    "render_manifests",
    "resource_name",
]

#: Where the definition ConfigMap is mounted inside the orchestrator container.
DEFINITION_MOUNT_PATH = "/etc/test-orchestrator"

OWNER_COMPONENT = "run-owner"
RUN_COMPONENT = "orchestrator"

#: Margin added to the test duration for everything around it — preflight, workload
#: deployment and readiness, measurement, diagnostics and cleanup. Deliberately generous:
#: this bound exists to stop an orphaned run, not to enforce a schedule. M1-05 (#40) owns
#: the deadline policy and may refine it; ADR-05-02 only requires that one exists.
DEADLINE_MARGIN_SECONDS = 900


def resource_name(run_id: str, suffix: str = "") -> str:
    """A DNS-1123 object name for a run.

    The ``run_id`` keeps its canonical spelling in labels, which permit uppercase, and is
    lowercased here, which object names do not.
    """
    return f"run-{run_id.lower()}{suffix}"


def definition_file_name(source_suffix: str) -> str:
    """The mounted file's name, keeping the authored format's suffix.

    The Job picks its parser from the suffix exactly as the local CLI does, so a
    ``.robot`` suite stays a ``.robot`` suite all the way into the cluster.
    """
    return f"definition{source_suffix.lower()}"


def deadline_seconds(definition: TestDefinition) -> int:
    """``activeDeadlineSeconds`` for the run's Job (ADR-05-02)."""
    return int(definition.duration.seconds) + DEADLINE_MARGIN_SECONDS


def namespace_manifest(namespace: str) -> dict[str, Any]:
    """The per-test-type namespace a run's objects live in.

    It carries ``managed-by`` but deliberately **not** a ``run-id``: the namespace is
    shared by every run of that test type, so a run-scoped cleanup must never select it.
    """
    return {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {"name": namespace, "labels": {MANAGED_BY_LABEL: MANAGED_BY}},
    }


def render_manifests(
    definition: TestDefinition,
    *,
    source: str,
    source_suffix: str,
    run_id: str,
    image: str,
    namespace: str,
    service_account: str | None = None,
) -> list[dict[str, Any]]:
    """Render the objects a submission creates, in the order they must be created."""
    owner = _owner_manifest(run_id, namespace)
    owner_reference = _owner_reference(owner)
    return [
        owner,
        _definition_manifest(
            run_id,
            namespace,
            source=source,
            source_suffix=source_suffix,
            owner_reference=owner_reference,
        ),
        _job_manifest(
            definition,
            run_id=run_id,
            namespace=namespace,
            image=image,
            source_suffix=source_suffix,
            service_account=service_account,
            owner_reference=owner_reference,
        ),
    ]


def _owner_manifest(run_id: str, namespace: str) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": resource_name(run_id, "-owner"),
            "namespace": namespace,
            "labels": ownership_labels(run_id, OWNER_COMPONENT),
        },
        "data": {"run-id": run_id},
    }


def _owner_reference(owner: dict[str, Any]) -> dict[str, Any]:
    """An ownerReference to the tombstone, resolved against the created object's uid.

    The uid is not known until the API server has created it, so this carries a
    placeholder that :func:`bind_owner` replaces.
    """
    return {
        "apiVersion": owner["apiVersion"],
        "kind": owner["kind"],
        "name": owner["metadata"]["name"],
        "uid": "",
        "blockOwnerDeletion": True,
    }


def bind_owner(manifest: dict[str, Any], uid: str) -> dict[str, Any]:
    """A copy of *manifest* with the owner uid the API server assigned filled in.

    A copy, because the manifest belongs to a frozen ``Submission``. ``frozen`` only
    stops reassigning its fields; the nested dictionaries are as mutable as ever, and
    binding in place would change what a dry run of the same submission prints.
    """
    bound = copy.deepcopy(manifest)
    for reference in bound["metadata"].get("ownerReferences", []):
        if not reference["uid"]:
            reference["uid"] = uid
    return bound


def _definition_manifest(
    run_id: str,
    namespace: str,
    *,
    source: str,
    source_suffix: str,
    owner_reference: dict[str, Any],
) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": resource_name(run_id, "-definition"),
            "namespace": namespace,
            "labels": ownership_labels(run_id, "definition"),
            "ownerReferences": [dict(owner_reference)],
        },
        "data": {definition_file_name(source_suffix): source},
    }


def _job_manifest(
    definition: TestDefinition,
    *,
    run_id: str,
    namespace: str,
    image: str,
    source_suffix: str,
    service_account: str | None,
    owner_reference: dict[str, Any],
) -> dict[str, Any]:
    labels = ownership_labels(run_id, RUN_COMPONENT)
    definition_path = f"{DEFINITION_MOUNT_PATH}/{definition_file_name(source_suffix)}"
    pod_spec: dict[str, Any] = {
        "restartPolicy": "Never",
        "securityContext": {
            "runAsNonRoot": True,
            "runAsUser": 65532,
            "runAsGroup": 65532,
            "seccompProfile": {"type": "RuntimeDefault"},
        },
        "containers": [
            {
                "name": "orchestrator",
                "image": image,
                "args": ["execute", "--definition", definition_path, "--run-id", run_id],
                "env": [{"name": "TEST_ORCHESTRATOR_RUN_ID", "value": run_id}],
                "securityContext": {
                    "allowPrivilegeEscalation": False,
                    "readOnlyRootFilesystem": True,
                    "capabilities": {"drop": ["ALL"]},
                },
                "volumeMounts": [
                    {"name": "definition", "mountPath": DEFINITION_MOUNT_PATH, "readOnly": True},
                    {"name": "work", "mountPath": "/work"},
                ],
            }
        ],
        "volumes": [
            {
                "name": "definition",
                "configMap": {"name": resource_name(run_id, "-definition")},
            },
            # Robot writes output.xml per pair and rebot merges them (ADR-01-08 phase
            # 9a), which a read-only root filesystem would otherwise prevent.
            {"name": "work", "emptyDir": {}},
        ],
    }
    if service_account is not None:
        pod_spec["serviceAccountName"] = service_account

    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": resource_name(run_id),
            "namespace": namespace,
            "labels": labels,
            "ownerReferences": [dict(owner_reference)],
        },
        "spec": {
            # ADR-01-08: the exit code is the verdict, so the controller must never retry.
            "backoffLimit": 0,
            "activeDeadlineSeconds": deadline_seconds(definition),
            "template": {"metadata": {"labels": labels}, "spec": pod_spec},
        },
    }
