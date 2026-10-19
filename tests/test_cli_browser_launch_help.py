"""`netkeep browser launch --help` reads as user help, not the developer docstring (#584)."""

import re

from typer.testing import CliRunner

from netkeep.cli import app


def test_browser_launch_help_is_written_for_the_person_running_it() -> None:
    result = CliRunner().invoke(
        app,
        ["browser", "launch", "--help"],
        env={"NO_COLOR": "1", "TERM": "dumb", "COLUMNS": "200"},
    )

    assert result.exit_code == 0
    # Strip ANSI codes too: a CI that forces color splits "--json" across escapes.
    plain = re.sub(r"\x1b\[[0-9;]*m", "", result.output)
    text = " ".join(plain.split())
    assert "Show how to start the Chrome that netkeep attaches to" in text
    assert "--json" in text
    for leaked in ("netkeep.services", "P2-12", "ADR 0002", "GET /api/v1"):
        assert leaked not in text
