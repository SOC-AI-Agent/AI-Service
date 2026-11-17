"""
Confluence pages -> playbook records.

A field is read from either of two layouts, and a page may mix them:

- a heading and the text under it (`True Condition`, `Steps to Work (For Tier
  1/Shift lead):`), the way the SOC writes playbooks. The alert name is then the
  page title, and the version is the newest row of a version-history table whose
  first column is `Version`.
- a row of a two-column table (a Page Properties macro), the older layout.

The backend fetches the pages (through the Confluence connector, which holds the
Confluence credentials) and sends each one here as {id, title, version, url,
body}, body being the storage-format XHTML exactly as Confluence returned it.

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
    "use_case_id", "use_case_category", "version", "splunk_query", "work_around",
    "target_audience", "contact",
]
# A titled page counts as a playbook only with one of these: the space home page
# has a "Description" heading too, and is not a detection.
_PLAYBOOK_EVIDENCE = ("use_case_id", "true_condition", "false_condition", "work_around")

# Confluence label -> our field. A label that is not here is ignored rather
# than guessed at: a page that grows a new row should not silently change what
# the matcher sees.
FIELD_LABELS = {
    "alert name": "alert_name",
    "alert": "alert_name",
    "use case": "alert_name",
    "use case / alert name": "alert_name",
    "use case name": "alert_name",
    "use case id": "use_case_id",
    "use case code": "use_case_id",
    "uc id": "use_case_id",
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
    "steps to work": "work_around",
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
    # The SOC's own code for the detection (UC-CAT-012). Matched before any
    # name: it does not change when the detection is renamed.
    use_case_id: str = ""
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


# The SOC's use case codes: UC-CAT-012, UC-CSOC-CUSTOM-018. Written in the
# detection's name, and so in the playbook page's title.
_USE_CASE_ID = re.compile(r"\bUC(?:-[A-Z0-9]+)*-\d+\b", re.I)


def use_case_id_in(text: str) -> str:
    """The use case code a name carries ('Threat - UC-CAT-012 - ...' -> 'UC-CAT-012'), or ''."""
    found = _USE_CASE_ID.search(text or "")
    return found.group(0).upper() if found else ""


def without_use_case_id(name: str) -> str:
    """'UC-WEB-001 - Directory Brute Force Detected' -> 'Directory Brute Force Detected'."""
    return " - ".join(p.strip() for p in _USE_CASE_ID.sub("", name or "").split(" - ") if p.strip())


_HEADING = re.compile(r"<h[1-6][^>]*>(.*?)</h[1-6]>", re.S | re.I)
_TABLE = re.compile(r"<table[^>]*>(.*?)</table>", re.S | re.I)
_PAREN = re.compile(r"\([^)]*\)")


def label_key(label: str) -> str:
    """'Steps to Work (For Tier 1/Shift lead):' -> 'steps to work', the form FIELD_LABELS uses."""
    return " ".join(_PAREN.sub(" ", label).strip().rstrip(":").lower().split())


def sections_from_storage(xhtml: str) -> dict[str, str]:
    """
    Each heading and the XHTML under it, up to the next heading of any level.

    Only what sits directly under a heading: `Workaround` with nothing but its
    sub-headings beneath it is empty and left out, so it does not swallow
    `True Condition` and the rest. The first heading of a repeated label wins.
    """
    xhtml = xhtml or ""
    heads = list(_HEADING.finditer(xhtml))
    out: dict[str, str] = {}
    for i, head in enumerate(heads):
        label = cell_text(head.group(1)).replace("\n", " ")
        end = heads[i + 1].start() if i + 1 < len(heads) else len(xhtml)
        body = xhtml[head.end():end]
        if label and label not in out and cell_text(body):
            out[label] = body
    return out


def _version_number(value: str) -> tuple:
    return tuple(int(p) for p in re.findall(r"\d+", value))


def _history_versions(table: str) -> list[str]:
    """The versions a version-history table lists, or [] for any other table."""
    rows = [[cell_text(c) for c in _CELL.findall(r)] for r in _ROW.findall(table)]
    if len(rows) < 2 or not rows[0] or label_key(rows[0][0]) != "version":
        return []
    # A version has a digit; this keeps a Page Properties table that opens on a
    # Version row ("Alert Name", "Aliases" below it) from passing as a history.
    return [r[0] for r in rows[1:] if r and re.search(r"\d", r[0])]


def version_from_history(xhtml: str) -> str:
    """The newest version in a table whose first header is `Version`, newest by number."""
    for table in _TABLE.findall(xhtml or ""):
        versions = _history_versions(table)
        if versions:
            return max(versions, key=_version_number)
    return ""


def without_history(xhtml: str) -> str:
    """The page without its version-history table, which would otherwise read as part
    of whatever section it follows (Aliases, when it sits under the overview)."""
    return _TABLE.sub(lambda m: "" if _history_versions(m.group(1)) else m.group(0), xhtml or "")


def _split_aliases(value: str) -> list[str]:
    return [a.strip() for a in re.split(r"[,;\n]", value or "") if a.strip()]


def _parse_query(value: str) -> dict | None:
    """
    `splunk_query` holds parameters, so the cell should be JSON.

    A page written the SOC way holds its detection rule's SPL here instead:
    documentation for the detection team, never read as a search, and with no
    warning since it is expected. Without parameters the backend plans its own
    searches. Something that starts like JSON but does not parse is dropped
    with a warning rather than passed on as a string: guessing at a search is
    the failure this whole system is built to avoid.
    """
    value = (value or "").strip().strip("`")
    if not value:
        return None
    if value.lower().startswith("json"):
        value = value[4:].strip()
    if not value.startswith("{"):
        return None
    try:
        parsed = json.loads(value)
    except (ValueError, TypeError):
        logger.warning("splunk_query is not JSON, ignoring it: %.80s", value)
        return None
    return parsed if isinstance(parsed, dict) else None


def from_rows(page_id: str, rows: dict[str, str], url: str = "",
              source: str = "confluence", title: str = "") -> Playbook | None:
    """
    One page's labelled fields (table rows or headed sections) -> a playbook,
    or None if it has no name.

    With no `Alert Name` field, the page title is the name, but only for a page
    that carries playbook content (a use case id, conditions or steps). A page
    that cannot be routed to is not a playbook; saying so is better than
    indexing something nothing will ever match.
    """
    fields: dict[str, Any] = {}
    for label, value in rows.items():
        name = FIELD_LABELS.get(label_key(label))
        if not name:
            continue
        text = cell_text(value)
        if name == "aliases":
            fields["aliases"] = _split_aliases(text)
        elif name == "splunk_query":
            fields["splunk_query"] = _parse_query(text)
        elif name == "use_case_id":
            fields["use_case_id"] = text.split("\n")[0].strip()
        else:
            fields[name] = text

    # The title names the detection, use case code and all ("Web - UC-WEB-001 - ...").
    # A code alone is not enough: an empty page titled with one has no criteria to give.
    if not fields.get("alert_name") and title.strip() and any(fields.get(f) for f in _PLAYBOOK_EVIDENCE):
        fields["alert_name"] = title.strip()
    if not fields.get("alert_name"):
        return None
    fields.setdefault("use_case_id", use_case_id_in(fields["alert_name"]))
    return Playbook(page_id=page_id, url=url, source=source, **fields)


def from_pages(pages: list[dict[str, Any]], source: str = "confluence") -> tuple[list[Playbook], list[dict]]:
    """Every page that is a playbook, and a note for each one that is not."""
    playbooks: list[Playbook] = []
    skipped: list[dict] = []
    for page in pages:
        page_id = str(page.get("id") or "")
        body = page.get("body") or ""
        # A table row outranks a heading of the same label: it is the more deliberate of the two.
        fields = {**sections_from_storage(without_history(body)), **rows_from_storage(body)}
        version = version_from_history(body)
        if version:
            # Over any "Version" row: a two-column history's header row also reads as Version -> "Date".
            fields = {k: v for k, v in fields.items() if label_key(k) != "version"}
            fields["Version"] = version
        record = from_rows(page_id, fields, url=page.get("url") or "", source=source,
                           title=page.get("title") or "")
        if record is None:
            skipped.append({"page_id": page_id, "title": page.get("title") or "",
                            "reason": "no alert name: no Alert Name field, and no playbook "
                                      "sections (Use Case ID, conditions, steps) under its title"})
            continue
        playbooks.append(record)
    if skipped:
        logger.warning("%d page(s) are not playbooks (no alert name): %s", len(skipped),
                       ", ".join(s["title"] or s["page_id"] for s in skipped[:10]))
    return playbooks, skipped
