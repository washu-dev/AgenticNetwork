"""OASF taxonomy resolution — turn a user's skill choice into an official routing
skill (name + integer id) so the generated card is DISCOVERABLE.

Why this exists: ADS routing matches on official OASF skill *names*, not the
slash-namespaced card skill ids. A card with no official routing skill publishes
"stored but not routed" and the planner never sees it. The generator resolves the
user's chosen ``oasf_key`` against the taxonomy and stamps the result onto the
card as an ``oasf:<name>:<id>`` tag (read back by the patched ads_utils.converter).

The taxonomy is injected (a list of {name,id,key} dicts) so this module has no
hard dependency on registration_service; ``load_taxonomy()`` is a convenience that
imports it at generation time when available.
"""
from __future__ import annotations

from dataclasses import dataclass

# Tag convention placed on card skills so the directory converter can route them
# without a per-agent edit to its hardcoded maps. Format: oasf:<name>:<id>.
OASF_TAG_PREFIX = "oasf:"


@dataclass(frozen=True)
class ResolvedOASFSkill:
    name: str  # canonical path-style OASF name (also the routing key)
    id: int

    def as_tag(self) -> str:
        return f"{OASF_TAG_PREFIX}{self.name}:{self.id}"


def load_taxonomy() -> list[dict]:
    """Load the OASF taxonomy from registration_service if importable, else []."""
    try:
        from registration_service.oasf_skills import OASF_SKILLS

        return list(OASF_SKILLS)
    except Exception:
        return []


def resolve(oasf_key: str, taxonomy: list[dict]) -> ResolvedOASFSkill:
    """Resolve a taxonomy key (e.g. 'nlp/.../question_answering') to name + id.

    Matches on the taxonomy entry's ``key`` (preferred) or ``name``. Raises with a
    helpful message if not found, since an unresolved skill means an undiscoverable
    agent."""
    for entry in taxonomy:
        if entry.get("key") == oasf_key or entry.get("name") == oasf_key:
            return ResolvedOASFSkill(name=entry["key"], id=int(entry["id"]))
    raise ValueError(
        f"OASF skill key {oasf_key!r} not found in the taxonomy "
        f"({len(taxonomy)} entries). Pick a key from registration_service/oasf_skills.py."
    )


def parse_tag(tag: str) -> ResolvedOASFSkill | None:
    """Inverse of ResolvedOASFSkill.as_tag(); None if the tag isn't an OASF tag.

    This is the exact logic the patched ads_utils.converter uses to read routing
    skills off a card — kept here too so the generator and the directory agree."""
    if not tag.startswith(OASF_TAG_PREFIX):
        return None
    rest = tag[len(OASF_TAG_PREFIX):]
    name, sep, id_str = rest.rpartition(":")
    if not sep or not name:
        return None
    try:
        return ResolvedOASFSkill(name=name, id=int(id_str))
    except ValueError:
        return None
