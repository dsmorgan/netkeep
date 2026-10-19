"""One source for the local owner (#623).

``services.users.local_owner`` (the first ``local`` user) and ``local_users`` (all of
them) are the only places that query for local users. A copy elsewhere can drift from
them: ``/backups`` checks the requester against ``local_owner``, so a differently ordered
copy in ``LocalSingleUser`` would make the current user and the owner two accounts.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parents[1] / "netkeep"
HOME = PACKAGE / "services" / "users.py"
QUERY = re.compile(r"User\.kind\s*==\s*UserKind\.LOCAL")


def _copies(source: str) -> list[int]:
    return [n for n, line in enumerate(source.splitlines(), 1) if QUERY.search(line)]


def test_no_copy_of_the_local_user_query_outside_the_users_service() -> None:
    found = {
        f"{path.relative_to(PACKAGE.parent)}:{n}"
        for path in PACKAGE.rglob("*.py")
        if path != HOME
        for n in _copies(path.read_text())
    }
    assert not found, f"use services.users.local_owner / local_users instead: {sorted(found)}"


@pytest.mark.parametrize(
    "snippet",
    [
        "select(User).where(User.kind == UserKind.LOCAL).order_by(User.id)",
        "select(User.id).where(User.kind==UserKind.LOCAL)",
    ],
)
def test_the_scan_catches_a_copy(snippet: str) -> None:
    assert _copies(snippet) == [1]


def test_the_users_service_still_holds_the_query() -> None:
    assert _copies(HOME.read_text())
