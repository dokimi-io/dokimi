"""Run ownership: labels, the placeholder owner object, and label-scoped deletion.

[ADR-05-02] decides three rules, and this module is where each becomes code:

* **Every created object carries run-ownership labels** — ``app.kubernetes.io/managed-by:
  dokimi`` and ``dokimi.io/run-id: <run_id>``, plus
  ``dokimi.io/test-id`` where applicable. :meth:`RunOwnership.create` is the one
  path by which the library creates objects, and it cannot create one without them.
* **Every created object carries an ownerReference** to a placeholder owner, so deleting
  the owner lets the cluster's garbage collector remove the rest even if our own cleanup
  never runs. There is no CRD in Phase 1, so the owner is a ``ConfigMap`` — see
  :func:`owner_manifest` for its shape. Garbage collection does not cross namespaces, so
  a run gets one owner **per namespace it creates objects in**, each created before
  anything else in that namespace.
* **Nothing lacking those labels is ever modified or deleted.** :func:`delete_run_owned`
  selects by label, re-checks the labels on what came back, and deletes each object by
  name *and uid*, so neither a broken selector nor a same-named replacement created in
  the meantime can widen what it touches.

The orchestrator's own Job is labelled with its ``run_id`` too, so the CLI can find it to
reattach ([ADR-01-06]), but it carries ``app.kubernetes.io/component: orchestrator`` and
is excluded from deletion — it is the thing running the run, not a resource the run
created, and a cleanup that deleted it would kill itself.

**One documented exception: a kept managed Prometheus** ([ADR-03-02]). It is kept after
the run by default, so it cannot be a child of an owner that cleanup deletes: it carries
the ownership labels, so that it stays attributable to its run and removable by
``run_id``, but no ownerReference, and :func:`delete_run_owned` leaves it alone unless
asked to remove it. :meth:`RunOwnership.create_kept` is the one path that creates such an
object. ADR-05-02 foresaw the exception ("deliberately outlives the run") and required it
documented; this is where. It never blocks a later run: exclusivity reads owners only
([ADR-05-13]).

The owner also carries the run's **claim** ([ADR-05-13]): three annotations that say
whether the run is active. :func:`claim_of` reads them and :meth:`Claim.is_active` is
the one predicate the exclusivity scan applies. No other object is evidence of an active
run — not a workload, not retained evidence, not a kept managed Prometheus.
"""

from __future__ import annotations

import copy
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from dokimi.k8s.identity import COMPONENT_LABEL, MANAGED_BY_LABEL, RUN_ID_LABEL
from dokimi.k8s.identity import MANAGED_BY as MANAGED_BY_VALUE
from dokimi.k8s.resources import (
    CONFIG_MAP,
    DAEMON_SET,
    DEPLOYMENT,
    JOB,
    POD,
    SERVICE,
    SERVICE_ACCOUNT,
    STATEFUL_SET,
    AlreadyExistsError,
    ClusterError,
    ConflictError,
    Kind,
    Manifest,
    NotFoundError,
    ResourceClient,
)
from dokimi.run_identity import RunId

__all__ = [
    "CLAIMED_AT_ANNOTATION",
    "CLAIM_EXPIRES_AT_ANNOTATION",
    "COMPONENT_LABEL",
    "FINISHED_AT_ANNOTATION",
    "RETAINED_ANNOTATION",
    "MANAGED_BY_LABEL",
    "MANAGED_BY_VALUE",
    "ORCHESTRATOR_COMPONENT",
    "OWNER_COMPONENT",
    "PROMETHEUS_COMPONENT",
    "RUN_ID_LABEL",
    "RUN_OWNED_KINDS",
    "TEST_ID_LABEL",
    "Claim",
    "DeletionReport",
    "OwnedObject",
    "OwnershipError",
    "RunOwnership",
    "claim_of",
    "delete_objects",
    "delete_run_owned",
    "format_timestamp",
    "find_run_owned",
    "is_run_owned",
    "owner_manifest",
    "owner_name",
    "ownership_labels",
    "run_selector",
]

TEST_ID_LABEL = "dokimi.io/test-id"

#: The claim, written on the owner under the run-start lock ([ADR-05-10], [ADR-05-13]).
CLAIMED_AT_ANNOTATION = "dokimi.io/claimed-at"
#: When the claim stops counting: the orchestrator Job's own deadline.
CLAIM_EXPIRES_AT_ANNOTATION = "dokimi.io/claim-expires-at"
#: Written at the start of phase 11. A finished run no longer holds the cluster.
FINISHED_AT_ANNOTATION = "dokimi.io/finished-at"
#: Written with ``finished-at`` when the run keeps its evidence, saying why. Whoever fetches
#: its artifacts then removes only its Job, and leaves the rest for ``dokimi cleanup``.
RETAINED_ANNOTATION = "dokimi.io/retained"

#: The placeholder owner ConfigMap.
OWNER_COMPONENT = "run-owner"
#: The orchestrator Job and its pod. Never deleted or counted by this module.
ORCHESTRATOR_COMPONENT = "orchestrator"
#: A managed Prometheus: run-owned, but kept after the run unless removal is asked for.
PROMETHEUS_COMPONENT = "prometheus"
#: Components :func:`delete_run_owned` keeps by default ([ADR-03-02]).
_KEPT_COMPONENTS = frozenset({PROMETHEUS_COMPONENT})

#: The kinds a run may create, and therefore the kinds label-scoped cleanup looks at. A
#: later issue that creates a new kind adds it here, or its objects become invisible to
#: cleanup. The exclusivity scan does not use this list: it reads owners only.
RUN_OWNED_KINDS: tuple[Kind, ...] = (
    CONFIG_MAP,
    POD,
    SERVICE,
    SERVICE_ACCOUNT,
    JOB,
    DEPLOYMENT,
    DAEMON_SET,
    STATEFUL_SET,
)

_OWNERSHIP_KEYS = (MANAGED_BY_LABEL, RUN_ID_LABEL, TEST_ID_LABEL)

#: Kinds that must carry ``activeDeadlineSeconds`` to be created. A Deployment cannot:
#: its pods restart by definition, so anything long-lived by design (such as a managed
#: Prometheus) is bounded by cleanup and garbage collection, not by a deadline.
_SELF_LIMITING_KINDS = (POD, JOB)


class OwnershipError(RuntimeError):
    """An object could not be given, or was found without, the ownership a run needs."""


def ownership_labels(
    run_id: RunId, *, test_id: str | None = None, component: str | None = None
) -> dict[str, str]:
    """The labels that mark an object as owned by *run_id*."""
    labels = {MANAGED_BY_LABEL: MANAGED_BY_VALUE, RUN_ID_LABEL: str(run_id)}
    if test_id is not None:
        labels[TEST_ID_LABEL] = test_id
    if component is not None:
        labels[COMPONENT_LABEL] = component
    return labels


def run_selector(run_id: RunId | None = None) -> str:
    """A label selector for one run's objects, or with no *run_id* for every run's.

    Always excludes the orchestrator component, for the reason in the module docstring.
    """
    terms = [f"{MANAGED_BY_LABEL}={MANAGED_BY_VALUE}"]
    if run_id is not None:
        terms.append(f"{RUN_ID_LABEL}={run_id}")
    else:
        terms.append(RUN_ID_LABEL)
    terms.append(f"{COMPONENT_LABEL}!={ORCHESTRATOR_COMPONENT}")
    return ",".join(terms)


def is_run_owned(manifest: Manifest, run_id: RunId | None = None) -> bool:
    """Whether *manifest* carries the ownership labels, for *run_id* if given."""
    labels: Mapping[str, str] = manifest.get("metadata", {}).get("labels") or {}
    if labels.get(MANAGED_BY_LABEL) != MANAGED_BY_VALUE:
        return False
    owner = labels.get(RUN_ID_LABEL)
    if not owner:
        return False
    return run_id is None or owner == str(run_id)


def owner_name(run_id: RunId | str) -> str:
    """The placeholder owner's name, ``run-<run_id>-owner`` ([ADR-05-13]).

    Identical in every namespace the run touches, and free of the product name, so the
    Job and the definition ConfigMap that ``submit`` creates reference it unchanged.
    """
    return f"run-{run_id}-owner"


def owner_manifest(
    run_id: RunId,
    namespace: str,
    *,
    metadata: Mapping[str, str] | None = None,
    created_at: datetime | None = None,
) -> Manifest:
    """The placeholder owner ``ConfigMap`` for *run_id* in *namespace*.

    Its shape:

    * **name** ``run-<run_id>-owner`` — findable by name for a human, though this
      library only ever finds it by label;
    * **labels** the ownership labels plus ``app.kubernetes.io/component: run-owner``, so
      the owner is itself run-owned and label-scoped cleanup removes it like anything else;
    * **data** ``run_id`` and ``created_at``, plus the run metadata passed in — for
      example :meth:`~dokimi.images.OrchestratorImage.run_metadata` — so a
      retained run can be identified from the cluster alone, without its result artifact.

    It carries no ownerReference of its own: it is the root. It carries no claim either;
    :func:`~dokimi.k8s.exclusivity.claim_run` writes that, inside the Job.
    """
    extra = dict(metadata or {})
    reserved = {"run_id", "created_at"} & extra.keys()
    if reserved:
        raise OwnershipError(f"run metadata may not override {sorted(reserved)}")
    moment = datetime.now(UTC) if created_at is None else created_at
    return {
        "apiVersion": CONFIG_MAP.api_version,
        "kind": CONFIG_MAP.kind,
        "metadata": {
            "name": owner_name(run_id),
            "namespace": namespace,
            "labels": ownership_labels(run_id, component=OWNER_COMPONENT),
        },
        "data": {
            "run_id": str(run_id),
            "created_at": moment.astimezone(UTC).isoformat(),
            **extra,
        },
    }


@dataclass(frozen=True)
class Claim:
    """What an owner's annotations say about its run ([ADR-05-13])."""

    claimed_at: datetime | None
    expires_at: datetime | None
    finished_at: datetime | None
    #: Why the finished run keeps its evidence; ``None`` if it doesn't.
    retained: str | None = None

    def is_active(self, now: datetime) -> bool:
        """Claimed, not finished, and not yet expired.

        An owner that was never claimed — a refused submission, a Job that never
        started — never counts. Neither does a claim without a readable expiry: ADR-05-13
        counts a run as active only while its ``claim-expires-at`` is in the future, so
        that a run can never block the cluster until someone deletes it by hand.
        """
        if self.claimed_at is None or self.finished_at is not None:
            return False
        return self.expires_at is not None and now < self.expires_at


def claim_of(owner: Manifest) -> Claim:
    """The claim recorded on *owner*. Unparseable timestamps read as absent."""
    annotations: Mapping[str, str] = owner.get("metadata", {}).get("annotations") or {}
    return Claim(
        claimed_at=_timestamp(annotations.get(CLAIMED_AT_ANNOTATION)),
        expires_at=_timestamp(annotations.get(CLAIM_EXPIRES_AT_ANNOTATION)),
        finished_at=_timestamp(annotations.get(FINISHED_AT_ANNOTATION)),
        retained=annotations.get(RETAINED_ANNOTATION) or None,
    )


def format_timestamp(moment: datetime) -> str:
    """How the claim annotations spell a moment: ISO 8601, UTC, second precision."""
    return moment.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


class RunOwnership:
    """Creates a run's objects, always labelled and always owned.

    One instance per run. Owners are created lazily, the first time the run creates
    something in a namespace, and always before that something.
    """

    def __init__(
        self,
        cluster: ResourceClient,
        run_id: RunId,
        *,
        metadata: Mapping[str, str] | None = None,
    ) -> None:
        self.cluster = cluster
        self.run_id = run_id
        self._metadata = dict(metadata or {})
        self._owners: dict[str, Manifest] = {}

    def owner_in(self, namespace: str) -> Manifest:
        """The owner in *namespace*, created now if the run has none there yet.

        Read first: in the Job, ``submit`` has already created the owner in the run's
        namespace, and the Job's role may read it but not create it.
        """
        owner = self._owners.get(namespace)
        if owner is not None:
            return owner
        try:
            owner = self._adoptable(namespace)
        except NotFoundError:
            body = owner_manifest(self.run_id, namespace, metadata=self._metadata)
            try:
                owner = self.cluster.create(CONFIG_MAP, namespace, body)
            except AlreadyExistsError:
                # Created between our read and our create, e.g. a retry after a lost
                # response. The same adoption rule applies.
                owner = self._adoptable(namespace)
        self._owners[namespace] = owner
        return owner

    def existing_owner_in(self, namespace: str) -> Manifest:
        """The owner ``submit`` created in *namespace*. Never creates one.

        Raises :class:`OwnershipError` if it is absent: a Job whose owner is gone has
        nothing to claim on, and nothing its objects could be owned by.
        """
        owner = self._owners.get(namespace)
        if owner is not None:
            return owner
        try:
            owner = self._adoptable(namespace)
        except NotFoundError:
            raise OwnershipError(
                f"ConfigMap {namespace}/{owner_name(self.run_id)} does not exist. "
                f"submit creates it before the Job starts, so run {self.run_id} was not "
                "submitted to this namespace, or its owner was deleted"
            ) from None
        self._owners[namespace] = owner
        return owner

    def remember(self, owner: Manifest) -> None:
        """Keep *owner*, as updated elsewhere (the claim), for later reference."""
        self._owners[owner["metadata"]["namespace"]] = owner

    def _adoptable(self, namespace: str) -> Manifest:
        """The existing owner in *namespace*, only if it really is this run's."""
        owner = self.cluster.get(CONFIG_MAP, namespace, owner_name(self.run_id))
        if not is_run_owned(owner, self.run_id):
            raise OwnershipError(
                f"ConfigMap {namespace}/{owner_name(self.run_id)} exists but is not "
                f"labelled as owned by run {self.run_id}; refusing to adopt it"
            )
        return owner

    def owner_reference(self, namespace: str) -> dict[str, str]:
        """An ownerReference to this run's owner in *namespace*.

        ``blockOwnerDeletion`` is deliberately unset: setting it requires permission on
        the owner's finalizers under the ``OwnerReferencesPermissionEnforcement``
        admission plugin, and nothing here needs deletion of the owner to wait.
        """
        owner = self.owner_in(namespace)
        metadata = owner["metadata"]
        return {
            "apiVersion": CONFIG_MAP.api_version,
            "kind": CONFIG_MAP.kind,
            "name": metadata["name"],
            "uid": metadata["uid"],
        }

    def stamp(
        self,
        manifest: Manifest,
        *,
        test_id: str | None = None,
        component: str | None = None,
        owned: bool = True,
    ) -> Manifest:
        """A copy of *manifest* carrying this run's labels and, if *owned*, ownerReference.

        The namespace must be explicit in ``metadata.namespace`` — an object placed by an
        implicit default namespace is one whose owner cannot be known in advance.
        """
        stamped = copy.deepcopy(manifest)
        metadata = stamped.setdefault("metadata", {})
        namespace = metadata.get("namespace")
        if not namespace:
            raise OwnershipError(
                f"{stamped.get('kind', 'object')} {metadata.get('name', '?')!r} has no "
                "metadata.namespace; run-owned objects must name their namespace"
            )

        labels = metadata.setdefault("labels", {})
        wanted = ownership_labels(self.run_id, test_id=test_id, component=component)
        for key in _OWNERSHIP_KEYS:
            if key in labels and labels[key] != wanted.get(key):
                raise OwnershipError(
                    f"{stamped.get('kind', 'object')} {metadata.get('name', '?')!r} already "
                    f"carries {key}={labels[key]!r}, which conflicts with run {self.run_id}"
                )
        labels.update(wanted)
        if not owned:
            return stamped

        reference = self.owner_reference(namespace)
        references = metadata.setdefault("ownerReferences", [])
        if not any(ref.get("uid") == reference["uid"] for ref in references):
            references.append(reference)
        return stamped

    def create(
        self,
        kind: Kind,
        manifest: Manifest,
        *,
        test_id: str | None = None,
        component: str | None = None,
    ) -> Manifest:
        """Create *manifest* as a run-owned object. The only creation path to use.

        Pods and Jobs are refused without ``spec.activeDeadlineSeconds``: a run-owned
        workload that can run forever is exactly what ADR-05-02 rules out. Apply one with
        :func:`~dokimi.k8s.deadlines.bound_pod` or
        :func:`~dokimi.k8s.deadlines.bound_job` first.
        """
        if kind in _SELF_LIMITING_KINDS and not manifest.get("spec", {}).get(
            "activeDeadlineSeconds"
        ):
            raise OwnershipError(
                f"{kind} {manifest.get('metadata', {}).get('name', '?')!r} has no "
                "activeDeadlineSeconds; run-owned workloads must stop on their own"
            )
        stamped = self.stamp(manifest, test_id=test_id, component=component)
        return self.cluster.create(kind, stamped["metadata"]["namespace"], stamped)

    def create_kept(self, kind: Kind, manifest: Manifest, *, component: str) -> Manifest:
        """Create *manifest* run-labelled but unowned, to outlive the run ([ADR-03-02]).

        Only for a component cleanup keeps by default — today, a managed Prometheus — and
        never for a Pod or a Job: something kept must not be a workload that sends
        traffic, and must be long-lived by design rather than bounded by a deadline.
        """
        if component not in _KEPT_COMPONENTS:
            raise OwnershipError(
                f"{component!r} is not a component cleanup keeps; create it with create()"
            )
        if kind in _SELF_LIMITING_KINDS:
            raise OwnershipError(f"a {kind} cannot be kept after its run")
        stamped = self.stamp(manifest, component=component, owned=False)
        return self.cluster.create(kind, stamped["metadata"]["namespace"], stamped)


@dataclass(frozen=True)
class OwnedObject:
    """A run-owned object found in the cluster."""

    kind: Kind
    namespace: str
    name: str
    uid: str
    run_id: str
    component: str | None
    #: The test it belongs to, for a test's pods.
    test_id: str | None = None

    @property
    def is_owner(self) -> bool:
        return self.kind == CONFIG_MAP and self.component == OWNER_COMPONENT

    def __str__(self) -> str:
        return f"{self.kind}/{self.name} in {self.namespace}"


def find_run_owned(
    cluster: ResourceClient,
    run_id: RunId | None = None,
    *,
    namespace: str | None = None,
    kinds: Sequence[Kind] = RUN_OWNED_KINDS,
    components: Collection[str] | None = None,
) -> list[OwnedObject]:
    """Every run-owned object of *kinds*, for *run_id* or for any run.

    *namespace* ``None`` means every namespace, and *components* ``None`` every component.
    The labels are re-checked on each result, so a selector the server misread cannot
    smuggle in an object that is not ours.
    """
    selector = run_selector(run_id)
    found: list[OwnedObject] = []
    for kind in kinds:
        for item in cluster.list(kind, selector, namespace):
            if not is_run_owned(item, run_id):
                continue
            metadata = item["metadata"]
            labels = metadata.get("labels") or {}
            if labels.get(COMPONENT_LABEL) == ORCHESTRATOR_COMPONENT:
                continue
            if components is not None and labels.get(COMPONENT_LABEL) not in components:
                continue
            found.append(
                OwnedObject(
                    kind=kind,
                    namespace=metadata["namespace"],
                    name=metadata["name"],
                    uid=metadata["uid"],
                    run_id=labels[RUN_ID_LABEL],
                    component=labels.get(COMPONENT_LABEL),
                    test_id=labels.get(TEST_ID_LABEL),
                )
            )
    return found


@dataclass
class DeletionReport:
    """What a scoped delete did. Failures are listed, never swallowed ([ADR-05-02])."""

    run_id: str
    deleted: list[OwnedObject] = field(default_factory=list)
    already_gone: list[OwnedObject] = field(default_factory=list)
    #: Kept on purpose, not deleted: a managed Prometheus without the removal flag.
    kept: list[OwnedObject] = field(default_factory=list)
    failed: list[tuple[OwnedObject, str]] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return not self.failed


def delete_run_owned(
    cluster: ResourceClient,
    run_id: RunId,
    *,
    namespace: str | None = None,
    kinds: Sequence[Kind] = RUN_OWNED_KINDS,
    components: Collection[str] | None = None,
    remove_kept: bool = False,
) -> DeletionReport:
    """Delete everything *run_id* owns, in *namespace* or everywhere, of *components*.

    A ``run_id`` is required: "delete every run's objects" is a different, age-bounded
    operation that belongs to the cleanup phase and needs its own guard. Dependents are
    deleted before owners, so each one is deleted and reported by us rather than raced
    by the garbage collector cascading from its owner.

    A kept component — a managed Prometheus — is listed in the report's ``kept`` and left
    alone, unless *remove_kept* is set: ADR-03-02 keeps it by default and removes it only
    on an explicit flag.
    """
    objects = find_run_owned(
        cluster, run_id, namespace=namespace, kinds=kinds, components=components
    )
    kept = [] if remove_kept else [obj for obj in objects if obj.component in _KEPT_COMPONENTS]
    report = delete_objects(cluster, run_id, [obj for obj in objects if obj not in kept])
    report.kept = kept
    return report


def delete_objects(
    cluster: ResourceClient, run_id: RunId, objects: Iterable[OwnedObject]
) -> DeletionReport:
    """Delete *objects*, found by :func:`find_run_owned` for *run_id*, each by name and uid.

    For a caller that chose which of a run's objects go, such as the test pods of only
    some tests. Dependents go before owners, for the reason :func:`delete_run_owned` gives.
    """
    report = DeletionReport(run_id=str(run_id))
    for obj in _dependents_first(obj for obj in objects if obj.run_id == str(run_id)):
        try:
            cluster.delete(obj.kind, obj.namespace, obj.name, uid=obj.uid)
        except NotFoundError:
            report.already_gone.append(obj)
        except ConflictError:
            # The name now belongs to a different object than the one we selected.
            # That object was not selected and is not ours to delete.
            report.already_gone.append(obj)
        except ClusterError as error:
            report.failed.append((obj, str(error)))
        else:
            report.deleted.append(obj)
    return report


def _dependents_first(objects: Iterable[OwnedObject]) -> list[OwnedObject]:
    return sorted(objects, key=lambda obj: obj.is_owner)
