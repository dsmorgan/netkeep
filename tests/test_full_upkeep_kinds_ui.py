"""The UI's copy of the kinds quick checks skip matches the scheduler's (#829, drift guard).

``GET /linkedin/schedule`` doesn't say which job kinds the depth skips, so the frontend
keeps its own ``FULL_UPKEEP_KINDS`` (``frontend/src/features/automation/api.ts``) to mark
them on the LinkedIn page and leave them out of the dashboard's Coming up. This test
reads that literal and fails when it and :data:`netkeep.services.scheduler.FULL_UPKEEP_KINDS`
disagree, so a kind added to one can't be missed in the other.
"""

from __future__ import annotations

import re
from pathlib import Path

from netkeep.services.scheduler import FULL_UPKEEP_KINDS

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "frontend/src/features/automation/api.ts"

#: ``export const FULL_UPKEEP_KINDS ... = new Set([...])``, its list of string literals.
LITERAL = re.compile(r"export const FULL_UPKEEP_KINDS\b[^=]*=\s*new Set\(\[(?P<items>[^\]]*)\]\)")


def ui_kinds() -> set[str]:
    match = LITERAL.search(SOURCE.read_text(encoding="utf-8"))
    assert match is not None, f"no FULL_UPKEEP_KINDS literal in {SOURCE.relative_to(ROOT)}"
    items = match["items"]
    kinds = set(re.findall(r"'([a-z_]+)'", items))
    # Anything in the list that isn't a plain quoted kind would be missed above.
    assert re.sub(r"'[a-z_]+'|[\s,]", "", items) == "", items
    return kinds


def test_the_ui_skips_the_same_kinds_as_the_scheduler() -> None:
    assert ui_kinds() == {kind.value for kind in FULL_UPKEEP_KINDS}


def test_the_kinds_are_the_full_sync_and_enrichment() -> None:
    # Pinned by value, so the comparison above can't pass on two empty sets.
    assert ui_kinds() == {"connections_full", "enrich"}
