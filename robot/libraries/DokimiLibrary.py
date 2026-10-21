"""The Robot Framework keywords of a dokimi suite: a thin adapter over the library.

This file lives outside ``src/`` deliberately. It is the Robot-facing surface, so it is
allowed to know Robot exists; the library it wraps is not (ADR-01-02, ADR-01-08).

Everything a keyword does is a call into :mod:`dokimi`. What this adapter adds is only
what is Robot's: argument passing, the log, and how a failure reads in ``log.html``.

* ``Run Network Test`` is :func:`dokimi.execution.run_network_test`, with the run's
  identity from the environment phase 6 starts this process with. Its measurement, read
  directly from the exporters (phase 7), is kept for the criteria and written next to
  this process's ``output.xml`` as ``<test-id>.measurement.json``, where the Job reads it
  to confirm Prometheus holds the same values.
* The criterion keywords are exactly the ones the Robot format's parser accepts, from
  the same table (:func:`dokimi.formats.robot_suite.criterion_keyword_names`), so a
  keyword the parser reads is always one this library implements. Evaluating them is
  phase 8, which arrives with M1-13. Until then each one **fails**, naming the criterion
  it could not check, and the test with it: a run must never look as if it passed when
  nothing was compared.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from robot.api import logger
from robot.libraries.BuiltIn import BuiltIn

from dokimi import __version__, execution
from dokimi.formats.robot_suite import (
    RUN_NETWORK_TEST,
    criterion_from_keyword,
    criterion_keyword_names,
)
from dokimi.measurement import Measurement, write_measurement
from dokimi.validation import NotEvaluatedError, evaluate

_RUN_NETWORK_TEST_ARGUMENTS = ["type", "protocol", "source", "target", "bandwidth", "duration"]


class CriterionNotEvaluated(AssertionError):
    """A criterion keyword this build cannot evaluate yet. Continues, so all are listed."""

    ROBOT_CONTINUE_ON_FAILURE = True
    ROBOT_SUPPRESS_NAME = True


class DokimiLibrary:
    """Robot library exposing the orchestration keywords.

    ``GLOBAL`` scope, per ADR-01-03's open point closed as OQ-35: tests within one cluster
    do not run in parallel, so run state and ``run_id`` are held once per Robot process.
    Robot's dynamic library API, because the criterion keywords are generated from the
    model's vocabulary rather than written out one by one.
    """

    ROBOT_LIBRARY_SCOPE = "GLOBAL"
    ROBOT_LIBRARY_VERSION = __version__

    def __init__(self) -> None:
        #: Each test's measurement, by test id: what the criteria are evaluated on (M1-13).
        self.measurements: dict[str, Measurement] = {}

    def get_keyword_names(self) -> list[str]:
        return [RUN_NETWORK_TEST, *criterion_keyword_names()]

    def get_keyword_arguments(self, name: str) -> list[str]:
        if name == RUN_NETWORK_TEST:
            return list(_RUN_NETWORK_TEST_ARGUMENTS)
        return ["comparison", "value", "*unit"]

    def get_keyword_documentation(self, name: str) -> str:
        if name == RUN_NETWORK_TEST:
            return (
                "Deploys the pair, waits until both pods are Ready, starts the test and waits "
                "for it to finish. The arguments come from the suite's variables."
            )
        return (
            f"Pass criterion: ``{name} <comparison> <value> <unit>``. Evaluating criteria "
            "arrives with M1-13; until then this keyword fails and says so."
        )

    def run_keyword(self, name: str, args: list[Any], kwargs: dict[str, Any]) -> None:
        if name == RUN_NETWORK_TEST:
            self._run_network_test(_named(args, kwargs))
            return
        self._criterion(name, [str(argument) for argument in args])

    def _run_network_test(self, named: dict[str, str]) -> None:
        context = execution.RunContext.from_environment()
        logger.info(
            f"{context.test_id}: {named['type']} over {named['protocol']}, "
            f"{named['source']} -> {named['target']}, {named['bandwidth']} for "
            f"{named['duration']}"
        )
        # Through the module, not a name bound at import: the library's own tests replace
        # it, and nothing else may.
        result = execution.run_network_test(
            context,
            test_type=named["type"],
            protocol=named["protocol"],
            source=named["source"],
            target=named["target"],
            bandwidth=named["bandwidth"],
            duration=named["duration"],
        )
        for role, pod in sorted(result.pods.items(), key=lambda item: item[0].value):
            logger.info(f"{role.value} pod: {pod}")
        for key, value in result.run_metadata.items():
            logger.info(f"{key}: {value}")
        if result.summary:
            logger.info(f"summary: {result.summary}")
        measurement = result.measurement
        self.measurements[measurement.test_id] = measurement
        for metric, value in sorted(measurement.values.items()):
            logger.info(
                f"measured ({measurement.direction}, {measurement.source}): {metric} = {value:g}"
            )
        # Next to output.xml, where the Job looks: ${OUTPUT FILE}'s directory. Not
        # ${OUTPUT DIR}, which is --outputdir, the working directory unless given.
        output_file = BuiltIn().get_variable_value("${OUTPUT FILE}")
        if output_file and str(output_file).upper() != "NONE":
            write_measurement(measurement, Path(str(output_file)).parent)

    def _criterion(self, name: str, args: list[str]) -> None:
        criterion = criterion_from_keyword(name, args)
        try:
            evaluate(criterion, " ".join([name, *args]))
        except NotEvaluatedError as error:
            raise CriterionNotEvaluated(str(error)) from error


def _named(args: list[Any], kwargs: dict[str, Any]) -> dict[str, str]:
    """``Run Network Test``'s arguments by name, however the suite passed them."""
    named = dict(zip(_RUN_NETWORK_TEST_ARGUMENTS, (str(arg) for arg in args), strict=False))
    named.update({key: str(value) for key, value in kwargs.items()})
    missing = [name for name in _RUN_NETWORK_TEST_ARGUMENTS if name not in named]
    if missing:
        raise ValueError(f"{RUN_NETWORK_TEST} is missing {', '.join(missing)}")
    return named
