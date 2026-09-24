"""An in-memory :class:`~test_orchestrator.k8s.cluster.ClusterClient` for tests.

It reproduces the API-server behaviour the safety rules depend on, and nothing else:
``create`` is atomic and fails on a taken name, ``replace`` and ``delete`` honour
``resourceVersion`` and uid preconditions, label selectors are evaluated server-side, and
all of it is serialised by one lock so concurrent callers race the way real ones do.
"""

from __future__ import annotations

import copy
import itertools
import threading
import uuid
from collections.abc import Callable

from test_orchestrator.k8s.cluster import (
    AlreadyExistsError,
    ConflictError,
    Kind,
    Manifest,
    NotFoundError,
)

Key = tuple[str, str, str]  # kind, namespace, name
Hook = Callable[[Kind, str, Manifest], None]


class FakeCluster:
    def __init__(self, namespaces: set[str] | None = None) -> None:
        self.namespaces = (
            {"default", "test-orchestrator", "test-orchestrator-iperf2"}
            if namespaces is None
            else namespaces
        )
        self.objects: dict[Key, Manifest] = {}
        self.created: list[tuple[Kind, Manifest]] = []
        self._lock = threading.RLock()
        self._versions = itertools.count(1)
        #: Called before each create, outside the lock, so a test can interleave callers.
        self.before_create: Hook | None = None

    # -- ClusterClient -------------------------------------------------------------------

    def create(self, kind: Kind, namespace: str, body: Manifest) -> Manifest:
        if self.before_create is not None:
            self.before_create(kind, namespace, body)
        with self._lock:
            if namespace not in self.namespaces:
                raise NotFoundError(f"namespaces {namespace!r} not found")
            stored = copy.deepcopy(body)
            metadata = stored.setdefault("metadata", {})
            metadata["namespace"] = namespace
            key = (kind.kind, namespace, metadata["name"])
            if key in self.objects:
                raise AlreadyExistsError(f"{kind} {metadata['name']!r} already exists")
            metadata["uid"] = str(uuid.uuid4())
            metadata["resourceVersion"] = str(next(self._versions))
            self.objects[key] = stored
            self.created.append((kind, copy.deepcopy(stored)))
            return copy.deepcopy(stored)

    def get(self, kind: Kind, namespace: str, name: str) -> Manifest:
        with self._lock:
            try:
                return copy.deepcopy(self.objects[(kind.kind, namespace, name)])
            except KeyError:
                raise NotFoundError(f"{kind} {name!r} not found") from None

    def list(self, kind: Kind, label_selector: str, namespace: str | None = None) -> list[Manifest]:
        with self._lock:
            return [
                copy.deepcopy(obj)
                for (obj_kind, obj_namespace, _), obj in self.objects.items()
                if obj_kind == kind.kind
                and (namespace is None or obj_namespace == namespace)
                and matches(label_selector, obj.get("metadata", {}).get("labels") or {})
            ]

    def replace(self, kind: Kind, namespace: str, body: Manifest) -> Manifest:
        with self._lock:
            key = (kind.kind, namespace, body["metadata"]["name"])
            current = self.objects.get(key)
            if current is None:
                raise NotFoundError(f"{kind} {key[2]!r} not found")
            expected = body["metadata"].get("resourceVersion")
            if expected != current["metadata"]["resourceVersion"]:
                raise ConflictError(f"{kind} {key[2]!r} was modified")
            stored = copy.deepcopy(body)
            stored["metadata"]["namespace"] = namespace
            stored["metadata"]["uid"] = current["metadata"]["uid"]
            stored["metadata"]["resourceVersion"] = str(next(self._versions))
            self.objects[key] = stored
            return copy.deepcopy(stored)

    def delete(
        self,
        kind: Kind,
        namespace: str,
        name: str,
        *,
        uid: str,
        resource_version: str | None = None,
    ) -> None:
        with self._lock:
            key = (kind.kind, namespace, name)
            current = self.objects.get(key)
            if current is None:
                raise NotFoundError(f"{kind} {name!r} not found")
            if current["metadata"]["uid"] != uid:
                raise ConflictError(f"{kind} {name!r} uid precondition failed")
            if resource_version is not None and (
                current["metadata"]["resourceVersion"] != resource_version
            ):
                raise ConflictError(f"{kind} {name!r} resourceVersion precondition failed")
            del self.objects[key]

    # -- test helpers ----------------------------------------------------------------------

    def put(self, kind: Kind, namespace: str, manifest: Manifest) -> Manifest:
        """Create an object as some other actor would, bypassing hooks."""
        hook, self.before_create = self.before_create, None
        try:
            return self.create(kind, namespace, manifest)
        finally:
            self.before_create = hook

    def exists(self, kind: Kind, namespace: str, name: str) -> bool:
        with self._lock:
            return (kind.kind, namespace, name) in self.objects


def matches(selector: str, labels: dict[str, str]) -> bool:
    """Evaluate the equality-based label selector subset: ``k=v``, ``k!=v`` and ``k``."""
    for term in filter(None, (part.strip() for part in selector.split(","))):
        if "!=" in term:
            key, value = term.split("!=", 1)
            if labels.get(key) == value:
                return False
        elif "=" in term:
            key, value = term.split("=", 1)
            if labels.get(key) != value:
                return False
        elif term not in labels:
            return False
    return True
