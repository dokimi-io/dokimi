"""The ``cluster`` a run executes against, as its operator named it ([ADR-02-07]).

``cluster`` is a required label on every metric and a column of the result artifact, so
that results from different clusters stay separable in history. [ADR-02-07] decides how it
is populated, and this module is that decision in code:

* **Supplied by the operator at submission, required, no default.** ``--cluster`` for one
  run, ``DOKIMI_CLUSTER`` for one installation, the same two levels every image has
  ([ADR-04-08]). Nothing is derived: not the kubeconfig context, not the ``kube-system``
  UID. A silently defaulted name would merge distinct clusters in exactly the history this
  label exists to keep apart.
* **Resolved on the submitter's side and passed into the Job**, whose environment the
  submitter's never reaches. ``submit`` writes it into the Job as ``DOKIMI_CLUSTER``; the
  Job reads that back and never falls back to anything.
* **A DNS label** (``OQ-103``, decided 2026-10-13): lowercase letters, digits and hyphens,
  starting and ending with a letter or digit, at most 63 characters. It is then one
  spelling everywhere it goes, as a Prometheus label value, a Kubernetes label value and a
  CSV cell, with no escaping. Anything else is refused, never normalised: ``Prod-EU``
  quietly becoming ``prod-eu`` would merge it with a cluster someone else called that.
  Loosening the rule later is possible; tightening it would strand history.

The value records what the operator called the cluster, not a stable identity: two
operators can name one cluster differently, and nothing detects that. Deriving one is
``OQ-75``, deferred to Phase 6.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping

from dokimi.model import ConfigError

__all__ = ["CLUSTER_ENV_VAR", "cluster_from_job_environment", "resolve_cluster"]

#: The installation level on the submitter's side, and the resolved value in the Job.
CLUSTER_ENV_VAR = "DOKIMI_CLUSTER"

_DNS_LABEL = re.compile(r"[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?")


def resolve_cluster(flag: str | None = None, *, env: Mapping[str, str] | None = None) -> str:
    """The run's cluster name: ``--cluster`` over ``DOKIMI_CLUSTER``. Never a default.

    Surrounding whitespace is trimmed, the one thing that is: a name pasted with a space
    or read from a file with its newline means the same name. Everything inside it is
    held to the DNS-label rule as given.

    Raises:
        ConfigError: if neither is given, or the name is not a DNS label. Before
            anything is created.
    """
    given = (flag or "").strip()
    if given:
        return _validated(given, "--cluster")
    environment = os.environ if env is None else env
    installation = (environment.get(CLUSTER_ENV_VAR) or "").strip()
    if installation:
        return _validated(installation, CLUSTER_ENV_VAR)
    raise ConfigError(
        "name the cluster this run executes against with --cluster <name> or "
        f"{CLUSTER_ENV_VAR}. It labels every result, and has no default, so that results "
        "from different clusters are never merged (ADR-02-07)"
    )


def cluster_from_job_environment(env: Mapping[str, str] | None = None) -> str:
    """The cluster name ``submit`` resolved for this run, read inside the Job.

    Raises:
        ConfigError: if it is missing or malformed: a Job created by hand or by an older
            CLI, and resubmitting fixes it.
    """
    environment = os.environ if env is None else env
    value = (environment.get(CLUSTER_ENV_VAR) or "").strip()
    if not value:
        raise ConfigError(
            f"this Job was submitted without a cluster name ({CLUSTER_ENV_VAR}). Submit the "
            "run again with this version of dokimi"
        )
    return _validated(value, CLUSTER_ENV_VAR)


def _validated(value: str, where: str) -> str:
    if not _DNS_LABEL.fullmatch(value):
        raise ConfigError(
            f"{where} {value!r} is not a cluster name: use lowercase letters, digits and "
            "hyphens, starting and ending with a letter or digit, at most 63 characters"
        )
    return value
