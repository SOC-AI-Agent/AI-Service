"""
The playbook index: built from the pages the backend sends, kept on a volume,
answered from memory.

    PUT  /playbooks/index   the backend sends every page; the corpus is replaced
    POST /playbooks/match   which playbook covers this alert

Embedding happens when the corpus changes, not per alert. Each embedded field
carries a digest of its text, and a rebuild reuses the vector of any field whose
text and model are unchanged — so a sync where nothing moved embeds nothing,
and one edited page costs that page's fields only. The query side is cached per
alert name, and alert names repeat.

Matching, cheapest first, each tier only when the one before cannot answer:

    1. exact    the normalised name or an alias          (router.py)
    2. fuzzy    a renamed detection, by spelling         (router.py)
    3. semantic the nearest description or criterion by meaning, if it
                clears PLAYBOOK_SEMANTIC_MIN_SCORE and leads the next playbook
                by PLAYBOOK_SEMANTIC_MIN_MARGIN

The index is one .npz file, written to a temporary name and renamed over the
old one, so a crash mid-write leaves the previous index rather than half of one.
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
from playbooks.router import AlertRouter, normalise

logger = logging.getLogger("playbooks.store")

# What is embedded. Not the alert name, though the notebook embedded it: the
# query here is always an alert name, and names are templated. Measured with
# bge-small on the notebook's six playbooks, "Excessive DNS Failures" scored
# 0.804 against "Excessive Failed Logins" on the name alone — higher than most
# genuine renames. Names are what tiers 1 and 2 are for; tier 3 compares the
# alert's name with what each playbook says the detection is about.
SEMANTIC_FIELDS = ["rule_description", "true_condition", "false_condition"]

FORMAT_VERSION = 1
QUERY_CACHE_SIZE = 2048


class IndexRefused(ValueError):
    """An index request this service will not apply. Answered with a 409."""


@dataclass
class Record:
    """One embedded field of one playbook."""

    page_id: str
    field: str
    text: str
    digest: str


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def records_for(playbooks: list[Playbook]) -> list[Record]:
    """
    One record per embedded field, not one per playbook.

    A query about what makes an alert noise should land on false_condition;
    averaging four fields into one vector blurs exactly that signal away
    (notebooks/playbook_rag.ipynb, section 2).
    """
    out: list[Record] = []
    for playbook in playbooks:
        for name in SEMANTIC_FIELDS:
            text = (getattr(playbook, name) or "").strip()
            if text:
                out.append(Record(playbook.page_id, name, text, _digest(text)))
    return out


@dataclass
class Index:
    playbooks: list[Playbook]
    records: list[Record]
    # (len(records), dim), or None when the semantic tier is off or failed.
    vectors: np.ndarray | None
    model: str | None
    built_at: str
    source: str
    pages: int
    skipped: list[dict] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.router: AlertRouter | None = None


class PlaybookStore:
    def __init__(self, path: str | Path, embedder: Embedder | None, *,
                 fuzzy_cutoff: float = 0.85, semantic: bool = True,
                 min_score: float = 0.75, min_margin: float = 0.05) -> None:
        self.path = Path(path)
        self.embedder = embedder if semantic else None
        self.fuzzy_cutoff = fuzzy_cutoff
        self.min_score = min_score
        self.min_margin = min_margin
        self._index: Index | None = None
        self._lock = asyncio.Lock()
        self._query_cache: OrderedDict[tuple[str, str], np.ndarray] = OrderedDict()
        # Why the semantic tier is not answering, when it is not.
        self.semantic_error: str | None = None if self.embedder else "PLAYBOOK_SEMANTIC is off"

    # ── Loading and saving ──────────────────────────────────────────────────

    def load(self) -> None:
        """The index the last run left, if there is one. Never raises."""
        if not self.path.exists():
            logger.info("no playbook index at %s yet — the backend's first sync writes it", self.path)
            return
        try:
            with np.load(self.path, allow_pickle=False) as data:
                meta = json.loads(str(data["meta"]))
                vectors = data["vectors"]
        except (OSError, ValueError, KeyError) as exc:
            logger.error("playbook index at %s is unreadable, starting empty: %s", self.path, exc)
            return
        if meta.get("format") != FORMAT_VERSION:
            logger.error("playbook index at %s is format %s, not %s — starting empty",
                         self.path, meta.get("format"), FORMAT_VERSION)
            return

        try:
            playbooks = [Playbook.from_dict(p) for p in meta["playbooks"]]
            by_page = {p.page_id: p for p in playbooks}
            records = [
                Record(r["page_id"], r["field"],
                       (getattr(by_page[r["page_id"]], r["field"]) or "").strip(), r["digest"])
                for r in meta["records"]
            ]
            index = Index(
                playbooks=playbooks, records=records,
                vectors=vectors if vectors.size and len(vectors) == len(records) else None,
                model=meta.get("model"), built_at=meta["built_at"], source=meta["source"],
                pages=meta["pages"], skipped=meta.get("skipped", []),
            )
        except (KeyError, TypeError, AttributeError) as exc:
            logger.error("playbook index at %s is malformed, starting empty: %r", self.path, exc)
            return
        self._install(index)
        logger.info("loaded %d playbook(s), %d embedded field(s) from %s",
                    len(playbooks), len(records) if index.vectors is not None else 0, self.path)

    def _save(self, index: Index) -> None:
        meta = {
            "format": FORMAT_VERSION,
            "built_at": index.built_at,
            "source": index.source,
            "pages": index.pages,
            "model": index.model,
            "skipped": index.skipped,
            "playbooks": [p.as_dict() for p in index.playbooks],
            "records": [{"page_id": r.page_id, "field": r.field, "digest": r.digest}
                        for r in index.records],
        }
        vectors = index.vectors if index.vectors is not None else np.zeros((0, 0), np.float32)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        # A file object, not a name: given a name np.savez appends ".npz".
        with open(tmp, "wb") as handle:
            np.savez(handle, vectors=vectors, meta=np.array(json.dumps(meta)))
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
            vectors, embedded, reused = await self._vectors_for(records, current)

            index = Index(
                playbooks=playbooks, records=records, vectors=vectors,
                model=self.embedder.name if vectors is not None else None,
                built_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                source=source, pages=len(pages), skipped=skipped,
            )
            await run_in_threadpool(self._save, index)
            self._install(index)

            took = int((time.monotonic() - started) * 1000)
            logger.info("indexed %d playbook(s) from %d page(s): %d field(s) embedded, %d reused, %dms",
                        len(playbooks), len(pages), embedded, reused, took)
            return {
                **self.status(),
                "embedded": embedded,
                "reused": reused,
                "took_ms": took,
            }

    async def _vectors_for(self, records: list[Record], current: Index | None
                           ) -> tuple[np.ndarray | None, int, int]:
        """Vectors in record order, embedding only text not seen before."""
        if self.embedder is None or not records:
            return None, 0, 0

        known: dict[str, np.ndarray] = {}
        if current and current.vectors is not None and current.model == self.embedder.name:
            known = {r.digest: v for r, v in zip(current.records, current.vectors)}

        fresh: dict[str, str] = {}
        for record in records:
            if record.digest not in known:
                fresh.setdefault(record.digest, record.text)

        if fresh:
            try:
                encoded = await run_in_threadpool(self.embedder.encode_documents, list(fresh.values()))
            except EmbedderUnavailable as exc:
                self.semantic_error = str(exc)
                logger.error("semantic tier off: %s", exc)
                return None, 0, 0
            known.update(zip(fresh.keys(), encoded))
        self.semantic_error = None

        reused = sum(1 for r in records if r.digest not in fresh)
        return np.stack([known[r.digest] for r in records]).astype(np.float32), len(fresh), reused

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
            semantic = await self._semantic(index, alert_name)
            if semantic.get("matched"):
                result = semantic
            elif semantic.get("note"):
                result["note"] = f"{result['note']} {semantic['note']}"
        result["corpus"] = self._corpus(index)
        return result

    async def _semantic(self, index: Index, alert_name: str) -> dict:
        if index.vectors is None or self.embedder is None:
            reason = self.semantic_error or "the index has no embeddings"
            return {"note": f"Matching by meaning was not tried: {reason}."}

        key = (self.embedder.name, normalise(alert_name))
        query = self._query_cache.get(key)
        if query is None:
            try:
                query = await run_in_threadpool(self.embedder.encode_query, alert_name)
            except EmbedderUnavailable as exc:
                self.semantic_error = str(exc)
                return {"note": f"Matching by meaning was not tried: {exc}."}
            self._query_cache[key] = query
            if len(self._query_cache) > QUERY_CACHE_SIZE:
                self._query_cache.popitem(last=False)
        else:
            self._query_cache.move_to_end(key)

        scores = index.vectors @ query
        # Each playbook scores by its best field; the runner-up is the best
        # *other* playbook, so two fields of one page do not count as a rival.
        by_page: dict[str, tuple[float, int]] = {}
        for i, record in enumerate(index.records):
            if record.page_id not in by_page or scores[i] > by_page[record.page_id][0]:
                by_page[record.page_id] = (float(scores[i]), i)
        ranked = sorted(by_page.values(), reverse=True)
        score, best = ranked[0]
        runner_up = ranked[1][0] if len(ranked) > 1 else -1.0
        record = index.records[best]
        playbook = index.router.by_page[record.page_id]

        if score < self.min_score:
            return {"note": (
                f"Nearest by meaning was {playbook.alert_name!r} (its {record.field}, "
                f"{score:.2f}), below the {self.min_score} needed to call it a match."
            )}
        if score - runner_up < self.min_margin:
            # Close to two playbooks at once is not knowing which one. Unrelated
            # names sit in a crowd of near-equal scores; genuine renames stand out.
            second = index.router.by_page[index.records[ranked[1][1]].page_id]
            return {"note": (
                f"Nearest by meaning were {playbook.alert_name!r} ({score:.2f}) and "
                f"{second.alert_name!r} ({runner_up:.2f}), too close together to choose "
                f"between (a lead of {self.min_margin} is needed)."
            )}

        logger.info("semantic match %r -> %r via %s (%.3f)",
                    alert_name, playbook.alert_name, record.field, score)
        return {
            "matched": True, "how": "semantic",
            "matched_name": playbook.alert_name,
            "matched_field": record.field,
            "similarity": round(score, 3),
            "playbook": playbook.as_dict(),
        }

    # ── Reporting ───────────────────────────────────────────────────────────

    def _corpus(self, index: Index | None) -> dict:
        return {
            "playbooks": len(index.playbooks) if index else 0,
            "source": index.source if index else None,
            "built_at": index.built_at if index else None,
        }

    def status(self) -> dict:
        index = self._index
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
                "embedded_fields": len(index.records) if index and index.vectors is not None else 0,
                "min_score": self.min_score,
                "min_margin": self.min_margin,
                "error": self.semantic_error,
            },
        }


_store: PlaybookStore | None = None


def get_store() -> PlaybookStore:
    global _store
    if _store is None:
        from core.config import settings
        from playbooks.embedder import SentenceTransformerEmbedder

        _store = PlaybookStore(
            settings.PLAYBOOK_INDEX_PATH,
            SentenceTransformerEmbedder(settings.EMBED_MODEL, settings.EMBED_CACHE_DIR,
                                        settings.EMBED_QUERY_PREFIX),
            fuzzy_cutoff=settings.PLAYBOOK_FUZZY_CUTOFF,
            semantic=settings.PLAYBOOK_SEMANTIC,
            min_score=settings.PLAYBOOK_SEMANTIC_MIN_SCORE,
            min_margin=settings.PLAYBOOK_SEMANTIC_MIN_MARGIN,
        )
    return _store


def set_store(store: PlaybookStore | None) -> None:
    """For tests: swap in a store with a stand-in embedder and a temporary path."""
    global _store
    _store = store
