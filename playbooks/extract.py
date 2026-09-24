"""
Confluence pages -> playbook records.

Playbooks are authored with a Page Properties macro, which stores each field as
a row in a two-column table. The backend fetches the pages (through the
Confluence connector, which holds the Confluence credentials) and sends each
one here as {id, title, version, url, body}, body being the storage-format
XHTML exactly as Confluence returned it.

Ported from MCP-Server/Confluence (confluence_api.rows_from_storage and
playbooks.from_rows) when matching moved to this service, with one change on
the way: a cell is reduced to its text. The connector kept a cell's XHTML as it
came, so an alert name the editor had wrapped in <p> normalised to
"p excessive failed logins p" and missed its exact match, and the criteria
reached the model with their tags and entities still in them.
"""

from __future__ import annotations

import html
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("playbooks.extract")

# Identify which playbook applies. The split was settled in
# MCP-Server/notebooks/playbook_rag.ipynb; store.SEMANTIC_FIELDS says which of
# them are embedded, and why the name is not.
MATCHED_FIELDS = ["alert_name", "rule_description", "true_condition", "false_condition"]
# What the analyst then acts on. Never used for matching: embedding a contact
# would match on people's names and a version number carries no meaning at all.
METADATA_FIELDS = [
    "use_case_category", "version", "splunk_query", "work_around",
    "target_audience", "contact",
]

# Confluence label -> our field. A label that is not here is ignored rather
# than guessed at: a page that grows a new row should not silently change what
# the matcher sees.
FIELD_LABELS = {
    "alert name": "alert_name",
    "alert": "alert_name",
    "use case": "alert_name",
    "use case / alert name": "alert_name",
    "use case name": "alert_name",
    "use case category": "use_case_category",
    "category": "use_case_category",
    "aliases": "aliases",
    "rule description": "rule_description",
    "description": "rule_description",
    "true condition": "true_condition",
    "false condition": "false_condition",
    "version": "version",
    "splunk query": "splunk_query",
    "work around": "work_around",
    "workaround": "work_around",
    "step to work": "work_around",
    "target audience": "target_audience",
    "contact": "contact",
    "contact info": "contact",
}


@dataclass
class Playbook:
    page_id: str
    # Confluence keeps the use case name and the alert name in one column, so
    # this is both: the routing key, and what an analyst calls the detection.
    alert_name: str
    use_case_category: str = ""
    rule_description: str = ""
    true_condition: str = ""
    false_condition: str = ""
    # The same detection under other names. FortiSOAR and Confluence do not
    # always agree, and detections get renamed without the playbook following.
    aliases: list[str] = field(default_factory=list)
    version: str = ""
    # Parameters for a search, never raw SPL. The backend builds the search
    # from these (Backend: modules/enrichment/spl.ts); SPL copied out of a wiki
    # anyone in the space can edit is not something to run.
    splunk_query: dict | None = None
    work_around: str = ""
    target_audience: str = ""
    contact: str = ""
    url: str = ""
    source: str = "confluence"

    def as_dict(self) -> dict[str, Any]:
        return {
            "page_id": self.page_id,
            "alert_name": self.alert_name,
            "aliases": self.aliases,
            "url": self.url,
            "source": self.source,
            **{f: getattr(self, f) for f in MATCHED_FIELDS if f != "alert_name"},
            **{f: getattr(self, f) for f in METADATA_FIELDS},
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Playbook":
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)


_ROW = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S | re.I)
_CELL = re.compile(r"<t[hd][^>]*>(.*?)</t[hd]>", re.S | re.I)
_CDATA = re.compile(r"<!\[CDATA\[(.*?)\]\]>", re.S)
# Where the editor ends a line. Kept as a newline so aliases written one per
# line stay separate once the tags are gone.
_BREAKS = re.compile(r"<br\s*/?>|</p>|</li>|</div>|</h[1-6]>", re.I)
_TAGS = re.compile(r"<[^>]+>")
_SPACES = re.compile(r"[ \t\r\f\v]+")


def cell_text(xhtml: str) -> str:
    """
    What a cell says, without its markup.

    A code macro keeps its content verbatim in CDATA; that is where a
    `splunk_query` written as JSON lives, and it is returned as it stands
    rather than run through the tag stripper, which would eat a `<` inside it.
    """
    xhtml = xhtml or ""
    cdata = _CDATA.findall(xhtml)
    if cdata:
        return "\n".join(part.strip() for part in cdata).strip()
    text = _TAGS.sub(" ", _BREAKS.sub("\n", xhtml))
    text = html.unescape(text).replace("\xa0", " ")
    lines = (_SPACES.sub(" ", line).strip() for line in text.split("\n"))
    return "\n".join(line for line in lines if line)


def rows_from_storage(xhtml: str) -> dict[str, str]:
    """
    The Page Properties table as {label: cell XHTML}.

    Only two-column rows are taken. A row with one cell is a heading and a row
    with three is a different table that happens to be on the page; neither is
    a property, and treating them as one would put junk in the record.
    """
    out: dict[str, str] = {}
    for row in _ROW.findall(xhtml or ""):
        cells = _CELL.findall(row)
        if len(cells) != 2:
            continue
        label = cell_text(cells[0]).replace("\n", " ")
        if label:
            out[label] = cells[1]
    return out


def _split_aliases(value: str) -> list[str]:
    return [a.strip() for a in re.split(r"[,;\n]", value or "") if a.strip()]


def _parse_query(value: str) -> dict | None:
    """
    `splunk_query` holds parameters, so the cell should be JSON.

    A cell that is not JSON is dropped with a warning rather than passed on as
    a string. Downstream would have to guess what to do with it, and guessing
    at a search is the failure this whole system is built to avoid.
    """
    value = (value or "").strip().strip("`")
    if not value:
        return None
    if value.lower().startswith("json"):
        value = value[4:].strip()
    try:
        parsed = json.loads(value)
    except (ValueError, TypeError):
        logger.warning("splunk_query is not JSON, ignoring it: %.80s", value)
        return None
    return parsed if isinstance(parsed, dict) else None


def from_rows(page_id: str, rows: dict[str, str], url: str = "",
              source: str = "confluence") -> Playbook | None:
    """
    One page's Page Properties rows -> a playbook, or None if it has no name.

    A page without an alert name cannot be routed to, so it is not a playbook
    for our purposes. Saying so is better than indexing something nothing will
    ever match.
    """
    fields: dict[str, Any] = {}
    for label, value in rows.items():
        name = FIELD_LABELS.get(label.strip().lower())
        if not name:
            continue
        text = cell_text(value)
        if name == "aliases":
            fields["aliases"] = _split_aliases(text)
        elif name == "splunk_query":
            fields["splunk_query"] = _parse_query(text)
        else:
            fields[name] = text

    if not fields.get("alert_name"):
        return None
    return Playbook(page_id=page_id, url=url, source=source, **fields)


def from_pages(pages: list[dict[str, Any]], source: str = "confluence") -> tuple[list[Playbook], list[dict]]:
    """Every page that is a playbook, and a note for each one that is not."""
    playbooks: list[Playbook] = []
    skipped: list[dict] = []
    for page in pages:
        page_id = str(page.get("id") or "")
        record = from_rows(page_id, rows_from_storage(page.get("body") or ""),
                           url=page.get("url") or "", source=source)
        if record is None:
            skipped.append({"page_id": page_id, "title": page.get("title") or "",
                            "reason": "no alert name in its Page Properties table"})
            continue
        playbooks.append(record)
    if skipped:
        logger.warning("%d page(s) are not playbooks (no alert name): %s", len(skipped),
                       ", ".join(s["title"] or s["page_id"] for s in skipped[:10]))
    return playbooks, skipped
