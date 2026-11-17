"""
The vector database: Qdrant, behind the few calls this service needs.

    ensure          a collection of the right dimension exists
    vectors_for     the stored vectors for these text digests (reuse, not re-embed)
    upsert          write points
    keep_only       delete every point not in this set
    search          top-k by cosine, filtered on payload keywords

Nothing here knows what a playbook is. The playbook store decides what a point
is (one embedded field, or one step of a runbook) and what its payload holds;
this module stores and searches them.

Where the data lives, by configuration:

    QDRANT_URL set      a Qdrant server (the `qdrant` service in docker-compose)
    QDRANT_PATH set     Qdrant's embedded local mode, files on a volume
    neither             in memory, gone on restart (tests)

Every method blocks — an HTTP round trip, or disk I/O in local mode — so the
store calls them through `run_in_threadpool`, never on the event loop. Any
failure is raised as VectorDBUnavailable: matching by meaning goes quiet and
says why, and matching by name carries on without it.

One collection per embedding model (`collection_name`). Vectors from two models
are not comparable, so a model change is a new collection, filled on the next
sync, rather than a mix that answers with nonsense similarities.
"""

from __future__ import annotations

import logging
import re
import threading
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterable

import numpy as np

logger = logging.getLogger("vectordb")

# Fixed, so a point's id is the same on every machine and every run: the id is
# derived from what the point is, which is what makes an upsert idempotent.
_NAMESPACE = uuid.UUID("6f1c2b0e-5a0f-4c1e-9b9a-3f4d6c2a7e51")
_BATCH = 256


class VectorDBUnavailable(RuntimeError):
    """Qdrant could not be reached, or refused the call. Matching by name carries on."""


@dataclass
class Point:
    id: str
    vector: np.ndarray
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class Hit:
    id: str
    score: float
    payload: dict[str, Any]


def _models():
    try:
        from qdrant_client import models
    except ImportError as exc:
        raise VectorDBUnavailable("qdrant-client is not installed (requirements.txt)") from exc
    return models


def point_id(*parts: str) -> str:
    """A deterministic UUID for a point, from what identifies it."""
    return str(uuid.uuid5(_NAMESPACE, "\x1f".join(parts)))


def collection_name(prefix: str, model: str) -> str:
    """'playbooks', 'BAAI/bge-small-en-v1.5' -> 'playbooks__baai-bge-small-en-v1-5'."""
    slug = re.sub(r"[^a-z0-9]+", "-", (model or "none").lower()).strip("-")
    return f"{prefix}__{slug}"


class VectorDB:
    def __init__(self, url: str = "", api_key: str = "", path: str = "",
                 timeout: int = 10, client: Any = None) -> None:
        self.url = url.strip()
        self.api_key = api_key
        self.path = path.strip()
        self.timeout = timeout
        self._client = client
        # Local mode is one process, one file lock, and not safe across threads;
        # a server does not need it, and the cost of taking turns here is small.
        self._lock = threading.Lock()

    @property
    def where(self) -> str:
        """Where the vectors are, for /playbooks. Never includes the API key."""
        if self.url:
            return self.url
        return f"local:{self.path}" if self.path else ":memory:"

    def _qc(self):
        if self._client is None:
            try:
                from qdrant_client import QdrantClient
            except ImportError as exc:
                raise VectorDBUnavailable("qdrant-client is not installed (requirements.txt)") from exc
            try:
                if self.url:
                    self._client = QdrantClient(url=self.url, api_key=self.api_key or None,
                                                timeout=self.timeout)
                elif self.path:
                    self._client = QdrantClient(path=self.path)
                else:
                    self._client = QdrantClient(location=":memory:")
            except Exception as exc:  # noqa: BLE001 — any failure to open means the same thing
                raise VectorDBUnavailable(f"could not open Qdrant at {self.where}: "
                                          f"{type(exc).__name__}: {exc}") from exc
        return self._client

    def _call(self, what: str, fn):
        with self._lock:
            client = self._qc()
            try:
                return fn(client)
            except VectorDBUnavailable:
                raise
            except Exception as exc:  # noqa: BLE001 — connection, timeout, bad request: all one outcome
                raise VectorDBUnavailable(f"Qdrant {what} failed at {self.where}: "
                                          f"{type(exc).__name__}: {exc}") from exc

    # ── Collections ─────────────────────────────────────────────────────────

    def exists(self, collection: str) -> bool:
        return self._call("collection check", lambda qc: qc.collection_exists(collection))

    def ensure(self, collection: str, dim: int) -> None:
        """A cosine collection of this dimension. One of another dimension is replaced."""
        models = _models()

        def run(qc):
            if qc.collection_exists(collection):
                size = qc.get_collection(collection).config.params.vectors.size
                if size == dim:
                    return
                logger.warning("collection %s holds %d-d vectors, not %d — recreating it",
                               collection, size, dim)
                qc.delete_collection(collection)
            qc.create_collection(
                collection,
                vectors_config=models.VectorParams(size=dim, distance=models.Distance.COSINE),
            )
            if self.url:
                # Payload indexes are a server feature; local mode only warns about them.
                for key in ("page_id", "field", "digest"):
                    qc.create_payload_index(collection, key, models.PayloadSchemaType.KEYWORD)
            logger.info("created collection %s (%d-d, cosine) at %s", collection, dim, self.where)

        self._call("ensure", run)

    def drop_others(self, prefix: str, keep: str) -> list[str]:
        """Delete this prefix's collections other than `keep` — an old model's vectors."""
        def run(qc):
            dropped = []
            for c in qc.get_collections().collections:
                if c.name.startswith(prefix + "__") and c.name != keep:
                    qc.delete_collection(c.name)
                    dropped.append(c.name)
            return dropped

        dropped = self._call("drop", run)
        if dropped:
            logger.info("dropped collection(s) of an earlier model: %s", ", ".join(dropped))
        return dropped

    def count(self, collection: str) -> int:
        def run(qc):
            if not qc.collection_exists(collection):
                return 0
            return qc.count(collection, exact=True).count

        return self._call("count", run)

    # ── Points ──────────────────────────────────────────────────────────────

    def vectors_for(self, collection: str, digests: Iterable[str]) -> dict[str, np.ndarray]:
        """{digest: vector} for the digests already stored. Missing ones are simply absent."""
        models = _models()

        wanted = sorted(set(digests))
        if not wanted:
            return {}

        def run(qc):
            if not qc.collection_exists(collection):
                return {}
            found: dict[str, np.ndarray] = {}
            for start in range(0, len(wanted), _BATCH):
                chunk = wanted[start:start + _BATCH]
                flt = models.Filter(must=[
                    models.FieldCondition(key="digest", match=models.MatchAny(any=chunk)),
                ])
                offset = None
                while True:
                    points, offset = qc.scroll(collection, scroll_filter=flt, limit=_BATCH,
                                               offset=offset, with_payload=["digest"],
                                               with_vectors=True)
                    for p in points:
                        found.setdefault(p.payload["digest"], np.asarray(p.vector, dtype=np.float32))
                    if offset is None:
                        break
            return found

        return self._call("read", run)

    def upsert(self, collection: str, points: list[Point]) -> None:
        models = _models()

        def run(qc):
            for start in range(0, len(points), _BATCH):
                qc.upsert(collection, wait=True, points=[
                    models.PointStruct(id=p.id, vector=np.asarray(p.vector, np.float32).tolist(),
                                       payload=p.payload)
                    for p in points[start:start + _BATCH]
                ])

        self._call("upsert", run)

    def keep_only(self, collection: str, ids: Iterable[str]) -> int:
        """Delete every point whose id is not in `ids`. Returns how many went."""
        models = _models()

        keep = set(ids)

        def run(qc):
            if not qc.collection_exists(collection):
                return 0
            stale: list[str] = []
            offset = None
            while True:
                points, offset = qc.scroll(collection, limit=_BATCH, offset=offset,
                                           with_payload=False, with_vectors=False)
                stale += [str(p.id) for p in points if str(p.id) not in keep]
                if offset is None:
                    break
            for start in range(0, len(stale), _BATCH):
                qc.delete(collection, wait=True,
                          points_selector=models.PointIdsList(points=stale[start:start + _BATCH]))
            return len(stale)

        return self._call("delete", run)

    def search(self, collection: str, vector: np.ndarray, limit: int,
               must: dict[str, str | list[str]] | None = None) -> list[Hit]:
        """
        The nearest points by cosine, best first.

        `must` filters on payload keywords: a string is an exact match, a list
        is "any of". The store filters on `field` so a search for a playbook's
        criteria never lands on a runbook step, and on `page_id` to search
        within one playbook.
        """
        models = _models()

        conditions = []
        for key, value in (must or {}).items():
            match = (models.MatchAny(any=list(value)) if isinstance(value, (list, tuple, set))
                     else models.MatchValue(value=value))
            conditions.append(models.FieldCondition(key=key, match=match))

        def run(qc):
            if not qc.collection_exists(collection):
                return []
            found = qc.query_points(
                collection, query=np.asarray(vector, np.float32).tolist(), limit=limit,
                query_filter=models.Filter(must=conditions) if conditions else None,
                with_payload=True,
            ).points
            return [Hit(str(p.id), float(p.score), dict(p.payload or {})) for p in found]

        return self._call("search", run)

    def close(self) -> None:
        with self._lock:
            if self._client is not None:
                try:
                    self._client.close()
                except Exception:  # noqa: BLE001 — closing is best effort
                    pass
                self._client = None
