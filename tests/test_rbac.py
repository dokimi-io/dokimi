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

from dokimi.k8s.identity import (
    MANAGED_BY,
    MANAGED_BY_LABEL,
    RUN_ID_LABEL,
    default_namespace,
)
from dokimi.model import TestType

RBAC = pathlib.Path(__file__).resolve().parent.parent / "deploy" / "rbac.yaml"


@pytest.fixture(scope="module")
def objects() -> dict[str, dict[str, Any]]:
    documents = [doc for doc in yaml.safe_load_all(RBAC.read_text()) if doc]
    return {doc["kind"]: doc for doc in documents}


def test_it_is_the_namespace_runs_actually_use(objects: dict[str, dict[str, Any]]) -> None:
    namespace = default_namespace(TestType.IPERF2)

    assert objects["Namespace"]["metadata"]["name"] == namespace
    assert objects["ServiceAccount"]["metadata"]["namespace"] == namespace
    assert objects["ClusterRoleBinding"]["subjects"][0]["namespace"] == namespace


def test_the_namespace_is_not_run_owned(objects: dict[str, dict[str, Any]]) -> None:
    # Shared by every run of the test type: a run-scoped cleanup must never select it.
    labels = objects["Namespace"]["metadata"]["labels"]

    assert labels == {MANAGED_BY_LABEL: MANAGED_BY}
    assert RUN_ID_LABEL not in labels


def test_the_binding_points_at_the_service_account_it_defines(
    objects: dict[str, dict[str, Any]],
) -> None:
    subject = objects["ClusterRoleBinding"]["subjects"][0]
    role_ref = objects["ClusterRoleBinding"]["roleRef"]

    assert subject == {
        "kind": "ServiceAccount",
        "name": objects["ServiceAccount"]["metadata"]["name"],
        "namespace": objects["ServiceAccount"]["metadata"]["namespace"],
    }
    assert role_ref["name"] == objects["ClusterRole"]["metadata"]["name"]


def test_the_grant_is_read_only(objects: dict[str, dict[str, Any]]) -> None:
    # Every later phase that needs more adds a rule of its own. This test is the place
    # that forces each widening of the grant to be a deliberate, reviewed change.
    rules = objects["ClusterRole"]["rules"]

    assert rules == [{"apiGroups": [""], "resources": ["nodes"], "verbs": ["get", "list"]}]


def test_the_error_message_names_the_service_account_this_file_creates(
    objects: dict[str, dict[str, Any]],
) -> None:
    source = (
        pathlib.Path(__file__).resolve().parent.parent / "src" / "dokimi" / "k8s" / "_kubernetes.py"
    ).read_text()
    name = objects["ServiceAccount"]["metadata"]["name"]

    assert "deploy/rbac.yaml" in source
    assert f"--service-account {name}" in source
