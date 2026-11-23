"""The manifests a submission creates: an owner object, the definition, and the Job.

Three objects, in this order, because the order is what makes cleanup survive a crash:

1. A **placeholder owner object** — a ConfigMap acting as a tombstone, since no CRD
   exists yet, named ``run-<run_id>-owner``. It is created first and owns
   everything else. It carries no claim: the Job writes that in phase 4, so what a
   submission creates is never evidence of an active run.
2. A **ConfigMap carrying the test definition** exactly as authored, mounted into the
   Job. The document travels with the run rather than being re-read from the submitter's
   disk, so the Job is self-contained and a reattaching process needs nothing local.
3. The **Job**, which is the run.

Deleting the owner cascades the cluster's own garbage collector through the other two,
so a run's resources go away even if nothing the framework wrote ever executes.

``restartPolicy: Never`` and ``backoffLimit: 0`` are not preferences. The Job's exit code
carries the run's verdict, so a Job controller that retries on non-zero would re-run the
whole matrix on a plain ``fail`` — an uncontrolled retry of everything, when a ``fail``
is never re-rolled.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from dokimi.cluster_name import CLUSTER_ENV_VAR
from dokimi.k8s.deadlines import job_budget_for
from dokimi.k8s.identity import ownership_labels
from dokimi.k8s.ownership import OWNER_COMPONENT, owner_manifest
from dokimi.model import TestDefinition
from dokimi.run_identity import RunId
from dokimi.run_images import job_environment
from dokimi.testtypes.interface import WorkloadImage

if TYPE_CHECKING:
    # Only a type here: the cleanup package sits above this module, and imports it.
    from dokimi.cleanup.retention import Retention

__all__ = [
    "DEFINITION_MOUNT_PATH",
    "JOB_TTL_SECONDS",
    "OWNER_COMPONENT",
    "RUN_COMPONENT",
    "bind_owner",
    "definition_file_name",
    "job_deadline_seconds",
    "render_manifests",
    "resource_name",
]

#: Where the definition ConfigMap is mounted inside the orchestrator container.
DEFINITION_MOUNT_PATH = "/etc/dokimi"

RUN_COMPONENT = "orchestrator"

#: How long a finished run's Job, and with it the artifacts in its log, stays for a
#: submitter to fetch: ``attach`` can fetch them once, within this hour.
JOB_TTL_SECONDS = 3600


def resource_name(run_id: str, suffix: str = "") -> str:
    """A DNS-1123 object name for a run: ``run-<run_id><suffix>``.

    The run id is used verbatim. Its format is lowercase letters, digits and
    hyphens precisely so that an object name and a label spell the same run the same way.
    """
    return f"run-{run_id}{suffix}"


def definition_file_name(source_suffix: str) -> str:
    """The mounted file's name, keeping the authored format's suffix.

    The Job picks its parser from the suffix exactly as the local CLI does, so a
    ``.robot`` suite stays a ``.robot`` suite all the way into the cluster.
    """
    return f"definition{source_suffix.lower()}"


def job_deadline_seconds(definition: TestDefinition) -> int:
    """``activeDeadlineSeconds`` for the run's Job: its :class:`JobBudget`."""
    return job_budget_for(definition).active_deadline_seconds


def render_manifests(
    definition: TestDefinition,
    *,
    source: str,
    source_suffix: str,
    run_id: str,
    image: str,
    namespace: str,
    images: Mapping[str, WorkloadImage],
    cluster: str,
    service_account: str | None = None,
    preflight_only: bool = False,
    retention: Retention | None = None,
) -> list[dict[str, Any]]:
    """Render the objects a submission creates, in the order they must be created.

    *image* is the orchestrator's own; *images* are the ones the run deploys, resolved at
    submission and handed to the Job in its environment, like *cluster*, the
    name the operator gave the cluster. *preflight_only* is ``dokimi preflight``:
    the same objects, and a Job that stops after phase 4. *retention* says when the run
    keeps its evidence; without it, the Job's own default applies.
    """
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
            images=images,
            cluster=cluster,
            source_suffix=source_suffix,
            service_account=service_account,
            owner_reference=owner_reference,
            preflight_only=preflight_only,
            retention=retention,
        ),
    ]


def _owner_manifest(run_id: str, namespace: str) -> dict[str, Any]:
    return owner_manifest(RunId(run_id), namespace)


def _owner_reference(owner: dict[str, Any]) -> dict[str, Any]:
    """An ownerReference to the tombstone, resolved against the created object's uid.

    The uid is not known until the API server has created it, so this carries a
    placeholder that :func:`bind_owner` replaces.

    ``blockOwnerDeletion`` is deliberately unset: setting it needs permission
    on the owner's finalizers, and nothing needs deleting the owner to wait.
    """
    return {
        "apiVersion": owner["apiVersion"],
        "kind": owner["kind"],
        "name": owner["metadata"]["name"],
        "uid": "",
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
    images: Mapping[str, WorkloadImage],
    cluster: str,
    source_suffix: str,
    service_account: str | None,
    owner_reference: dict[str, Any],
    preflight_only: bool = False,
    retention: Retention | None = None,
) -> dict[str, Any]:
    labels = ownership_labels(run_id, RUN_COMPONENT)
    definition_path = f"{DEFINITION_MOUNT_PATH}/{definition_file_name(source_suffix)}"
    deadline = job_deadline_seconds(definition)
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
                # The namespace and deadline let phase 4 find its owner and its Job,
                # and date its claim's expiry from the Job's start.
                "args": [
                    "execute",
                    "--definition",
                    definition_path,
                    "--run-id",
                    run_id,
                    "--namespace",
                    namespace,
                    "--deadline-seconds",
                    str(deadline),
                    *([] if retention is None else ["--retain", retention.value]),
                    *(["--preflight-only"] if preflight_only else []),
                ],
                # The images the run deploys and the cluster's name, resolved on the
                # submitter's side, whose environment the Job never sees.
                "env": [
                    {"name": "DOKIMI_RUN_ID", "value": run_id},
                    {"name": CLUSTER_ENV_VAR, "value": cluster},
                    *job_environment(images),
                ],
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
            # Robot writes output.xml per pair and rebot merges them (phase 9a), which a
            # read-only root filesystem would otherwise prevent.
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
            # The exit code is the verdict, so the controller must never retry.
            "backoffLimit": 0,
            "activeDeadlineSeconds": deadline,
            # Its artifacts can be fetched until then. Whoever fetches them deletes the
            # Job sooner; this removes one that nobody fetched.
            "ttlSecondsAfterFinished": JOB_TTL_SECONDS,
            "template": {"metadata": {"labels": labels}, "spec": pod_spec},
        },
    }
