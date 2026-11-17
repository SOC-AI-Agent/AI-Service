"""
The playbook routes. The backend is the only caller, behind the bearer token.

    PUT  /playbooks/index   {source, pages: [{id, title, version, url, body}]}
    POST /playbooks/match   {alert_name}
    GET  /playbooks         what is indexed, and whether matching by meaning works
"""

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
    `unindexed` means nothing has been indexed yet.
    """
    return await get_store().match(body.alert_name.strip(), body.use_case_id or "")


@router.get("", summary="What is indexed")
async def status() -> dict:
    return get_store().status()
