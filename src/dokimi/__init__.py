"""dokimi: orchestration library for Kubernetes network validation.

This package is the orchestration library. It must remain buildable, callable and
testable **with Robot Framework not installed** (ADR-01-02). Robot is a thin adapter over
this code, alongside the CLI, a CI entry point and a future Operator — never a dependency
of it.

The one exception is the Robot *format plugin* under :mod:`dokimi.formats`,
which may import Robot's parsing API to read a ``.robot`` suite into the canonical model.
That is a different role from Robot-as-runner, and ADR-01-08 keeps the two distinct.

``tests/test_core_independence.py`` enforces this as a test rather than a convention.
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
