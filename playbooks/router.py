"""
Alert name -> playbook, by name: tiers 1 and 2.

An exact match on a normalised name, then a fuzzy one for a detection that was
renamed. Both are free, and they answer almost all SOAR traffic — an alert
arrives with the name the detection was given, and that name is in the wiki.
Tier 3, matching by meaning, is in store.py and only runs when both miss.

Ported unchanged from MCP-Server/Confluence/router.py.

A miss is reported as a miss. A playbook that nearly matches is worse than
none: the analyst reads criteria written for a different detection and has no
way to tell.
"""

from __future__ import annotations

import difflib
import logging
import re

from playbooks.extract import Playbook

logger = logging.getLogger("playbooks.router")

# Dots and hyphens survive so T1059.001 stays one token.
_CLEAN = re.compile(r"[^a-z0-9.\- ]+")
_WS = re.compile(r"\s+")


def normalise(name: str) -> str:
    return _WS.sub(" ", _CLEAN.sub(" ", (name or "").lower())).strip()


class AlertRouter:
    """Rebuilt from the corpus on every index; holds no state between them."""

    def __init__(self, playbooks: list[Playbook], cutoff: float = 0.85) -> None:
        self.cutoff = cutoff
        self.by_page: dict[str, Playbook] = {p.page_id: p for p in playbooks}
        self.table: dict[str, tuple[str, str, str]] = {}   # key -> (page, shown, how)
        self.conflicts: list[dict] = []

        for playbook in playbooks:
            names = [(playbook.alert_name, "alert_name")]
            names += [(a, "alias") for a in playbook.aliases]
            for name, source in names:
                key = normalise(name)
                if not key:
                    continue
                existing = self.table.get(key)
                if existing and existing[0] != playbook.page_id:
                    # A primary name outranks an alias. Two primaries claiming
                    # one name is a real ambiguity in the wiki, and it is
                    # reported rather than silently resolved — whichever page
                    # happened to load second is not an answer.
                    if source == "alert_name" and existing[2] == "alias":
                        self.table[key] = (playbook.page_id, name, source)
                    elif existing[2] == "alert_name" and source == "alias":
                        pass
                    else:
                        self.conflicts.append(
                            {"name": name, "pages": [existing[0], playbook.page_id]}
                        )
                    continue
                self.table[key] = (playbook.page_id, name, source)

        self._keys = list(self.table)
        for clash in self.conflicts:
            logger.warning(
                "two playbooks claim %r: %s — one of them will never be reached",
                clash["name"], " and ".join(clash["pages"]),
            )

    def match(self, alert_name: str) -> dict:
        """
        The playbook for this alert, with how it was found.

        `how` is returned rather than implied: a fuzzy match is a guess that
        happened to clear a threshold, and an analyst reading criteria for a
        detection with a slightly different name should be told that is what
        they are reading.
        """
        key = normalise(alert_name)
        if not key:
            return {"matched": False, "how": "empty", "playbook": None}

        hit = self.table.get(key)
        if hit:
            return {
                "matched": True, "how": "exact", "matched_name": hit[1],
                "playbook": self.by_page[hit[0]].as_dict(),
            }

        near = difflib.get_close_matches(key, self._keys, n=1, cutoff=self.cutoff)
        if near:
            page, shown, _ = self.table[near[0]]
            score = difflib.SequenceMatcher(None, key, near[0]).ratio()
            logger.info("fuzzy match %r -> %r (%.2f)", alert_name, shown, score)
            return {
                "matched": True, "how": "fuzzy", "matched_name": shown,
                "similarity": round(score, 3),
                "playbook": self.by_page[page].as_dict(),
            }

        # Said plainly. "No playbook" is a fact an analyst can act on; a
        # near-miss presented as a match is one they cannot check.
        return {
            "matched": False, "how": "miss", "playbook": None,
            "note": (
                f"No playbook covers an alert named {alert_name!r}. "
                f"{len(self.by_page)} playbook(s) are loaded; the closest was not "
                f"within the {self.cutoff} similarity cutoff."
            ),
        }

    @property
    def stats(self) -> dict:
        by_source: dict[str, int] = {}
        for _page, _shown, source in self.table.values():
            by_source[source] = by_source.get(source, 0) + 1
        return {
            "playbooks": len(self.by_page),
            "names": len(self.table),
            "by_source": by_source,
            "conflicts": self.conflicts,
        }
