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

from playbooks.extract import Playbook, use_case_id_in, without_use_case_id

logger = logging.getLogger("playbooks.router")

# Dots and hyphens survive so T1059.001 stays one token.
_CLEAN = re.compile(r"[^a-z0-9.\- ]+")
_WS = re.compile(r"\s+")


def normalise(name: str) -> str:
    return _WS.sub(" ", _CLEAN.sub(" ", (name or "").lower())).strip()


def compatible(playbook: Playbook, use_case_id: str) -> bool:
    """
    May this playbook answer an alert of this use case, when no id matched?

    An alert from the SOAR is one use case, exactly. A playbook that carries a
    different use case id is written for a different detection, however close
    its name or its wording — reading its criteria against this alert is the
    failure this service exists to prevent. A playbook with no id yet is
    allowed: its page has simply not been given one.
    """
    if not use_case_id:
        return True
    own = (playbook.use_case_id or "").strip().upper()
    return not own or own == use_case_id


def mismatch(alert_name: str, use_case_id: str, playbook: Playbook, how: str) -> dict:
    """A name that reaches a playbook of another use case: refused, and said so."""
    logger.warning("%s match %r -> %r refused: use case %s, playbook is %s",
                   how, alert_name, playbook.alert_name, use_case_id, playbook.use_case_id)
    return {
        "matched": False, "how": "use_case_mismatch", "playbook": None,
        "use_case_id": use_case_id,
        "note": (
            f"The alert is use case {use_case_id}, and no playbook carries that id. "
            f"Its name reaches {playbook.alert_name!r} ({how}), but that playbook is "
            f"use case {playbook.use_case_id}, a different detection, so its criteria "
            f"were not used. Give the {use_case_id} playbook page its Use Case ID."
        ),
    }


class AlertRouter:
    """Rebuilt from the corpus on every index; holds no state between them."""

    def __init__(self, playbooks: list[Playbook], cutoff: float = 0.85) -> None:
        self.cutoff = cutoff
        self.by_page: dict[str, Playbook] = {p.page_id: p for p in playbooks}
        self.table: dict[str, tuple[str, str, str]] = {}   # key -> (page, shown, how)
        self.by_id: dict[str, str] = {}                     # use case id -> page
        self.conflicts: list[dict] = []

        for playbook in playbooks:
            uc = playbook.use_case_id.strip().upper()
            if uc:
                if uc in self.by_id and self.by_id[uc] != playbook.page_id:
                    # Same rule as two primary names: reported, not resolved by load order.
                    self.conflicts.append({"use_case_id": uc, "pages": [self.by_id[uc], playbook.page_id]})
                else:
                    self.by_id[uc] = playbook.page_id
            names = [(playbook.alert_name, "alert_name")]
            # The title leads with its use case code; an alert sent with the bare name still matches.
            bare = without_use_case_id(playbook.alert_name)
            if bare and bare != playbook.alert_name:
                names.append((bare, "alert_name"))
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
                clash.get("name") or clash.get("use_case_id"), " and ".join(clash["pages"]),
            )

    @staticmethod
    def use_case_for(alert_name: str, use_case_id: str = "") -> str:
        """The use case id the SOAR sent, else one written in the alert name, else ''."""
        return (use_case_id or "").strip().upper() or use_case_id_in(alert_name)

    def match(self, alert_name: str, use_case_id: str = "") -> dict:
        """
        The playbook for this alert, with how it was found.

        The use case id first: the one the SOAR sent, else a code in the alert
        name. It names the detection exactly, whatever it is called today. An id
        no playbook carries falls through to the name, so detections whose
        playbooks have no id yet still match.

        `how` is returned rather than implied: a fuzzy match is a guess that
        happened to clear a threshold, and an analyst reading criteria for a
        detection with a slightly different name should be told that is what
        they are reading.
        """
        uc = self.use_case_for(alert_name, use_case_id)
        if uc in self.by_id:
            playbook = self.by_page[self.by_id[uc]]
            return {
                "matched": True, "how": "id", "use_case_id": uc,
                "matched_name": playbook.alert_name, "playbook": playbook.as_dict(),
            }

        key = normalise(alert_name)
        if not key:
            return {"matched": False, "how": "empty", "playbook": None}

        hit = self.table.get(key)
        if hit:
            playbook = self.by_page[hit[0]]
            if not compatible(playbook, uc):
                return mismatch(alert_name, uc, playbook, "exact")
            return {
                "matched": True, "how": "exact", "matched_name": hit[1],
                "playbook": playbook.as_dict(),
            }

        near = difflib.get_close_matches(key, self._keys, n=1, cutoff=self.cutoff)
        if near:
            page, shown, _ = self.table[near[0]]
            playbook = self.by_page[page]
            if not compatible(playbook, uc):
                return mismatch(alert_name, uc, playbook, "fuzzy")
            score = difflib.SequenceMatcher(None, key, near[0]).ratio()
            logger.info("fuzzy match %r -> %r (%.2f)", alert_name, shown, score)
            return {
                "matched": True, "how": "fuzzy", "matched_name": shown,
                "similarity": round(score, 3),
                "playbook": playbook.as_dict(),
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
            "use_case_ids": len(self.by_id),
            "by_source": by_source,
            "conflicts": self.conflicts,
        }
