"""The package layout issue #36 prescribes must actually exist and import."""

from __future__ import annotations

import importlib

import pytest

CORE_MODULES = [
    "dokimi",
    "dokimi.cleanup",
    "dokimi.cli",
    "dokimi.diagnostics",
    "dokimi.formats",
    "dokimi.k8s",
    "dokimi.k8s.cluster",
    "dokimi.k8s.deadlines",
    "dokimi.k8s.exclusivity",
    "dokimi.k8s.namespaces",
    "dokimi.k8s.ownership",
    "dokimi.k8s.resources",
    "dokimi.metrics",
    "dokimi.model",
    "dokimi.planning",
    "dokimi.preflight",
    "dokimi.results",
    "dokimi.run_identity",
    "dokimi.testtypes",
    "dokimi.validation",
]


@pytest.mark.parametrize("name", CORE_MODULES)
def test_core_module_imports(name: str) -> None:
    importlib.import_module(name)
