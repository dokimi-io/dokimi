"""The images a run uses, chosen at submission and passed into the Job ([ADR-04-08]).

A run uses images the CLI does not build into itself: the orchestrator's own, which the
Job runs, and the test type's workload image. [ADR-04-08] decides how a user chooses
them, and this module is that decision in code:

* **Two override levels and the release default**, for every component alike:
  ``--image <component>=<reference>`` for one run, ``DOKIMI_<COMPONENT>_IMAGE`` for one
  installation, the digest baked into a released CLI otherwise. The orchestrator keeps
  its earlier names, ``--orchestrator-image`` and ``DOKIMI_IMAGE``, as aliases; an alias
  that disagrees with the new name is refused rather than one of them silently winning.
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

**Only the submitter's side resolves.** :func:`resolve_run_images` reads ``os.environ``,
and inside the Job the same variable names carry the values ``submit`` wrote. Job-side
code calls :func:`images_from_job_environment` and never :func:`resolve_run_images`: that
would read the resolved values back as installation overrides, and fall back to a release
default where ``submit`` chose something else.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from types import ModuleType

from dokimi import _image_digest, images
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
    "resolve_component",
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
    #: Earlier names for :attr:`env_var`, still read on the submitter's side.
    env_aliases: tuple[str, ...] = ()
    #: Part of the run's own monitoring: resolved only when the run may deploy it, for
    #: ``managed`` and ``auto`` and not for ``existing`` (ADR-04-08).
    monitoring: bool = False

    @property
    def env_var(self) -> str:
        """The installation override on the submitter's side, and the value in the Job."""
        return f"DOKIMI_{self.name.upper().replace('-', '_')}_IMAGE"

    @property
    def source_env_var(self) -> str:
        """Where the Job finds which level :attr:`env_var`'s value came from."""
        return f"{self.env_var}_SOURCE"


#: Every image a Milestone-01 run uses. The managed Prometheus and the node-exporter are
#: upstream images, so their release default is a digest pinned in code rather than one
#: the release workflow bakes in, and a source build has one too.
COMPONENTS: Mapping[str, Component] = {
    component.name: component
    for component in (
        Component(
            name="orchestrator",
            description="the run's own Job",
            release_default=lambda: images.default_reference(),
            env_aliases=("DOKIMI_IMAGE",),
        ),
        Component(
            name="iperf2",
            description="the iperf2 test workload, both roles",
            release_default=lambda: default_reference("iperf2", _image_digest.IPERF2_IMAGE_DIGEST),
        ),
        Component(
            name="prometheus",
            description="the run's managed Prometheus",
            release_default=lambda: _managed().PROMETHEUS_IMAGE,
            monitoring=True,
        ),
        Component(
            name="node-exporter",
            description="the node-exporter a run deploys when the cluster has none",
            release_default=lambda: _managed().NODE_EXPORTER_IMAGE,
            monitoring=True,
        ),
    )
}


def _managed() -> ModuleType:
    # Imported when a default is read, not at import: the managed Prometheus's module
    # imports the cluster side, which this module does not need.
    from dokimi.metrics import managed

    return managed


def parse_image_options(
    values: Sequence[str], *, orchestrator_image: str | None = None
) -> dict[str, str]:
    """``--image <component>=<reference>`` values, as a mapping. Refuses anything else.

    *orchestrator_image* is ``--orchestrator-image``, the alias of ``--image orchestrator=``.

    Raises:
        ConfigError: on a malformed option, an unknown component, a component given
            twice, a reference that cannot be one, or the alias disagreeing with
            ``--image orchestrator=``. Detected before anything is created.
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
    alias = (orchestrator_image or "").strip()
    if alias:
        alias = _reference(alias, "--orchestrator-image ")
        if chosen.setdefault("orchestrator", alias) != alias:
            raise ConfigError(
                "--orchestrator-image and --image orchestrator= name different images; "
                "give one of them"
            )
    return chosen


def resolve_run_images(
    run_overrides: Mapping[str, str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    monitoring: bool = True,
) -> dict[str, WorkloadImage]:
    """Every component's image for one run: run override, installation override, default.

    *monitoring* is whether the run may deploy its own monitoring; without it the
    Prometheus and the node-exporter are not resolved.

    Raises:
        ConfigError: if a component has none of the three, which is a source build that
            was given no override. Before anything is created.
    """
    overrides = dict(run_overrides or {})
    for name in overrides:
        _component(name)
    resolved: dict[str, WorkloadImage] = {}
    for component in COMPONENTS.values():
        if component.monitoring and not monitoring:
            continue
        image = resolve_component(component, overrides.get(component.name), env=env)
        if image is None:
            raise ConfigError(
                f"this is a development build with no published {component.name} image, so "
                f"there is no default to run. Choose one with --image {component.name}="
                f"<reference> or {component.env_var}"
            )
        resolved[component.name] = image
    return resolved


def resolve_component(
    component: Component,
    run_override: str | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> WorkloadImage | None:
    """One component's image, or ``None`` if a source build was given no override.

    Raises:
        ConfigError: if the installation override and one of its aliases disagree.
    """
    if run_override is not None:
        return WorkloadImage(run_override, ImageSource.RUN_OVERRIDE)
    environment = os.environ if env is None else env
    given = {
        name: value
        for name in (component.env_var, *component.env_aliases)
        if (value := (environment.get(name) or "").strip())
    }
    if len(set(given.values())) > 1:
        raise ConfigError(
            " and ".join(given) + " name different images; set one of them, "
            f"{component.env_var} by preference"
        )
    if given:
        name, installation = next(iter(given.items()))
        return WorkloadImage(
            _reference(installation, f"{name}="), ImageSource.INSTALLATION_OVERRIDE
        )
    default = component.release_default()
    return None if default is None else WorkloadImage(default, ImageSource.RELEASE_DEFAULT)


def job_environment(images: Mapping[str, WorkloadImage]) -> list[dict[str, str]]:
    """The Job container's environment entries that carry *images* into the run."""
    entries: list[dict[str, str]] = []
    for name, image in sorted(images.items()):
        component = _component(name)
        entries.append({"name": component.env_var, "value": image.reference})
        entries.append({"name": component.source_env_var, "value": str(image.source)})
    return entries


def images_from_job_environment(
    env: Mapping[str, str] | None = None, *, monitoring: bool = True
) -> dict[str, WorkloadImage]:
    """The images ``submit`` resolved for this run, read inside the Job.

    *monitoring* as for :func:`resolve_run_images`: without it, the Prometheus and the
    node-exporter are neither expected nor read.

    Raises:
        ConfigError: if a component is missing, or its source is not one the CLI writes.
            That is a Job created by hand or by an older CLI, and resubmitting fixes it.
    """
    environment = os.environ if env is None else env
    images: dict[str, WorkloadImage] = {}
    for component in COMPONENTS.values():
        if component.monitoring and not monitoring:
            continue
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
