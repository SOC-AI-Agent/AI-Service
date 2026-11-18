"""
The playbook index: built from the pages the backend sends, the playbooks kept
on a volume, their vectors in Qdrant (vectordb/).

    PUT  /playbooks/index   the backend sends every page; the corpus is replaced
    POST /playbooks/match   which playbook covers this alert
    POST /playbooks/search  the passages of the playbooks nearest a question

What is stored, per playbook (the "Storage (custom) for Search-Confluence"
split):

    embedded  rule_description, true_condition, false_condition — one point
              each — and every runbook step (work_around split into lines),
              one point per step
    metadata  alert name, use case id, version, splunk query / detection SPL,
              steps, target audience, contact — in the payload and the JSON
              file, never embedded

The alert name and use case id are not embedded: they are matched exactly
(router.py), because an alert from the SOAR is one use case and must reach its
own playbook, not the one whose name sounds closest.

Embedding happens when the corpus changes, not per alert. Each point carries a
digest of its text, and a rebuild reuses the stored vector of any text already
in the collection — so a sync where nothing moved embeds nothing, and one
edited page costs that page's changed fields only. The query side is cached per
alert name, and alert names repeat.

Matching, cheapest first, each tier only when the one before cannot answer:

    1. id       the use case id the SOAR sent, or one in the alert name (router.py)
    2. exact    the normalised name or an alias                      (router.py)
    3. fuzzy    a renamed detection, by spelling                     (router.py)
    4. semantic the nearest description or criterion by meaning, if it
                clears PLAYBOOK_SEMANTIC_MIN_SCORE and leads the next playbook
                by PLAYBOOK_SEMANTIC_MIN_MARGIN

Tiers 2 to 4 never return a playbook whose use case id differs from the
alert's (router.compatible). Steps are searchable but never matched on: "check
the firewall logs" is in half the runbooks and identifies none of them.

The playbooks are one JSON file, written to a temporary name and renamed over
the old one, so a crash mid-write leaves the previous index rather than half of
one. Qdrant is written first; a point for a page the file does not list is
ignored when searching, so the two never disagree in what an alert sees.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
from starlette.concurrency import run_in_threadpool

from playbooks.embedder import Embedder, EmbedderUnavailable
from playbooks.extract import Playbook, from_pages
from playbooks.router import AlertRouter, compatible, normalise
from vectordb import Point, VectorDB, VectorDBUnavailable, collection_name, point_id

logger = logging.getLogger("playbooks.store")

# What identifies a playbook by meaning. Not the alert name, though the notebook
# embedded it: the query here is always an alert name, and names are templated.
# Measured with bge-small on the notebook's six playbooks, "Excessive DNS
# Failures" scored 0.804 against "Excessive Failed Logins" on the name alone —
# higher than most genuine renames. Names are what tiers 1 to 3 are for; tier 4
# compares the alert's name with what each playbook says the detection is about.
SEMANTIC_FIELDS = ["rule_description", "true_condition", "false_condition"]
# One point per runbook step: searchable, never matched on.
STEP_FIELD = "step"
SEARCHABLE_FIELDS = SEMANTIC_FIELDS + [STEP_FIELD]

COLLECTION_PREFIX = "playbooks"
FORMAT_VERSION = 2
QUERY_CACHE_SIZE = 2048
# Hits read per semantic match. Enough to find the best field of the leading
# playbook and of its nearest rival in any corpus a SOC writes.
SEARCH_LIMIT = 64
# After Qdrant or the model failed, how long before an alert tries again.
RETRY_AFTER_S = 60


class IndexRefused(ValueError):
    """An index request this service will not apply. Answered with a 409."""


@dataclass
class Record:
    """One embedded passage of one playbook: a field, or one step (1-based)."""

    page_id: str
    field: str
    text: str
    digest: str
    step: int = 0

    @property
    def id(self) -> str:
        return point_id(self.page_id, self.field, str(self.step), self.digest)


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def records_for(playbooks: list[Playbook]) -> list[Record]:
    """
    One record per embedded field and per step, not one per playbook.

    A query about what makes an alert noise should land on false_condition;
    averaging four fields into one vector blurs exactly that signal away
    (notebooks/playbook_rag.ipynb, section 2). Steps are split for the same
    reason: a question about one step should find that step, not the runbook.
    """
    out: list[Record] = []
    for playbook in playbooks:
        for name in SEMANTIC_FIELDS:
            text = (getattr(playbook, name) or "").strip()
            if text:
                out.append(Record(playbook.page_id, name, text, _digest(text)))
        for n, text in enumerate(playbook.steps, start=1):
            out.append(Record(playbook.page_id, STEP_FIELD, text, _digest(text), n))
    return out


@dataclass
class Index:
    playbooks: list[Playbook]
    records: list[Record]
    model: str | None
    built_at: str
    source: str
    pages: int
    skipped: list[dict] = field(default_factory=list)
    # Every record has its point in Qdrant, under this model's collection.
    vectors_ready: bool = False

    def __post_init__(self) -> None:
        self.router: AlertRouter | None = None


class PlaybookStore:
    def __init__(self, path: str | Path, embedder: Embedder | None,
                 vectordb: VectorDB | None = None, *,
                 fuzzy_cutoff: float = 0.85, semantic: bool = True,
                 min_score: float = 0.75, min_margin: float = 0.05,
                 default_use_case: str = "UC-GEN-001") -> None:
        self.path = Path(path)
        self.embedder = embedder if semantic else None
        self.vectordb = (vectordb or VectorDB()) if self.embedder else None
        self.fuzzy_cutoff = fuzzy_cutoff
        self.min_score = min_score
        self.min_margin = min_margin
        self.default_use_case = default_use_case
        self._index: Index | None = None
        self._lock = asyncio.Lock()
        self._query_cache: OrderedDict[tuple[str, str], np.ndarray] = OrderedDict()
        self._failed_at = 0.0
        # Why the semantic tier is not answering, when it is not.
        self.semantic_error: str | None = None if self.embedder else "PLAYBOOK_SEMANTIC is off"

    @property
    def collection(self) -> str | None:
        return collection_name(COLLECTION_PREFIX, self.embedder.name) if self.embedder else None

    # ── Loading and saving ──────────────────────────────────────────────────

    def load(self) -> None:
        """
        The playbooks the last run left, if there are any. Never raises.

        Their vectors are not checked here — that is a network call, and this
        runs before the service answers anything. The first match by meaning,
        or the warm-up task, checks them and embeds whatever Qdrant is missing.
        """
        if not self.path.exists():
            logger.info("no playbook index at %s yet — the backend's first sync writes it", self.path)
            return
        try:
            meta = json.loads(self.path.read_text())
        except (OSError, ValueError) as exc:
            logger.error("playbook index at %s is unreadable, starting empty: %s", self.path, exc)
            return
        if not isinstance(meta, dict) or meta.get("format") != FORMAT_VERSION:
            got = meta.get("format") if isinstance(meta, dict) else type(meta).__name__
            logger.error("playbook index at %s is format %s, not %s — starting empty",
                         self.path, got, FORMAT_VERSION)
            return

        try:
            playbooks = [Playbook.from_dict(p) for p in meta["playbooks"]]
            index = Index(
                playbooks=playbooks, records=records_for(playbooks),
                model=meta.get("model"), built_at=meta["built_at"], source=meta["source"],
                pages=meta["pages"], skipped=meta.get("skipped", []),
            )
        except (KeyError, TypeError, AttributeError) as exc:
            logger.error("playbook index at %s is malformed, starting empty: %r", self.path, exc)
            return
        self._install(index)
        logger.info("loaded %d playbook(s) from %s", len(playbooks), self.path)

    def _save(self, index: Index) -> None:
        meta = {
            "format": FORMAT_VERSION,
            "built_at": index.built_at,
            "source": index.source,
            "pages": index.pages,
            "model": index.model,
            "skipped": index.skipped,
            "playbooks": [p.as_dict() for p in index.playbooks],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        with open(tmp, "w") as handle:
            json.dump(meta, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self.path)

    def _install(self, index: Index) -> None:
        index.router = AlertRouter(index.playbooks, self.fuzzy_cutoff)
        self._index = index

    # ── Building ────────────────────────────────────────────────────────────

    async def replace(self, pages: list[dict[str, Any]], source: str = "confluence",
                      allow_empty: bool = False) -> dict:
        """
        Make the corpus exactly these pages.

        Serialised: two syncs arriving together would otherwise each reuse the
        vectors of an index the other is about to replace.
        """
        async with self._lock:
            started = time.monotonic()
            playbooks, skipped = from_pages(pages, source=source)

            current = self._index
            if not playbooks and current and current.playbooks and not allow_empty:
                # Yesterday's playbooks beat none. A sync that finds nothing is
                # far more often a broken read than an emptied wiki, and every
                # alert answering "no playbook" is indistinguishable from a gap.
                raise IndexRefused(
                    f"{len(pages)} page(s) held no playbook, and replacing "
                    f"{len(current.playbooks)} with none would leave every alert unmatched. "
                    "Send allow_empty=true if the space really is empty."
                )

            records = records_for(playbooks)
            embedded, reused, ready = await self._try_write_vectors(records, playbooks)

            index = Index(
                playbooks=playbooks, records=records,
                model=self.embedder.name if ready else None,
                built_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                source=source, pages=len(pages), skipped=skipped, vectors_ready=ready,
            )
            await run_in_threadpool(self._save, index)
            self._install(index)

            took = int((time.monotonic() - started) * 1000)
            logger.info("indexed %d playbook(s) from %d page(s): %d passage(s) embedded, "
                        "%d reused, %dms", len(playbooks), len(pages), embedded, reused, took)
            return {
                **self.status(),
                "embedded": embedded,
                "reused": reused,
                "took_ms": took,
            }

    async def _try_write_vectors(self, records: list[Record], playbooks: list[Playbook]
                                 ) -> tuple[int, int, bool]:
        """(embedded, reused, ready). A failure turns the semantic tier off and says why."""
        if self.embedder is None:
            return 0, 0, False
        try:
            embedded, reused = await self._write_vectors(records, playbooks)
        except (EmbedderUnavailable, VectorDBUnavailable) as exc:
            self.semantic_error = str(exc)
            self._failed_at = time.monotonic()
            logger.error("semantic tier off: %s", exc)
            return 0, 0, False
        self.semantic_error = None
        return embedded, reused, True

    async def _write_vectors(self, records: list[Record], playbooks: list[Playbook]) -> tuple[int, int]:
        """
        Make the collection hold exactly these records, embedding only text it has not seen.

        Every record gets its own point even when its text is shared — the
        payload says which playbook and which field a hit came from — but a
        shared text is embedded once.
        """
        collection = self.collection
        db = self.vectordb
        known = await run_in_threadpool(db.vectors_for, collection, [r.digest for r in records])

        fresh: dict[str, str] = {}
        for record in records:
            if record.digest not in known:
                fresh.setdefault(record.digest, record.text)
        if fresh:
            encoded = await run_in_threadpool(self.embedder.encode_documents, list(fresh.values()))
            known.update(zip(fresh.keys(), np.asarray(encoded, dtype=np.float32)))

        if records:
            dim = len(next(iter(known.values())))
            await run_in_threadpool(db.ensure, collection, dim)
            by_page = {p.page_id: p for p in playbooks}
            points = [Point(r.id, known[r.digest], self._payload(r, by_page[r.page_id]))
                      for r in records]
            await run_in_threadpool(db.upsert, collection, points)
        removed = await run_in_threadpool(db.keep_only, collection, [r.id for r in records])
        if removed:
            logger.info("removed %d passage(s) no playbook has any more", removed)
        await run_in_threadpool(db.drop_others, COLLECTION_PREFIX, collection)

        reused = sum(1 for r in records if r.digest not in fresh)
        return len(fresh), reused

    @staticmethod
    def _payload(record: Record, playbook: Playbook) -> dict[str, Any]:
        # Metadata rides along so a hit can be read without the JSON file:
        # whoever inspects the collection sees which detection a passage is from.
        return {
            "page_id": record.page_id, "field": record.field, "step": record.step,
            "digest": record.digest, "text": record.text,
            "alert_name": playbook.alert_name, "use_case_id": playbook.use_case_id,
            "version": playbook.version,
        }

    async def ensure_vectors(self) -> bool:
        """
        The loaded index's vectors, checked and completed: whatever Qdrant is
        missing (a fresh Qdrant volume, a new model) is embedded from the
        playbooks on file. Called at startup and before a search needs them.
        """
        index = self._index
        if index is None or self.embedder is None:
            return False
        if index.vectors_ready:
            return True
        if self._failed_at and time.monotonic() - self._failed_at < RETRY_AFTER_S:
            return False
        async with self._lock:
            if self._index is not index:
                return self._index is not None and self._index.vectors_ready
            if index.vectors_ready:
                return True
            embedded, _reused, ready = await self._try_write_vectors(index.records, index.playbooks)
            if ready:
                index.vectors_ready = True
                index.model = self.embedder.name
                if embedded:
                    logger.info("re-embedded %d passage(s) Qdrant did not have", embedded)
            return ready

    # ── Matching ────────────────────────────────────────────────────────────

    async def match(self, alert_name: str, use_case_id: str = "") -> dict:
        index = self._index  # one snapshot for the whole answer, whatever a rebuild does meanwhile
        if index is None:
            return {
                "matched": False, "how": "unindexed", "playbook": None,
                "note": (
                    "No playbooks have been indexed yet, so none was searched for. This is "
                    "not the same as no playbook covering the alert — the backend has not "
                    "completed a Confluence sync since this index was created."
                ),
                "corpus": self._corpus(None),
            }

        result = index.router.match(alert_name, use_case_id)
        if not result["matched"] and result["how"] == "miss":
            uc = AlertRouter.use_case_for(alert_name, use_case_id)
            semantic = await self._semantic(index, alert_name, uc)
            if semantic.get("matched"):
                result = semantic
            elif semantic.get("note"):
                result["note"] = f"{result['note']} {semantic['note']}"
        if not result["matched"]:
            result = self._fallback(index, result)
        result["corpus"] = self._corpus(index)
        return result

    def _fallback(self, index: Index, missed: dict) -> dict:
        """
        No playbook matched: the generic triage playbook (Use Case ID
        PLAYBOOK_DEFAULT_USE_CASE) is the reference, labelled as such. The fix
        for an alert that lands here is to link it — the SOAR sends its
        use_case_id, or the playbook lists its name under Aliases — not a
        looser match. No generic playbook indexed: the miss stands.
        """
        why = missed.get("note", "")
        page = index.router.by_id.get(self.default_use_case.strip().upper())
        if page:
            playbook = index.router.by_page[page]
            return {
                "matched": True, "how": "default",
                "matched_name": playbook.alert_name, "playbook": playbook.as_dict(),
                "note": (
                    f"No playbook covers this alert, so the generic triage playbook "
                    f"{playbook.alert_name!r} is the reference. {why}"
                ).strip(),
            }
        return missed

    async def _query_vector(self, text: str) -> np.ndarray:
        key = (self.embedder.name, normalise(text))
        query = self._query_cache.get(key)
        if query is None:
            query = await run_in_threadpool(self.embedder.encode_query, text)
            self._query_cache[key] = query
            if len(self._query_cache) > QUERY_CACHE_SIZE:
                self._query_cache.popitem(last=False)
        else:
            self._query_cache.move_to_end(key)
        return query

    async def _hits(self, index: Index, text: str, limit: int, must: dict) -> tuple[list, str | None]:
        """Qdrant hits for this text, or why there are none."""
        if self.embedder is None:
            return [], self.semantic_error or "PLAYBOOK_SEMANTIC is off"
        if not await self.ensure_vectors() or self._index is not index:
            return [], self.semantic_error or "the index has no embeddings"
        try:
            query = await self._query_vector(text)
            hits = await run_in_threadpool(self.vectordb.search, self.collection, query, limit, must)
        except (EmbedderUnavailable, VectorDBUnavailable) as exc:
            self.semantic_error = str(exc)
            self._failed_at = time.monotonic()
            index.vectors_ready = False
            return [], str(exc)
        # A point for a page this index does not list is from a sync in progress.
        return [h for h in hits if h.payload.get("page_id") in index.router.by_page], None

    async def _semantic(self, index: Index, alert_name: str, use_case_id: str = "") -> dict:
        hits, why = await self._hits(index, alert_name, SEARCH_LIMIT, {"field": SEMANTIC_FIELDS})
        if why:
            return {"note": f"Matching by meaning was not tried: {why}."}

        # Each playbook scores by its best field; the runner-up is the best
        # *other* playbook, so two fields of one page do not count as a rival.
        # A playbook of another use case is not a candidate at all.
        by_page: dict[str, tuple[float, Any]] = {}
        for hit in hits:
            page = hit.payload["page_id"]
            if not compatible(index.router.by_page[page], use_case_id):
                continue
            if page not in by_page or hit.score > by_page[page][0]:
                by_page[page] = (hit.score, hit)
        if not by_page:
            return {"note": "Nothing indexed by meaning could be compared with it."}
        ranked = sorted(by_page.values(), key=lambda pair: pair[0], reverse=True)
        score, best = ranked[0]
        runner_up = ranked[1][0] if len(ranked) > 1 else -1.0
        playbook = index.router.by_page[best.payload["page_id"]]
        matched_field = best.payload["field"]

        if score < self.min_score:
            return {"note": (
                f"Nearest by meaning was {playbook.alert_name!r} (its {matched_field}, "
                f"{score:.2f}), below the {self.min_score} needed to call it a match."
            )}
        if score - runner_up < self.min_margin:
            # Close to two playbooks at once is not knowing which one. Unrelated
            # names sit in a crowd of near-equal scores; genuine renames stand out.
            second = index.router.by_page[ranked[1][1].payload["page_id"]]
            return {"note": (
                f"Nearest by meaning were {playbook.alert_name!r} ({score:.2f}) and "
                f"{second.alert_name!r} ({runner_up:.2f}), too close together to choose "
                f"between (a lead of {self.min_margin} is needed)."
            )}

        logger.info("semantic match %r -> %r via %s (%.3f)",
                    alert_name, playbook.alert_name, matched_field, score)
        return {
            "matched": True, "how": "semantic",
            "matched_name": playbook.alert_name,
            "matched_field": matched_field,
            "similarity": round(score, 3),
            "playbook": playbook.as_dict(),
        }

    # ── Searching passages ──────────────────────────────────────────────────

    async def search(self, query: str, k: int = 5, page_id: str | None = None,
                     fields: list[str] | None = None) -> dict:
        """
        The playbook passages nearest a question, best first, each with where it is from.

        Retrieval, not matching: no threshold, and a step can be a hit. What
        comes back is quoted from a playbook, so whoever reads it can cite it.
        """
        index = self._index
        if index is None:
            return {"hits": [], "note": "No playbooks have been indexed yet.",
                    "corpus": self._corpus(None)}
        must: dict[str, Any] = {"field": [f for f in (fields or SEARCHABLE_FIELDS)
                                          if f in SEARCHABLE_FIELDS] or SEARCHABLE_FIELDS}
        if page_id:
            must["page_id"] = page_id
        hits, why = await self._hits(index, query, k, must)
        out = []
        for hit in hits[:k]:
            playbook = index.router.by_page[hit.payload["page_id"]]
            out.append({
                "page_id": playbook.page_id,
                "alert_name": playbook.alert_name,
                "use_case_id": playbook.use_case_id,
                "version": playbook.version,
                "url": playbook.url,
                "field": hit.payload["field"],
                "step": hit.payload.get("step") or None,
                "text": hit.payload.get("text", ""),
                "score": round(hit.score, 3),
            })
        result: dict[str, Any] = {"hits": out, "corpus": self._corpus(index)}
        if why:
            result["note"] = f"Search by meaning is not available: {why}."
        return result

    # ── Reporting ───────────────────────────────────────────────────────────

    def _corpus(self, index: Index | None) -> dict:
        return {
            "playbooks": len(index.playbooks) if index else 0,
            "source": index.source if index else None,
            "built_at": index.built_at if index else None,
        }

    def status(self) -> dict:
        index = self._index
        ready = bool(index and index.vectors_ready)
        return {
            "indexed": index is not None,
            **self._corpus(index),
            "pages": index.pages if index else 0,
            "skipped": index.skipped if index else [],
            "conflicts": index.router.conflicts if index else [],
            "names": len(index.router.table) if index else 0,
            "semantic": {
                "enabled": self.embedder is not None,
                "model": self.embedder.name if self.embedder else None,
                "embedded_fields": sum(1 for r in index.records if r.field != STEP_FIELD) if ready else 0,
                "embedded_steps": sum(1 for r in index.records if r.field == STEP_FIELD) if ready else 0,
                "min_score": self.min_score,
                "min_margin": self.min_margin,
                "error": self.semantic_error,
            },
            "vectordb": {
                "where": self.vectordb.where if self.vectordb else None,
                "collection": self.collection,
                "ready": ready,
            },
        }


_store: PlaybookStore | None = None


def get_store() -> PlaybookStore:
    global _store
    if _store is None:
        from core.config import settings
        from playbooks.embedder import SentenceTransformerEmbedder

        path = Path(settings.PLAYBOOK_INDEX_PATH)
        if path.suffix == ".npz":
            # An .env from before the vectors moved to Qdrant. The old file is
            # left where it is; the next sync writes the JSON beside it.
            path = path.with_suffix(".json")
            logger.warning("PLAYBOOK_INDEX_PATH names the old .npz index; using %s", path)
        _store = PlaybookStore(
            path,
            SentenceTransformerEmbedder(settings.EMBED_MODEL, settings.EMBED_CACHE_DIR,
                                        settings.EMBED_QUERY_PREFIX),
            VectorDB(url=settings.QDRANT_URL, api_key=settings.QDRANT_API_KEY,
                     path=settings.QDRANT_PATH, timeout=settings.QDRANT_TIMEOUT_S),
            fuzzy_cutoff=settings.PLAYBOOK_FUZZY_CUTOFF,
            semantic=settings.PLAYBOOK_SEMANTIC,
            min_score=settings.PLAYBOOK_SEMANTIC_MIN_SCORE,
            min_margin=settings.PLAYBOOK_SEMANTIC_MIN_MARGIN,
            default_use_case=settings.PLAYBOOK_DEFAULT_USE_CASE,
        )
    return _store


def set_store(store: PlaybookStore | None) -> None:
    """For tests: swap in a store with a stand-in embedder and a temporary path."""
    global _store
    _store = store
