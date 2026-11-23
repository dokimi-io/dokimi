"""Test type plugins.

Each test type declares its images, parameters, pod specs, capabilities for a given
parameter set and environment, ``start()``, ``is_complete()`` and its exporter.
The interface is
:class:`~dokimi.testtypes.interface.TestTypePlugin`; :func:`plugin_for` is how the
rest of the library gets from a definition's :class:`~dokimi.model.TestType` to the
code that runs it, and :func:`parameters_for` is how a definition becomes that
plugin's parameters.
"""

from __future__ import annotations

from dokimi.model import TestDefinition, TestType
from dokimi.testtypes.interface import TestTypePlugin, WorkloadImage
from dokimi.testtypes.iperf2 import Iperf2

__all__ = ["parameters_for", "plugin_for"]


def plugin_for(test_type: TestType, *, image: WorkloadImage | str | None = None) -> TestTypePlugin:
    """The plugin for *test_type*. *image* replaces its release-default image."""
    match test_type:
        case TestType.IPERF2:
            return Iperf2(image=image)


def parameters_for(definition: TestDefinition) -> dict[str, str]:
    """*definition*'s test parameters, spelled as its plugin's schema accepts them.

    The canonical model carries exactly the parameters Milestone-01 is scoped to.
    A parameter the model does not carry — ``iperf2``'s ``warmup`` — takes the schema's
    default, which is where the warm-up pass belongs: on unless disabled.
    """
    return {
        "protocol": definition.protocol.value,
        "targetBandwidth": str(definition.target_bandwidth),
        "duration": str(definition.duration),
    }
