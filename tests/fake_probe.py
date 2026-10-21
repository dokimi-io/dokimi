"""A probe whose pods are already in the state a test asks for, for the preflight's tests.

The real ``iperf2`` probe builds the workload, and this adds a pod status to each pod
before it is created. The fake store keeps a created object as given, so the real
``deploy()``, readiness gate and result path read exactly that status, with no kubelet
and no waiting. ``start()`` and the report are scripted.
"""

from __future__ import annotations

import copy
from typing import Any

from dokimi.testtypes.interface import (
    DeployedWorkload,
    ParameterSchema,
    Placement,
    Role,
    TestParameters,
    WorkloadSpec,
)
from dokimi.testtypes.iperf2 import Iperf2
from dokimi.testtypes.probe import ProbeResult

__all__ = ["READY_IPS", "FakeProbe", "pod_status"]

READY_IPS = {Role.CLIENT: "10.244.1.10", Role.SERVER: "10.244.2.20"}

PASSED = ProbeResult(None, "UDP to the server pod at 10.244.2.20:5001 arrived")


def pod_status(role: Role, *, waiting: str | None = None, phase: str = "Running") -> dict[str, Any]:
    """A pod's status: Ready, or waiting with *waiting* as its container's reason."""
    if waiting is None:
        return {
            "phase": phase,
            "podIPs": [{"ip": READY_IPS[role]}],
            "conditions": [{"type": "Ready", "status": "True"}],
        }
    return {
        "phase": "Pending",
        "conditions": [{"type": "Ready", "status": "False"}],
        "containerStatuses": [{"name": "iperf2", "state": {"waiting": {"reason": waiting}}}],
    }


class FakeProbe:
    """The ``iperf2`` probe's workload, with a scripted status, start and report."""

    def __init__(
        self,
        *,
        result: ProbeResult | None = PASSED,
        statuses: dict[Role, dict[str, Any]] | None = None,
        start_error: Exception | None = None,
    ) -> None:
        self.real = Iperf2(image="ghcr.io/dokimi-io/iperf2@sha256:" + "ab" * 32)
        self.result = result
        self.statuses = statuses or {role: pod_status(role) for role in Role}
        self.start_error = start_error
        self.specs: list[WorkloadSpec] = []
        self.started: list[DeployedWorkload] = []

    def parameter_schema(self) -> ParameterSchema:
        return self.real.parameter_schema()

    def probe(self, params: TestParameters, placement: Placement) -> WorkloadSpec:
        spec = self.real.probe(params, placement)
        pods = {}
        for role, pod in spec.pods.items():
            pods[role] = copy.deepcopy(pod)
            pods[role]["status"] = copy.deepcopy(self.statuses[role])
        self.specs.append(spec)
        return WorkloadSpec(
            namespace=spec.namespace,
            test_id=spec.test_id,
            pods=pods,
            run_metadata=spec.run_metadata,
            warmup_seconds=spec.warmup_seconds,
        )

    def start(self, deployed: DeployedWorkload) -> None:
        if self.start_error is not None:
            raise self.start_error
        # As the real start() does: the server's address, sent to the client's.
        deployed.pod_ip(Role.SERVER)
        deployed.pod_ip(Role.CLIENT)
        self.started.append(deployed)

    def probe_result(self, deployed: DeployedWorkload) -> ProbeResult | None:
        return self.result
