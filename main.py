import logging

from fastapi import FastAPI, HTTPException

from core.config import settings
from schemas.analysis import AnalyzeRequest, AnalyzeResponse, ProbeResponse
from services import llm_client

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("soc-ai")

app = FastAPI(
    title=settings.APP_NAME,
    version="3.1.0",
    description="Door to the LLM server. Streams the agent's answer back to the core backend.",
    docs_url="/docs",
    redoc_url="/redoc",
)

# No CORS middleware: nothing in a browser calls this service. Only the core
# backend does, from inside soc-network.


@app.post("/analyze", response_model=AnalyzeResponse, tags=["Analysis"])
async def analyze(body: AnalyzeRequest) -> AnalyzeResponse:
    try:
        result = await llm_client.run_agent(body.rendered_prompt, body.agent, body.model)
    except llm_client.LLMServerError as exc:
        # The status the backend sees decides whether it retries, so a timeout
        # and a missing agent must not look the same.
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    return AnalyzeResponse(**result)


@app.post("/llm-probe", response_model=ProbeResponse, tags=["Diagnostics"])
async def llm_probe(body: AnalyzeRequest) -> ProbeResponse:
    """
    Run the same call as /analyze and show the wiring.

    Returns the request that was sent and the raw stream lines that came back,
    so a new or changed LLM server can be checked against this client without
    putting an alert through the queue first. If the answer is empty but events
    arrived, the raw lines say which key the text was under.
    """
    agent = body.agent or settings.LLM_DEFAULT_AGENT
    url, request_body, headers = llm_client.build_request(body.rendered_prompt, agent, body.model)
    sent = {
        "url": url,
        "body": request_body,
        # Never echo the Authorization header back.
        "headers": {k: v for k, v in headers.items() if k.lower() != "authorization"},
        "stream_mode": settings.LLM_STREAM_MODE,
    }

    try:
        result = await llm_client.run_agent(
            body.rendered_prompt, body.agent, body.model, collect_events=True
        )
    except llm_client.LLMServerError as exc:
        return ProbeResponse(ok=False, request=sent, error=str(exc))

    return ProbeResponse(
        ok=True,
        request=sent,
        stream_events=result.get("stream_events", 0),
        raw_events=result.get("raw_events", [])[:200],
        parsed=AnalyzeResponse(**result),
    )


@app.get("/health", tags=["System"])
async def health() -> dict:
    upstream = await llm_client.health()
    return {
        "status": "ok",
        "app": settings.APP_NAME,
        "llm_server": bool(upstream),
        "llm_server_url": settings.LLM_SERVER_URL,
        "default_agent": settings.LLM_DEFAULT_AGENT,
        "stream_mode": settings.LLM_STREAM_MODE,
        "upstream": upstream,
    }
