"""What one Robot process needs to know about the run it executes a pair of.

Phase 6 runs one Robot process per pair. The suite says *what* to test,
the pair's parameters, and nothing about the run: no ``run_id``, no namespace, no image.
Those come from the Job, through the process's environment, and this is the one place
that names the variables and reads them back.

The environment rather than Robot variables, so that the retained suite stays exactly
what the user-facing conversion command renders, and can be run again with the same
environment and no edits.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field

from dokimi.cluster_name import CLUSTER_ENV_VAR, cluster_from_job_environment
from dokimi.model import ConfigError
from dokimi.run_images import images_from_job_environment, job_environment
from dokimi.testtypes.interface import WorkloadImage

__all__ = ["RUN_ID_VARIABLE", "RunContext"]

#: Already in the Job's environment: the submitted manifest sets it.
RUN_ID_VARIABLE = "DOKIMI_RUN_ID"
_NAMESPACE_VARIABLE = "DOKIMI_NAMESPACE"
_TEST_ID_VARIABLE = "DOKIMI_TEST_ID"
_SOURCE_VARIABLE = "DOKIMI_SOURCE_NODE"
_TARGET_VARIABLE = "DOKIMI_TARGET_NODE"
_REQUIRED = (
    RUN_ID_VARIABLE,
    _NAMESPACE_VARIABLE,
    _TEST_ID_VARIABLE,
    _SOURCE_VARIABLE,
    _TARGET_VARIABLE,
)


@dataclass(frozen=True, slots=True)
class RunContext:
    """The run identity and images one pair's Robot process runs under."""

    run_id: str
    namespace: str
    test_id: str
    #: The pair's nodes as phase 3 planned them. The suite carries nodes too; with more
    #: than one pair it cannot name each process's own, so the plan's are authoritative.
    source_node: str
    target_node: str
    #: What the operator called the cluster, for every result's labels.
    cluster: str
    images: Mapping[str, WorkloadImage] = field(default_factory=dict)

    def environment(self) -> dict[str, str]:
        """The variables a Robot process for this pair is started with."""
        variables = {
            RUN_ID_VARIABLE: self.run_id,
            _NAMESPACE_VARIABLE: self.namespace,
            _TEST_ID_VARIABLE: self.test_id,
            _SOURCE_VARIABLE: self.source_node,
            _TARGET_VARIABLE: self.target_node,
            CLUSTER_ENV_VAR: self.cluster,
        }
        for entry in job_environment(self.images):
            variables[entry["name"]] = entry["value"]
        return variables

    @classmethod
    def from_environment(cls, env: Mapping[str, str] | None = None) -> RunContext:
        """The context the Job started this process with.

        Raises:
            ConfigError: if a variable is missing. That is a Robot process started by
                hand without the Job's environment, and the message says which.
        """
        environment = os.environ if env is None else env
        missing = [name for name in _REQUIRED if not (environment.get(name) or "").strip()]
        if missing:
            raise ConfigError(
                "this Robot process was started without the run it belongs to: "
                f"{', '.join(missing)} not set. Phase 6 of a run sets them; to rerun a "
                "retained suite by hand, set them as the Job did"
            )
        return cls(
            run_id=environment[RUN_ID_VARIABLE].strip(),
            namespace=environment[_NAMESPACE_VARIABLE].strip(),
            test_id=environment[_TEST_ID_VARIABLE].strip(),
            source_node=environment[_SOURCE_VARIABLE].strip(),
            target_node=environment[_TARGET_VARIABLE].strip(),
            cluster=cluster_from_job_environment(environment),
            # A Robot process deploys the test's pods and nothing of the monitoring.
            images=images_from_job_environment(environment, monitoring=False),
        )
