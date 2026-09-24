"""The package layout issue #36 prescribes must actually exist and import."""

from __future__ import annotations

import importlib

import pytest

CORE_MODULES = [
    "test_orchestrator",
    "test_orchestrator.cleanup",
    "test_orchestrator.cli",
    "test_orchestrator.diagnostics",
    "test_orchestrator.formats",
    "test_orchestrator.k8s",
    "test_orchestrator.k8s.cluster",
    "test_orchestrator.k8s.deadlines",
    "test_orchestrator.k8s.exclusivity",
    "test_orchestrator.k8s.namespaces",
    "test_orchestrator.k8s.ownership",
    "test_orchestrator.metrics",
    "test_orchestrator.model",
    "test_orchestrator.planning",
    "test_orchestrator.preflight",
    "test_orchestrator.results",
    "test_orchestrator.run_identity",
    "test_orchestrator.testtypes",
    "test_orchestrator.validation",
]


@pytest.mark.parametrize("name", CORE_MODULES)
def test_core_module_imports(name: str) -> None:
    importlib.import_module(name)
