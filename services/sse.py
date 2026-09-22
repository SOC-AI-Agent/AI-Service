import json
from typing import Any, Iterable

# Checked in order. The first key present and non-empty wins for that event.
DELTA_PATHS: tuple[tuple[str, ...], ...] = (
    ("choices", 0, "delta", "content"),
    ("choices", 0, "message", "content"),
    ("choices", 0, "text"),
    ("message", "content"),
    ("delta", "content"),
    ("delta",),
    ("response",),
    ("token",),
    ("chunk",),
    ("text",),
    ("content",),
)

# A payload carrying one of these is a finished answer, not a fragment.
COMPLETE_KEYS = ("output", "answer", "result", "final", "full_text")

# Sentinels a server may send to say the stream is over.
DONE_SENTINELS = {"[DONE]", "DONE", "[done]"}

# Metadata worth keeping if the stream happens to carry it. Mapped to the names
# the core backend's contract uses.
META_KEYS = {
    "run_id": "run_id",
    "id": "run_id",
    "agent": "agent",
    "agent_name": "agent",
    "model": "model",
    "status": "status",
    "error": "error",
    "steps_used": "steps_used",
    "steps": "steps_used",
    "tools_available": "tools_available",
    "prompt_eval_count": "tokens_prompt",
    "tokens_prompt": "tokens_prompt",
    "eval_count": "tokens_eval",
    "tokens_eval": "tokens_eval",
    "total_duration_ms": "total_duration_ms",
    "duration_ms": "total_duration_ms",
}


def _dig(payload: Any, path: tuple) -> Any:
    for key in path:
        if isinstance(key, int):
            if not isinstance(payload, list) or len(payload) <= key:
                return None
            payload = payload[key]
        else:
            if not isinstance(payload, dict) or key not in payload:
                return None
            payload = payload[key]
    return payload


def text_from(payload: Any) -> tuple[str, bool]:
    """
    Pull the text out of one decoded event.

    Returns (text, is_complete). `is_complete` means the text is a whole answer
    and should replace what came before rather than be appended to it.
    """
    if isinstance(payload, str):
        return payload, False
    if not isinstance(payload, dict):
        return "", False

    for key in COMPLETE_KEYS:
        if key not in payload:
            continue
        value = payload[key]
        if not isinstance(value, str):
            continue
        # Complete because the event carries the answer field, not because the
        # answer is non-empty. A failed run's final event has output="" beside
        # status="failed" and the reason it failed; treating that as "no text
        # here, keep going" left the earlier status="started" in place and threw
        # the reason away, so a 300s Ollama timeout surfaced as a complaint
        # about the stream being unparseable.
        return value, True

    for path in DELTA_PATHS:
        value = _dig(payload, path)
        if isinstance(value, str) and value:
            return value, False

    return "", False


def meta_from(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    found: dict[str, Any] = {}
    for key, mapped in META_KEYS.items():
        if key in payload and payload[key] not in (None, ""):
            # Within one payload, the first spelling wins — a server that sends
            # both `duration_ms` and `total_duration_ms` means the same number.
            found.setdefault(mapped, payload[key])
    return found


class StreamReader:
    """
    Feed it raw lines; ask it for the answer.

    Accepts both SSE framing (`data:` prefixed, blank-line separated) and bare
    newline-delimited JSON, because "SSE" in practice means either.
    """

    def __init__(self) -> None:
        self.pieces: list[str] = []
        self.complete: str | None = None
        self.meta: dict[str, Any] = {}
        self.events = 0
        self.done = False
        self._data_lines: list[str] = []

    def feed_line(self, line: str) -> None:
        stripped = line.rstrip("\r")

        if not stripped.strip():
            # Blank line closes an SSE event.
            self._flush()
            return

        if stripped.startswith(":"):
            # A comment / keep-alive ping.
            return

        if stripped.startswith("data:"):
            self._data_lines.append(stripped[5:].lstrip())
            return

        if ":" in stripped and stripped.split(":", 1)[0] in ("event", "id", "retry"):
            # Field we do not act on. `event:` names are server-specific, and
            # the payload is what carries the answer either way.
            return

        # No SSE framing — treat the line as a payload of its own (NDJSON).
        self._data_lines.append(stripped)
        self._flush()

    def _flush(self) -> None:
        if not self._data_lines:
            return
        raw = "\n".join(self._data_lines)
        self._data_lines = []

        if raw.strip() in DONE_SENTINELS:
            self.done = True
            return

        self.events += 1
        try:
            payload: Any = json.loads(raw)
        except (ValueError, TypeError):
            payload = raw

        text, is_complete = text_from(payload)
        event_meta = meta_from(payload)

        if is_complete:
            # The event carrying the finished answer also carries the numbers
            # that describe the whole run, and those outrank anything a progress
            # event said. Without this a per-step `duration_ms` — the time for
            # one tool call — wins over the run's own, because it arrived first.
            self.complete = text
            self.meta.update(event_meta)
        else:
            if text:
                self.pieces.append(text)
            # Progress events only fill gaps, never overwrite.
            for key, value in event_meta.items():
                self.meta.setdefault(key, value)

        if isinstance(payload, dict) and payload.get("done") is True:
            self.done = True

    def close(self) -> None:
        self._flush()

    @property
    def output(self) -> str:
        # A complete answer beats the accumulated deltas: a server that streams
        # tokens and then repeats the finished text must not produce it twice.
        if self.complete is not None:
            return self.complete
        return "".join(self.pieces)


def read_lines(lines: Iterable[str]) -> StreamReader:
    """Convenience for tests and for a body already in memory."""
    reader = StreamReader()
    for line in lines:
        reader.feed_line(line)
    reader.close()
    return reader
