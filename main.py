import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from starlette.concurrency import run_in_threadpool

from core.auth import BearerAuthMiddleware
from core.config import settings
from playbooks.embedder import EmbedderUnavailable
from playbooks.routes import router as playbooks_router
from playbooks.store import get_store
from schemas.analysis import AnalyzeRequest, AnalyzeResponse, ProbeResponse
from services import llm_client

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("soc-ai")

# Refused at boot, like the connectors. Without a token every route here —
# running agents, rewriting the playbook index — is open to anything that can
# reach the port.
if not settings.AI_SERVICE_AUTH_TOKEN:
    raise RuntimeError("refusing to start, unset required env: AI_SERVICE_AUTH_TOKEN")


async def _warm(store) -> None:
    """Load the embedding model now, off the event loop, so the first alert
    that needs matching by meaning does not pay for it inside its timeout."""
    if store.embedder is None or not hasattr(store.embedder, "warm"):
        return
    try:
        await run_in_threadpool(store.embedder.warm)
    except EmbedderUnavailable as exc:
        store.semantic_error = str(exc)
        logger.error("matching by meaning is off: %s", exc)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    store = get_store()
    store.load()
    warming = asyncio.create_task(_warm(store))
    try:
        yield
    finally:
        warming.cancel()


app = FastAPI(
    title=settings.APP_NAME,
    version="3.2.0",
    description=(
        "The backend's door to the LLM server, and the playbook index: the backend sends "
        "the Confluence pages, this service extracts, embeds and matches them."
    ),
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

# No CORS middleware: nothing in a browser calls this service. Only the core
# backend does, from inside soc-network, with the bearer token.
app.add_middleware(BearerAuthMiddleware, token=settings.AI_SERVICE_AUTH_TOKEN)
app.include_router(playbooks_router)


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
        "playbooks": get_store().status()["playbooks"],
    }
