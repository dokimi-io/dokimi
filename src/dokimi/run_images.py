"""The images a run deploys, chosen at submission and passed into the Job ([ADR-04-08]).

A run deploys images the CLI does not build into itself: today the test type's workload
image. [ADR-04-08] decides how a user chooses them, and this module is that decision in
code:

* **Two override levels and the release default**, the same shape :mod:`dokimi.images`
  gives the orchestrator's own image ([ADR-05-08]): ``--image <component>=<reference>``
  for one run, ``DOKIMI_<COMPONENT>_IMAGE`` for one installation, the digest baked into
  a released CLI otherwise.
* **Resolved at submission, on the submitter's side.** The submitter's environment never
  reaches the Job, so the Job cannot read an installation override itself. ``submit``
  resolves every component and writes the reference and its source into the Job's
  environment, which is also why ``--dry-run`` shows them.
* **Never in the test document.** Images are site-specific, not test-specific: the same
  test runs unchanged wherever it is submitted, while each site may pull from its own
  mirror.
* **A source build with no override is refused at submission**, before anything is
  created, rather than failing inside the Job.

The components are named, not open-ended. A test type or a deployed dependency that is
added later adds its own entry to :data:`COMPONENTS`.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from dokimi import _image_digest
from dokimi.images import ImageSource
from dokimi.model import ConfigError
from dokimi.testtypes.images import default_reference
from dokimi.testtypes.interface import WorkloadImage

__all__ = [
    "COMPONENTS",
    "Component",
    "images_from_job_environment",
    "job_environment",
    "parse_image_options",
    "resolve_run_images",
]

#: Whatever a container runtime would accept as a reference, loosely: no whitespace, and
#: not something that reads as a flag. The registry is the authority on the rest.
_REFERENCE = re.compile(r"^[A-Za-z0-9][^\s]*$")


@dataclass(frozen=True, slots=True)
class Component:
    """One image a run deploys, and where its release default comes from."""

    name: str
    description: str
    #: The released reference, read when needed rather than at import, so that the
    #: digest the release workflow bakes in is the one used.
    release_default: Callable[[], str | None]

    @property
    def env_var(self) -> str:
        """The installation override on the submitter's side, and the value in the Job."""
        return f"DOKIMI_{self.name.upper().replace('-', '_')}_IMAGE"

    @property
    def source_env_var(self) -> str:
        """Where the Job finds which level :attr:`env_var`'s value came from."""
        return f"{self.env_var}_SOURCE"


#: Every image a Milestone-01 run deploys. The managed Prometheus and the node-exporter
#: join this table with dokimi#36.
COMPONENTS: Mapping[str, Component] = {
    component.name: component
    for component in (
        Component(
            name="iperf2",
            description="the iperf2 test workload, both roles",
            release_default=lambda: default_reference("iperf2", _image_digest.IPERF2_IMAGE_DIGEST),
        ),
    )
}


def parse_image_options(values: Sequence[str]) -> dict[str, str]:
    """``--image <component>=<reference>`` values, as a mapping. Refuses anything else.

    Raises:
        ConfigError: on a malformed option, an unknown component, a component given
            twice, or a reference that cannot be one. Detected before anything is created.
    """
    chosen: dict[str, str] = {}
    for value in values:
        name, separator, reference = value.partition("=")
        name, reference = name.strip(), reference.strip()
        if not separator or not name:
            raise ConfigError(
                f"--image {value!r} is not <component>=<reference>, for example "
                "--image iperf2=registry.internal/iperf2:2.2.1"
            )
        component = _component(name)
        if component.name in chosen:
            raise ConfigError(f"--image names {component.name!r} more than once")
        chosen[component.name] = _reference(reference, f"--image {component.name}=")
    return chosen


def resolve_run_images(
    run_overrides: Mapping[str, str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> dict[str, WorkloadImage]:
    """Every component's image for one run: run override, installation override, default.

    Raises:
        ConfigError: if a component has none of the three, which is a source build that
            was given no override. Before anything is created.
    """
    overrides = dict(run_overrides or {})
    for name in overrides:
        _component(name)
    environment = os.environ if env is None else env
    resolved: dict[str, WorkloadImage] = {}
    for component in COMPONENTS.values():
        run_override = overrides.get(component.name)
        installation = (environment.get(component.env_var) or "").strip()
        if run_override is not None:
            resolved[component.name] = WorkloadImage(run_override, ImageSource.RUN_OVERRIDE)
        elif installation:
            resolved[component.name] = WorkloadImage(
                _reference(installation, f"{component.env_var}="),
                ImageSource.INSTALLATION_OVERRIDE,
            )
        elif (default := component.release_default()) is not None:
            resolved[component.name] = WorkloadImage(default, ImageSource.RELEASE_DEFAULT)
        else:
            raise ConfigError(
                f"this is a development build with no published {component.name} image, so "
                f"there is no default to run. Choose one with --image {component.name}="
                f"<reference> or {component.env_var}"
            )
    return resolved


def job_environment(images: Mapping[str, WorkloadImage]) -> list[dict[str, str]]:
    """The Job container's environment entries that carry *images* into the run."""
    entries: list[dict[str, str]] = []
    for name, image in sorted(images.items()):
        component = _component(name)
        entries.append({"name": component.env_var, "value": image.reference})
        entries.append({"name": component.source_env_var, "value": str(image.source)})
    return entries


def images_from_job_environment(env: Mapping[str, str] | None = None) -> dict[str, WorkloadImage]:
    """The images ``submit`` resolved for this run, read inside the Job.

    Raises:
        ConfigError: if a component is missing, or its source is not one the CLI writes.
            That is a Job created by hand or by an older CLI, and resubmitting fixes it.
    """
    environment = os.environ if env is None else env
    images: dict[str, WorkloadImage] = {}
    for component in COMPONENTS.values():
        reference = (environment.get(component.env_var) or "").strip()
        source = (environment.get(component.source_env_var) or "").strip()
        if not reference or not source:
            raise ConfigError(
                f"this Job was submitted without an image for {component.name} "
                f"({component.env_var} and {component.source_env_var}). Submit the run "
                "again with this version of dokimi"
            )
        try:
            images[component.name] = WorkloadImage(reference, ImageSource(source))
        except ValueError:
            raise ConfigError(
                f"{component.source_env_var} is {source!r}, which is not an image source "
                "this version of dokimi writes"
            ) from None
    return images


def _component(name: str) -> Component:
    component = COMPONENTS.get(name)
    if component is None:
        raise ConfigError(
            f"there is no component {name!r} to choose an image for. Components are: "
            + ", ".join(sorted(COMPONENTS))
        )
    return component


def _reference(reference: str, where: str) -> str:
    if not _REFERENCE.match(reference):
        raise ConfigError(f"{where}{reference!r} is not an image reference")
    return reference
