"""
The playbook routes. The backend is the only caller, behind the bearer token.

    PUT  /playbooks/index   {source, pages: [{id, title, version, url, body}]}
    POST /playbooks/match   {alert_name, use_case_id?}
    POST /playbooks/search  {query, page_id?, fields?, k?}
    GET  /playbooks         what is indexed, and whether matching by meaning works
"""

from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from core.config import settings
from playbooks.store import IndexRefused, get_store

router = APIRouter(prefix="/playbooks", tags=["Playbooks"])


class PageIn(BaseModel):
    # Confluence ids are strings in its JSON; an int is accepted rather than
    # refusing a whole sync over the type of one id.
    id: str | int
    title: str = ""
    version: int | None = None
    url: str = ""
    # Storage-format XHTML, as Confluence returned it.
    body: str = Field("", max_length=2_000_000)


class IndexRequest(BaseModel):
    source: str = Field("confluence", max_length=64)
    pages: list[PageIn] = Field(max_length=settings.PLAYBOOK_MAX_PAGES)
    # Replacing a non-empty index with nothing is refused unless this is set.
    allow_empty: bool = False


class MatchRequest(BaseModel):
    alert_name: str = Field(min_length=1, max_length=512)
    # The SOC's use case code, when the SOAR sent one. Matched before the name.
    use_case_id: str | None = Field(None, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    # Within one playbook — the one /match returned — or across all of them.
    page_id: str | None = Field(None, max_length=64)
    fields: list[Literal["rule_description", "true_condition", "false_condition", "step"]] | None = None
    k: int = Field(5, ge=1, le=20)


@router.put("/index", summary="Replace the playbook corpus with these pages")
async def index_pages(body: IndexRequest) -> dict:
    """
    Extracts each page's Page Properties table, embeds what changed since the
    last index, and writes the index to the volume. A page with no alert name
    is listed under `skipped`, not indexed.
    """
    pages = [{**page.model_dump(), "id": str(page.id)} for page in body.pages]
    try:
        return await get_store().replace(pages, source=body.source, allow_empty=body.allow_empty)
    except IndexRefused as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/match", summary="Which playbook covers this alert")
async def match(body: MatchRequest) -> dict:
    """
    `how` says how it was found: `id` (the use case id, sent or in the name),
    `exact`, `fuzzy` (by spelling) or `semantic` (by meaning, with the field
    that matched). `miss` is a searched corpus with nothing close enough;
    `use_case_mismatch` is a name that reached a playbook of a different use
    case, refused; `unindexed` means nothing has been indexed yet.
    """
    return await get_store().match(body.alert_name.strip(), body.use_case_id or "")


@router.post("/search", summary="The playbook passages nearest a question")
async def search(body: SearchRequest) -> dict:
    """
    Retrieval over the vector store: descriptions, criteria and runbook steps,
    each hit with its playbook, field, step number and score. No threshold —
    this answers "what do the playbooks say about X", not "which playbook is
    this alert".
    """
    return await get_store().search(body.query.strip(), k=body.k, page_id=body.page_id,
                                    fields=list(body.fields) if body.fields else None)


@router.get("", summary="What is indexed")
async def status() -> dict:
    return get_store().status()
