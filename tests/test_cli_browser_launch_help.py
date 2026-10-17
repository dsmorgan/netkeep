"""`netkeeper browser launch --help` reads as user help, not the developer docstring (#584)."""

from typer.testing import CliRunner

from netkeeper.cli import app


def test_browser_launch_help_is_written_for_the_person_running_it() -> None:
    result = CliRunner().invoke(app, ["browser", "launch", "--help"])

    assert result.exit_code == 0
    text = " ".join(result.output.split())
    assert "Show how to start the Chrome that netkeeper attaches to" in text
    assert "--json" in text
    for leaked in ("netkeeper.services", "P2-12", "ADR 0002", "GET /api/v1"):
        assert leaked not in text
