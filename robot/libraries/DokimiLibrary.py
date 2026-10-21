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
  keyword the parser reads is always one this library implements. Each is phase 8,
  :func:`dokimi.validation.evaluate`, over the pair the plan gave this process. The
  library classifies; this adapter only renders: a ``fail`` is a failed keyword naming
  the pairs and values, an ``error`` one whose message starts with ``ERROR``, so that it
  never reads as a finding about the network. Either way the test goes on to its next
  criterion, so every one of them is in ``log.html``. What was decided is written next to
  ``output.xml`` as ``<test-id>.validation.json``.
* Each test is tagged with its outcome as it is known so far: ``outcome:pass``,
  ``outcome:fail`` or ``outcome:error``, and for an error ``error:<category>``. The tags
  come from the same classification as the file, never from Robot's status.
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
from dokimi.validation import Evaluation, ExpectedPair, Outcome, evaluate, write_validation

_RUN_NETWORK_TEST_ARGUMENTS = ["type", "protocol", "source", "target", "bandwidth", "duration"]


class CriterionFailed(AssertionError):
    """A criterion that does not hold: a finding. Continues, so all are listed."""

    ROBOT_CONTINUE_ON_FAILURE = True
    ROBOT_SUPPRESS_NAME = True


class CriterionError(AssertionError):
    """A criterion that could not be evaluated: an error, never a pass. Continues too."""

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
        #: Each test's measurement, by test id: what the criteria are evaluated on.
        self.measurements: dict[str, Measurement] = {}
        #: Each test's evaluated criteria, by test id, in the order they ran.
        self.evaluations: dict[str, list[Evaluation]] = {}
        #: Each test's outcome so far, by test id: what its tags say.
        self.outcomes: dict[str, Outcome] = {}

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
            f"Pass criterion: ``{name} <comparison> <value> <unit>``, checked against what "
            "the test measured. Fails naming the pairs and values that violate it, and "
            "fails as ``ERROR`` if a pair could not be measured, which is never a pass."
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
        try:
            # Through the module, not a name bound at import: the library's own tests
            # replace it, and nothing else may.
            result = execution.run_network_test(
                context,
                test_type=named["type"],
                protocol=named["protocol"],
                source=named["source"],
                target=named["target"],
                bandwidth=named["bandwidth"],
                duration=named["duration"],
            )
        except Exception as error:
            # Nothing was validly measured, whatever the reason: an error, not a finding.
            self._tag(context.test_id, Outcome.ERROR, (_category(error),))
            raise
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
        directory = _output_directory()
        if directory is not None:
            write_measurement(measurement, directory)
        # Measured: a pass until a criterion says otherwise. With no criteria, it stays one.
        self.evaluations[measurement.test_id] = []
        self._record(measurement.test_id)
        self._tag(measurement.test_id, Outcome.PASS)

    def _criterion(self, name: str, args: list[str]) -> None:
        criterion = criterion_from_keyword(name, args)
        context = execution.RunContext.from_environment()
        # The pair the plan gave this process, not whatever was measured: a criterion is
        # quantified over what was expected, so a missing result can't pass.
        expected = [ExpectedPair(context.test_id, context.source_node, context.target_node)]
        evaluation = evaluate(
            criterion,
            self.measurements,
            expected,
            written=" ".join([name, *args]),
            run_id=context.run_id,
        )
        logger.debug(f"PromQL equivalent, not what was evaluated: {evaluation.promql_equivalent}")
        self.evaluations.setdefault(context.test_id, []).append(evaluation)
        self._record(context.test_id)
        self._tag(context.test_id, evaluation.outcome, evaluation.categories)
        if evaluation.outcome is Outcome.ERROR:
            raise CriterionError(evaluation.message())
        if evaluation.outcome is Outcome.FAIL:
            raise CriterionFailed(evaluation.message())
        logger.info(evaluation.message())

    def _record(self, test_id: str) -> None:
        """Write the test's criteria so far next to its output, where the Job reads them."""
        directory = _output_directory()
        if directory is not None:
            write_validation(test_id, self.evaluations.get(test_id, []), directory)

    def _tag(self, test_id: str, outcome: Outcome, categories: tuple[str, ...] = ()) -> None:
        """Tag the running test with its worst outcome so far, and an error's categories."""
        worst = max(outcome, self.outcomes.get(test_id, Outcome.PASS), key=lambda o: o.rank)
        self.outcomes[test_id] = worst
        builtin = BuiltIn()
        builtin.remove_tags("outcome:*")
        builtin.set_tags(f"outcome:{worst.value}")
        if outcome is Outcome.ERROR:
            builtin.set_tags(*(f"error:{category}" for category in categories if category))


def _output_directory() -> Path | None:
    """Where the Job looks: ``${OUTPUT FILE}``'s directory, if there is an output file.

    Not ``${OUTPUT DIR}``, which is ``--outputdir``, the working directory unless given.
    """
    output_file = BuiltIn().get_variable_value("${OUTPUT FILE}")
    if not output_file or str(output_file).upper() == "NONE":
        return None
    return Path(str(output_file)).parent


def _category(error: Exception) -> str:
    """An exception's error category, as the library spells it, if it has one."""
    category = getattr(error, "category", None)
    value = getattr(category, "value", category)
    return value if isinstance(value, str) else ""


def _named(args: list[Any], kwargs: dict[str, Any]) -> dict[str, str]:
    """``Run Network Test``'s arguments by name, however the suite passed them."""
    named = dict(zip(_RUN_NETWORK_TEST_ARGUMENTS, (str(arg) for arg in args), strict=False))
    named.update({key: str(value) for key, value in kwargs.items()})
    missing = [name for name in _RUN_NETWORK_TEST_ARGUMENTS if name not in named]
    if missing:
        raise ValueError(f"{RUN_NETWORK_TEST} is missing {', '.join(missing)}")
    return named
