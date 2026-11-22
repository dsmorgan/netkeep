"""The UI reads which kinds quick checks skip from the API, and keeps no copy (#829, #834).

Until #834 the frontend kept its own ``FULL_UPKEEP_KINDS`` and a drift guard here compared
it with the scheduler's. ``GET /linkedin/schedule`` now marks each job ``skipped_on_quick``
(``tests/test_runs_serve.py`` pins the answer by value), so the guard is that no copy
comes back: a second list could drift from :data:`netkeep.services.scheduler.FULL_UPKEEP_KINDS`
with nothing to catch it.
"""

from __future__ import annotations

import re
from pathlib import Path

from netkeep.services.scheduler import FULL_UPKEEP_KINDS, SERVED_SCHEDULES

ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "frontend/src"

#: A set or array literal naming both kinds, in either order, as a copy of the list would.
COPY = re.compile(r"\[\s*'(connections_full|enrich)'\s*,\s*'(connections_full|enrich)'\s*\]")


def _sources() -> list[Path]:
    return [
        path
        for path in FRONTEND.rglob("*.ts*")
        if not path.name.endswith((".test.ts", ".test.tsx", ".d.ts"))
        and "test-support" not in path.name
        and "fixtures" not in path.name
    ]


def test_the_frontend_keeps_no_copy_of_the_skipped_kinds() -> None:
    found = [
        str(path.relative_to(ROOT))
        for path in _sources()
        if "FULL_UPKEEP_KINDS" in (text := path.read_text(encoding="utf-8"))
        or any(a != b for a, b in COPY.findall(text))
    ]
    assert found == []


def test_the_kinds_are_the_full_sync_and_enrichment() -> None:
    # Pinned by value: what `skipped_on_quick` marks, and every one of them is served.
    assert {kind.value for kind in FULL_UPKEEP_KINDS} == {"connections_full", "enrich"}
    assert set(SERVED_SCHEDULES) >= FULL_UPKEEP_KINDS
