import json
import logging
import time
from typing import Any, Optional

import httpx

from core.config import settings
from services import sse

logger = logging.getLogger("llm-client")

STREAM_CONTENT_TYPES = ("text/event-stream", "application/x-ndjson", "application/stream+json")


class LLMServerError(RuntimeError):
    """The LLM server could not be reached, or refused the run."""

    def __init__(self, message: str, status_code: int = 502) -> None:
        super().__init__(message)
        self.status_code = status_code


def build_request(rendered_prompt: str, agent: str, model: Optional[str]) -> tuple[str, dict, dict]:
    """(url, body, headers) — kept separate so the probe endpoint can show it."""
    body: dict[str, Any] = {settings.LLM_INPUT_FIELD: rendered_prompt}
    if settings.LLM_AGENT_FIELD:
        body[settings.LLM_AGENT_FIELD] = agent
    if model:
        body["model"] = model
    body.update(settings.extra_body)

    headers = {"Accept": "text/event-stream, application/json", **settings.extra_headers}
    if settings.LLM_AUTH_TOKEN:
        headers["Authorization"] = f"Bearer {settings.LLM_AUTH_TOKEN}"

    return settings.run_url(agent), body, headers


def _wants_stream(content_type: str) -> bool:
    if settings.LLM_STREAM_MODE == "always":
        return True
    if settings.LLM_STREAM_MODE == "never":
        return False
    return any(ct in content_type for ct in STREAM_CONTENT_TYPES)


async def _error_from(resp: httpx.Response, agent: str) -> LLMServerError:
    body = (await resp.aread()).decode(errors="replace")[:400]
    if resp.status_code == 404:
        # Worth its own message: the usual cause is that nobody has created the
        # agent yet, which is a setup step, not an outage.
        return LLMServerError(
            f"the LLM server has no agent {agent!r} at {settings.run_url(agent)}",
            status_code=502,
        )
    detail = body
    try:
        parsed = json.loads(body)
        if isinstance(parsed, dict):
            detail = parsed.get("detail") or parsed.get("error") or body
    except ValueError:
        pass
    return LLMServerError(f"LLM server returned {resp.status_code}: {detail}")


async def run_agent(
    rendered_prompt: str,
    agent: Optional[str] = None,
    model: Optional[str] = None,
    collect_events: bool = False,
) -> dict[str, Any]:
    """
    Run one alert through an agent.

    `collect_events` keeps the raw lines for the probe endpoint. It is off for
    real traffic: a long stream held twice in memory buys nothing in production.
    """
    agent_ref = agent or settings.LLM_DEFAULT_AGENT
    url, body, headers = build_request(rendered_prompt, agent_ref, model)
    started = time.monotonic()
    raw_events: list[str] = []

    logger.info("running agent %s at %s", agent_ref, url)

    try:
        async with httpx.AsyncClient(timeout=settings.LLM_TIMEOUT) as client:
            async with client.stream("POST", url, json=body, headers=headers) as resp:
                if resp.status_code >= 400:
                    raise await _error_from(resp, agent_ref)

                content_type = resp.headers.get("content-type", "")

                if not _wants_stream(content_type):
                    payload = json.loads((await resp.aread()).decode())
                    return _from_object(payload, started, agent_ref, raw_events)

                reader = sse.StreamReader()
                async for line in resp.aiter_lines():
                    if collect_events:
                        raw_events.append(line)
                    reader.feed_line(line)
                    if reader.done:
                        break
                reader.close()

    except LLMServerError:
        raise
    except httpx.TimeoutException as exc:
        raise LLMServerError(
            f"agent run exceeded LLM_TIMEOUT ({settings.LLM_TIMEOUT}s)", status_code=504
        ) from exc
    except httpx.HTTPError as exc:
        raise LLMServerError(
            f"cannot reach the LLM server at {settings.LLM_SERVER_URL}: {exc}", status_code=503
        ) from exc
    except (ValueError, TypeError) as exc:
        raise LLMServerError(f"could not read the LLM server's reply: {exc}") from exc

    result = _shape(reader.meta, reader.output, started, agent_ref)
    result["stream_events"] = reader.events
    if collect_events:
        result["raw_events"] = raw_events

    if result["status"] == "failed":
        raise LLMServerError(f"agent run failed: {result.get('error') or 'unknown error'}")
    if not result["response"].strip():
        # An empty answer is a failure the backend should see as one, not a
        # COMPLETED job holding an empty string.
        raise LLMServerError(
            f"the LLM server streamed {reader.events} events but no answer text"
        )
    return result


def _from_object(payload: Any, started: float, agent: str, raw: list) -> dict[str, Any]:
    """A plain JSON reply — the non-streaming case."""
    text, _ = sse.text_from(payload)
    result = _shape(sse.meta_from(payload), text, started, agent)
    result["stream_events"] = 0
    if raw is not None:
        result["raw_events"] = []
    if result["status"] == "failed":
        raise LLMServerError(f"agent run failed: {result.get('error') or 'unknown error'}")
    if not result["response"].strip():
        raise LLMServerError("the LLM server returned no answer text")
    return result


def _shape(meta: dict, text: str, started: float, agent: str) -> dict[str, Any]:
    """Whatever came back -> the shape ../backend/src/modules/ai/ai.service.ts reads."""
    elapsed = int((time.monotonic() - started) * 1000)
    status = str(meta.get("status") or "completed").lower()
    return {
        "response": text,
        "model": str(meta.get("model") or ""),
        # Their duration if they reported one, ours otherwise — the backend
        # stores this on the job row either way.
        "total_duration_ms": int(meta.get("total_duration_ms") or elapsed),
        "tokens_prompt": int(meta.get("tokens_prompt") or 0),
        "tokens_eval": int(meta.get("tokens_eval") or 0),
        "run_id": meta.get("run_id"),
        "agent": str(meta.get("agent") or agent),
        "steps_used": int(meta.get("steps_used") or 0),
        "tools_available": int(meta.get("tools_available") or 0),
        "status": status,
        "error": meta.get("error"),
    }


async def health() -> dict[str, Any]:
    url = settings.LLM_SERVER_URL.rstrip("/") + "/health"
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            resp = await client.get(url, headers=settings.extra_headers)
            resp.raise_for_status()
            return resp.json()
        except (httpx.HTTPError, ValueError):
            return {}
