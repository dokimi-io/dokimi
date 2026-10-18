"""The cluster's name: given at submission, required, a DNS label, never derived (ADR-02-07)."""

from __future__ import annotations

import pytest

from dokimi.cluster_name import CLUSTER_ENV_VAR, cluster_from_job_environment, resolve_cluster
from dokimi.model import ConfigError


def test_the_flag_wins_over_the_installation_override() -> None:
    assert resolve_cluster("lab-fra-1", env={CLUSTER_ENV_VAR: "lab-ber-2"}) == "lab-fra-1"


def test_the_installation_override_names_it_without_a_flag() -> None:
    assert resolve_cluster(env={CLUSTER_ENV_VAR: "lab-ber-2"}) == "lab-ber-2"


@pytest.mark.parametrize("flag", [None, "", "   "])
def test_there_is_no_default(flag: str | None) -> None:
    # A defaulted name would merge distinct clusters in history.
    with pytest.raises(ConfigError, match="--cluster <name> or DOKIMI_CLUSTER"):
        resolve_cluster(flag, env={})


def test_a_blank_installation_override_is_no_name() -> None:
    with pytest.raises(ConfigError, match="has no default"):
        resolve_cluster(env={CLUSTER_ENV_VAR: "  "})


@pytest.mark.parametrize(
    "name",
    [
        "Prod-EU",  # refused, never lowercased into someone else's name
        "prod eu",
        "prod_eu",
        "-prod",
        "prod-",
        "prod.eu",
        "a" * 64,
        "prÖd",
    ],
)
def test_anything_but_a_dns_label_is_refused_not_normalised(name: str) -> None:
    with pytest.raises(ConfigError, match="is not a cluster name"):
        resolve_cluster(name, env={})


@pytest.mark.parametrize("name", ["a", "kind", "lab-fra-1", "0x2a", "a" * 63])
def test_a_dns_label_is_taken_verbatim(name: str) -> None:
    assert resolve_cluster(name, env={}) == name


def test_a_malformed_installation_override_names_where_it_came_from() -> None:
    with pytest.raises(ConfigError, match="DOKIMI_CLUSTER 'Lab'"):
        resolve_cluster(env={CLUSTER_ENV_VAR: "Lab"})


def test_the_job_reads_back_what_submit_wrote() -> None:
    assert cluster_from_job_environment({CLUSTER_ENV_VAR: "lab-fra-1"}) == "lab-fra-1"


def test_a_job_without_a_cluster_name_is_refused() -> None:
    # Made by hand or by an older CLI: the Job never invents one.
    with pytest.raises(ConfigError, match="submitted without a cluster name"):
        cluster_from_job_environment({})


@pytest.mark.parametrize(
    ("flag", "env"),
    [(" lab-fra-1 ", {}), (None, {CLUSTER_ENV_VAR: "lab-fra-1\n"})],
)
def test_surrounding_whitespace_is_trimmed_and_nothing_else(
    flag: str | None, env: dict[str, str]
) -> None:
    # A name pasted with a space, or read from a file with its newline, is the same name.
    assert resolve_cluster(flag, env=env) == "lab-fra-1"


def test_whitespace_inside_the_name_is_still_refused() -> None:
    with pytest.raises(ConfigError, match="is not a cluster name"):
        resolve_cluster("lab\nfra", env={})


def test_a_trailing_newline_is_refused_by_the_rule_itself() -> None:
    # The trim removes it first today; the rule must hold without it, which a `$` anchor
    # alone would not ("prod\n" matches `^prod$`).
    from dokimi import cluster_name  # noqa: PLC0415

    with pytest.raises(ConfigError, match="is not a cluster name"):
        cluster_name._validated("prod\n", "--cluster")


def test_a_job_given_a_malformed_name_is_refused() -> None:
    # A Job edited by hand: it never normalises either.
    with pytest.raises(ConfigError, match="DOKIMI_CLUSTER 'Prod-EU' is not a cluster name"):
        cluster_from_job_environment({CLUSTER_ENV_VAR: "Prod-EU"})
