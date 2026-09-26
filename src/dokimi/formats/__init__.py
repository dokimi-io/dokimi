"""Authoring format plugins: ``parse()`` and ``render()``.

Robot and YAML are peer authoring formats, each a plugin against the canonical model
(ADR-04-05). ADR-04-06 puts both in Milestone-01, because the property that makes them
peers — no criterion expressible in one but not the other — is not a property a single
format can have.

Robot Framework's *parsing* API is imported inside
:mod:`dokimi.formats.robot_suite` only, and only when parsing is actually
called, so the core library stays importable without it (ADR-01-02, ADR-01-08 phase 2).
Rendering needs no Robot at all.

Conversion between formats is a composition of two functions that already exist, rather
than a transformer per format pair::

    render_robot(parse_yaml(document))
    render_yaml(parse_robot(suite))
"""

from __future__ import annotations

from pathlib import Path

from dokimi.formats.robot_suite import parse_robot, render_robot
from dokimi.formats.yaml_format import parse_yaml, render_yaml
from dokimi.model import ConfigError, TestDefinition

__all__ = [
    "SUFFIXES",
    "parse",
    "parse_file",
    "parse_robot",
    "parse_yaml",
    "render_robot",
    "render_yaml",
]

#: File suffixes each format claims.
SUFFIXES = {
    ".robot": "robot",
    ".yaml": "yaml",
    ".yml": "yaml",
}

_PARSERS = {
    "robot": parse_robot,
    "yaml": parse_yaml,
}


def parse(source: str, fmt: str) -> TestDefinition:
    """Parse ``source`` using the named format."""
    parser = _PARSERS.get(fmt)
    if parser is None:
        raise ConfigError(
            f"unknown format {fmt!r}. Accepted formats are: "
            + ", ".join(repr(name) for name in sorted(_PARSERS))
        )
    return parser(source)


def parse_file(path: Path) -> TestDefinition:
    """Parse a test definition, choosing the format from the file suffix."""
    fmt = SUFFIXES.get(path.suffix.lower())
    if fmt is None:
        raise ConfigError(
            f"cannot tell which format {path.name!r} is from its suffix. "
            f"Expected one of: {', '.join(sorted(SUFFIXES))}"
        )
    return parse(path.read_text(encoding="utf-8"), fmt)
