"""
This service's client against Agent-Server's (DALL-M) actual shapes.

LLM_RUN_PATH, LLM_INPUT_FIELD and the sse.META_KEYS mappings only mean
anything if they line up with what Agent-Server's /v1/agents/{slug}/chat
really sends and expects — see ../Agent-Server/server/app/routers/agent_invoke.py
and module/agent/schemas.py. httpx never leaves the process: MockTransport
answers in place of the network, so these run with no server up.
"""

import httpx
import pytest

from core.config import settings
from services import llm_client


class FakeAgentServer:
    """Records requests; `reply` decides what the next one gets back."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.reply: httpx.Response = httpx.Response(500)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.reply


@pytest.fixture
def agent_server(monkeypatch):
    fake = FakeAgentServer()
    real_client = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(fake)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    return fake


def sse_body(*lines: str) -> bytes:
    return ("\n".join(lines) + "\n\n").encode()


# ── The request this service sends ──────────────────────────────────────────

def test_the_request_matches_agent_invoke_router():
    url, body, headers = llm_client.build_request("alert text here", "soc-suggest", None)
    assert url == "http://127.0.0.1:9/v1/agents/soc-suggest/chat"
    # AgentChatRequest: {message: str, stream: bool = True}. No "agent" field —
    # LLM_AGENT_FIELD is empty because the slug is already in the path.
    assert body == {"message": "alert text here"}
    assert "Authorization" not in headers  # no LLM_AUTH_TOKEN — Agent-Server takes none


def test_a_pinned_prompt_version_is_a_different_path():
    # server/app/routers/agent_invoke.py also serves /{slug}/versions/{version}/chat.
    # This service does not pin one — the assertion is that the default path is
    # the unpinned route, not the versioned one.
    url, _, _ = llm_client.build_request("x", "soc-conclude", None)
    assert url.endswith("/v1/agents/soc-conclude/chat")
    assert "/versions/" not in url


# ── Streaming: event: token / event: done, as agent_invoke.py's generate() writes ──

@pytest.mark.asyncio
async def test_a_streamed_answer_is_reassembled_from_token_events(agent_server):
    agent_server.reply = httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        content=sse_body(
            "event: token", 'data: {"content": "Hello"}', "",
            "event: token", 'data: {"content": " world"}', "",
            "event: done",
            'data: {"latency_ms": 250, "prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15}',
        ),
    )
    result = await llm_client.run_agent("alert", agent="soc-suggest")
    assert result["response"] == "Hello world"
    assert result["tokens_prompt"] == 12       # from the done event's prompt_tokens
    assert result["tokens_eval"] == 3          # from completion_tokens
    assert result["total_duration_ms"] == 250  # from latency_ms
    assert result["agent"] == "soc-suggest"    # not in any event — falls back to what was asked
    assert result["status"] == "completed"     # Agent-Server never sends one — the default


@pytest.mark.asyncio
async def test_a_mid_stream_failure_surfaces_the_error_event(agent_server):
    # A real case: get_chat/stream_chat's httpx.HTTPError branch in agent_invoke.py.
    agent_server.reply = httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        content=sse_body("event: error", 'data: {"message": "connection reset by peer"}'),
    )
    with pytest.raises(llm_client.LLMServerError, match="connection reset by peer"):
        await llm_client.run_agent("alert", agent="soc-suggest")


@pytest.mark.asyncio
async def test_tokens_before_a_late_error_are_not_thrown_away(agent_server):
    agent_server.reply = httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        content=sse_body(
            "event: token", 'data: {"content": "Partial answer"}', "",
            "event: error", 'data: {"message": "upstream timed out"}',
        ),
    )
    # What streamed survives — an answer cut short is still an answer, and the
    # backend stores it either way. Only a fully empty stream is a hard failure
    # (the case above).
    result = await llm_client.run_agent("alert", agent="soc-suggest")
    assert result["response"] == "Partial answer"


# ── Non-streaming: the plain AgentChatResponse body ──────────────────────────

@pytest.mark.asyncio
async def test_a_non_streamed_answer_reads_agent_chat_response(agent_server):
    agent_server.reply = httpx.Response(
        200,
        json={
            "agent": "soc-conclude", "prompt_version": 2, "content": "Full answer here",
            "input_tokens": 20, "output_tokens": 8, "total_tokens": 28, "latency_ms": 500,
        },
    )
    result = await llm_client.run_agent("alert", agent="soc-conclude")
    assert result["response"] == "Full answer here"
    assert result["tokens_prompt"] == 20   # from input_tokens
    assert result["tokens_eval"] == 8      # from output_tokens
    assert result["total_duration_ms"] == 500
    assert result["agent"] == "soc-conclude"  # this shape does carry it


# ── Errors agent_invoke.py's _resolve() raises before any generation starts ──

@pytest.mark.asyncio
async def test_an_unknown_agent_is_reported_as_a_setup_problem(agent_server):
    agent_server.reply = httpx.Response(404, json={"detail": "Agent not found"})
    with pytest.raises(llm_client.LLMServerError, match="has no agent 'soc-suggest'"):
        await llm_client.run_agent("alert", agent="soc-suggest")


@pytest.mark.asyncio
async def test_an_inactive_agent_or_prompt_is_reported_from_its_detail(agent_server):
    agent_server.reply = httpx.Response(409, json={"detail": "Agent has no active prompt version"})
    with pytest.raises(llm_client.LLMServerError, match="Agent has no active prompt version"):
        await llm_client.run_agent("alert", agent="soc-suggest")


# ── /health has no /health — it is / ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_health_asks_the_configured_path(agent_server):
    agent_server.reply = httpx.Response(200, json={"status": "ok"})
    await llm_client.health()
    assert agent_server.requests[0].url.path == "/"


def test_the_health_path_setting_defaults_to_root():
    assert settings.LLM_HEALTH_PATH == "/"
