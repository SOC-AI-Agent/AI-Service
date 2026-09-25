"""
Text -> vectors, in this process.

Every method here blocks: loading the model reads a few hundred MB, and an
encode is CPU work that holds the thread for as long as it takes. Callers run
them through `run_in_threadpool`, never on the event loop, or /analyze — which
is waiting on a stream from the agent platform — stops being read until the
encode finishes.

Not asked of the agent platform. Agent-Server's llama.cpp only ever answers a
chat completion over `/v1/chat/completions`, reachable only through
Agent-Server's own `/v1/agents/{slug}/chat` at that — there is no shared
inference engine here to ask for an embedding instead.
"""

from __future__ import annotations

import logging
import threading
from typing import Protocol

import numpy as np

logger = logging.getLogger("playbooks.embedder")


class EmbedderUnavailable(RuntimeError):
    """The model could not be loaded. Tiers 1 and 2 carry on without it."""


class Embedder(Protocol):
    name: str

    def encode_documents(self, texts: list[str]) -> np.ndarray:
        """(len(texts), dim) float32, each row unit length."""

    def encode_query(self, text: str) -> np.ndarray:
        """(dim,) float32, unit length."""


class SentenceTransformerEmbedder:
    """
    A sentence-transformers model, loaded on first use.

    One lock around load and encode. The model already uses every core for one
    batch, so two encodes at once would only take turns less efficiently — and
    the index rebuild and a match should not both be loading the weights.
    """

    def __init__(self, model: str, cache_dir: str | None = None, query_prefix: str = "") -> None:
        self.name = model
        self.cache_dir = cache_dir or None
        self.query_prefix = query_prefix
        self._model = None
        self._lock = threading.Lock()

    def _loaded(self):
        if self._model is None:
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError as exc:
                raise EmbedderUnavailable(
                    "sentence-transformers is not installed (requirements-embed.txt)"
                ) from exc
            try:
                self._model = SentenceTransformer(self.name, device="cpu",
                                                  cache_folder=self.cache_dir)
            except Exception as exc:  # noqa: BLE001 — any load failure means the same thing
                raise EmbedderUnavailable(
                    f"could not load embedding model {self.name!r} from {self.cache_dir}: "
                    f"{type(exc).__name__}: {exc}"
                ) from exc
            logger.info("embedding model %s loaded", self.name)
        return self._model

    def warm(self) -> None:
        with self._lock:
            self._loaded()

    def _encode(self, texts: list[str]) -> np.ndarray:
        with self._lock:
            model = self._loaded()
            try:
                vectors = model.encode(
                    texts, batch_size=32, convert_to_numpy=True,
                    normalize_embeddings=True, show_progress_bar=False,
                )
            except Exception as exc:  # noqa: BLE001 — a failed encode is the same outcome as no model
                raise EmbedderUnavailable(f"encoding failed: {type(exc).__name__}: {exc}") from exc
        return np.asarray(vectors, dtype=np.float32)

    def encode_documents(self, texts: list[str]) -> np.ndarray:
        return self._encode(texts)

    def encode_query(self, text: str) -> np.ndarray:
        return self._encode([self.query_prefix + text])[0]
