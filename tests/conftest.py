"""Shared test configuration.

The core library must work with Robot Framework absent (ADR-01-02), and CI proves it by
installing without the ``robot`` extra. The tests that exercise the *Robot plugin*
obviously cannot run there, so they are marked and skipped rather than failing the job
that exists to prove the opposite property.
"""

from __future__ import annotations

import importlib.metadata as metadata

import pytest

__all__ = ["robot_framework_installed"]


def robot_framework_installed() -> bool:
    """Is the Robot Framework *distribution* present?

    Deliberately not ``importlib.util.find_spec("robot")``: this repository has a
    ``robot/`` directory at its root, which Python treats as a PEP 420 namespace package,
    so that import succeeds with Robot Framework absent and proves nothing. The installed
    distribution is the thing to ask about. CI's own guard checks it the same way.
    """
    try:
        metadata.version("robotframework")
    except metadata.PackageNotFoundError:
        return False
    return True


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "requires_robot: exercises the Robot format plugin, which needs the robot extra",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if robot_framework_installed():
        return
    skip = pytest.mark.skip(
        reason="needs the robot extra; this environment proves the core works without it"
    )
    for item in items:
        if "requires_robot" in item.keywords:
            item.add_marker(skip)
