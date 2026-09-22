from pydantic import BaseModel, Field


class AnalyzeRequest(BaseModel):
    # Already rendered by the backend (instruction + raw log). This service does
    # not assemble prompts — keeping that in one place is what makes a prompt
    # reproducible from the job row the backend stores.
    rendered_prompt: str = Field(min_length=1)
    # Which agent handles it. Omitted means LLM_DEFAULT_AGENT, which is how the
    # worker calls it today; naming one lets a playbook route unusual alerts to
    # a different agent without a code change.
    agent: str | None = None
    model: str | None = None


class AnalyzeResponse(BaseModel):
    response: str
    model: str
    total_duration_ms: int = 0
    tokens_prompt: int = 0
    tokens_eval: int = 0

    # ── beyond the original contract ─────────────────────────────────────────
    # Returned when the LLM server reports them; the backend's mapper ignores
    # what it does not know. run_id is how a job row traces back to the run.
    run_id: str | None = None
    agent: str | None = None
    steps_used: int = 0
    tools_available: int = 0
    status: str = "completed"
    error: str | None = None
    stream_events: int = 0


class ProbeResponse(BaseModel):
    """What /llm-probe returns: the wiring, not just the answer."""

    ok: bool
    request: dict
    stream_events: int = 0
    raw_events: list[str] = []
    parsed: AnalyzeResponse | None = None
    error: str | None = None
