"""deploy/rbac.yaml grants what the Job needs, and stays in step with the code.

The manifest is applied by hand, once, by whoever administers the cluster, and the code
that depends on it never reads it. Nothing would notice if the two drifted apart — a
renamed namespace or service account would surface only as a refused API call on a real
cluster. These tests are what notices.
"""

from __future__ import annotations

import pathlib
from typing import Any

import pytest
import yaml

from dokimi.k8s.exclusivity import DEFAULT_LOCK_NAMESPACE, LOCK_NAME
from dokimi.k8s.identity import (
    MANAGED_BY,
    MANAGED_BY_LABEL,
    RUN_ID_LABEL,
    default_namespace,
)
from dokimi.metrics.managed import PROMETHEUS_SERVICE_ACCOUNT, managed_manifests
from dokimi.model import TestType

RBAC = pathlib.Path(__file__).resolve().parent.parent / "deploy" / "rbac.yaml"
RUN_NAMESPACE = default_namespace(TestType.IPERF2)

Key = tuple[str, str, str | None]  # kind, name, namespace


@pytest.fixture(scope="module")
def objects() -> dict[Key, dict[str, Any]]:
    documents = [doc for doc in yaml.safe_load_all(RBAC.read_text()) if doc]
    return {
        (doc["kind"], doc["metadata"]["name"], doc["metadata"].get("namespace")): doc
        for doc in documents
    }


#: The Job's identity. The file also creates the managed Prometheus's.
JOB_ACCOUNT = "dokimi"
PROMETHEUS_ACCOUNT = PROMETHEUS_SERVICE_ACCOUNT


def _only(objects: dict[Key, dict[str, Any]], kind: str, name: str = JOB_ACCOUNT) -> dict[str, Any]:
    (found,) = (
        doc
        for (doc_kind, doc_name, _), doc in objects.items()
        if (doc_kind, doc_name) == (kind, name)
    )
    return found


def test_it_creates_both_namespaces_a_run_needs(objects: dict[Key, dict[str, Any]]) -> None:
    # A run never creates a namespace: the run's and the lock's come from here.
    namespaces = {name for (kind, name, _) in objects if kind == "Namespace"}

    assert namespaces == {RUN_NAMESPACE, DEFAULT_LOCK_NAMESPACE}


def test_it_is_the_namespace_runs_actually_use(objects: dict[Key, dict[str, Any]]) -> None:
    assert _only(objects, "ServiceAccount")["metadata"]["namespace"] == RUN_NAMESPACE
    assert _only(objects, "ClusterRoleBinding")["subjects"][0]["namespace"] == RUN_NAMESPACE


@pytest.mark.parametrize("name", [RUN_NAMESPACE, DEFAULT_LOCK_NAMESPACE])
def test_the_namespaces_are_not_run_owned(objects: dict[Key, dict[str, Any]], name: str) -> None:
    # Shared by every run: a run-scoped cleanup must never select them.
    labels = objects[("Namespace", name, None)]["metadata"]["labels"]

    assert labels == {MANAGED_BY_LABEL: MANAGED_BY}
    assert RUN_ID_LABEL not in labels


def test_every_binding_points_at_a_service_account_it_defines(
    objects: dict[Key, dict[str, Any]],
) -> None:
    # Each binding grants exactly one of the two identities: the Prometheus's binding is
    # the only one that names the Prometheus's account.
    for (kind, name, namespace), doc in objects.items():
        if kind not in ("ClusterRoleBinding", "RoleBinding"):
            continue
        role_kind = "ClusterRole" if kind == "ClusterRoleBinding" else "Role"
        account_name = PROMETHEUS_ACCOUNT if name == PROMETHEUS_ACCOUNT else JOB_ACCOUNT
        account = _only(objects, "ServiceAccount", account_name)["metadata"]

        assert doc["subjects"] == [
            {"kind": "ServiceAccount", "name": account["name"], "namespace": account["namespace"]}
        ]
        assert (role_kind, doc["roleRef"]["name"], namespace) in objects


def test_the_cluster_wide_grant_is_read_only(objects: dict[Key, dict[str, Any]]) -> None:
    # Every later phase that needs more adds a rule of its own. These tests are the place
    # that forces each widening of the grant to be a deliberate, reviewed change.
    rules = _only(objects, "ClusterRole")["rules"]

    assert rules == [
        {"apiGroups": [""], "resources": ["nodes"], "verbs": ["get", "list"]},
        # Phase 4: the exclusivity scan reads run owners in every namespace.
        {"apiGroups": [""], "resources": ["configmaps"], "verbs": ["list"]},
        # Phase 4: a supporting exporter already present is reused.
        {"apiGroups": [""], "resources": ["services"], "verbs": ["list"]},
    ]


def test_the_prometheus_account_is_the_one_its_deployment_runs_as(
    objects: dict[Key, dict[str, Any]],
) -> None:
    account = _only(objects, "ServiceAccount", PROMETHEUS_ACCOUNT)["metadata"]
    (_, deployment) = managed_manifests("20260101-100000-4f2a9c", RUN_NAMESPACE)[1]

    assert account["namespace"] == RUN_NAMESPACE
    assert deployment["spec"]["template"]["spec"]["serviceAccountName"] == account["name"]


def test_the_prometheus_can_only_read_what_discovery_needs(
    objects: dict[Key, dict[str, Any]],
) -> None:
    rules = _only(objects, "ClusterRole", PROMETHEUS_ACCOUNT)["rules"]

    assert rules == [
        {"apiGroups": [""], "resources": ["pods", "services"], "verbs": ["get", "list", "watch"]},
        {
            "apiGroups": ["discovery.k8s.io"],
            "resources": ["endpointslices"],
            "verbs": ["get", "list", "watch"],
        },
    ]


def test_the_lock_grant_is_confined_to_leases_in_the_lock_namespace(
    objects: dict[Key, dict[str, Any]],
) -> None:
    role = objects[("Role", "dokimi-run-lock", DEFAULT_LOCK_NAMESPACE)]

    assert role["rules"] == [
        {"apiGroups": ["coordination.k8s.io"], "resources": ["leases"], "verbs": ["create"]},
        {
            "apiGroups": ["coordination.k8s.io"],
            "resources": ["leases"],
            "resourceNames": [LOCK_NAME],
            "verbs": ["get", "update", "delete"],
        },
    ]


def test_the_run_namespace_grant_is_the_claim_the_managed_prometheus_and_the_pair_pods(
    objects: dict[Key, dict[str, Any]],
) -> None:
    # Claiming and marking finished write annotations onto the run's own owner, and the
    # claim's expiry is read from the Job's start time. Phase 4 creates the
    # managed Prometheus and, when the cluster has none, a node-exporter.
    # Phase 6 creates the pair's pods and reads them for the readiness gate and
    # completion. Nothing is deleted: the Prometheus is kept by default, and
    # cleanup is M1-16.
    role = objects[("Role", "dokimi", RUN_NAMESPACE)]

    assert role["rules"] == [
        {"apiGroups": [""], "resources": ["configmaps"], "verbs": ["get", "update"]},
        {"apiGroups": ["batch"], "resources": ["jobs"], "verbs": ["get"]},
        {"apiGroups": [""], "resources": ["configmaps", "services"], "verbs": ["create"]},
        {"apiGroups": ["apps"], "resources": ["deployments"], "verbs": ["create"]},
        {
            "apiGroups": ["apps"],
            "resources": ["daemonsets"],
            "verbs": ["create", "get", "list", "delete"],
        },
        {"apiGroups": [""], "resources": ["pods"], "verbs": ["create", "get", "list", "delete"]},
    ]


def test_the_job_cannot_exec_into_pods(objects: dict[Key, dict[str, Any]]) -> None:
    # iperf2 starts over a trigger port and reports completion through the pod's status
    # precisely so that the Job never needs a shell in every pod of the namespace.
    resources = {
        resource
        for (kind, _, _), doc in objects.items()
        if kind in ("Role", "ClusterRole")
        for rule in doc["rules"]
        for resource in rule["resources"]
    }

    assert "pods/exec" not in resources
    assert "pods/attach" not in resources


def test_the_error_message_names_the_service_account_this_file_creates(
    objects: dict[Key, dict[str, Any]],
) -> None:
    source = (
        pathlib.Path(__file__).resolve().parent.parent / "src" / "dokimi" / "k8s" / "_kubernetes.py"
    ).read_text()
    name = _only(objects, "ServiceAccount")["metadata"]["name"]

    assert "deploy/rbac.yaml" in source
    assert f"--service-account {name}" in source
