#!/usr/bin/env python3
"""Check that a release tag names the version the package says it is (#683).

The version lives in two places: ``pyproject.toml``'s ``[project] version`` (what the
wheel is called and what PyPI publishes) and ``netkeep/__init__.py``'s ``__version__``
(what ``netkeep version`` prints). The release workflow runs this before it builds, so a
tag that disagrees with either, or a pair that disagrees with each other, stops the run
before anything is published.

    scripts/check_release_version.py --tag v0.1.0   # a tag push: all three must agree
    scripts/check_release_version.py                # a dry run: the two files must agree

Prints the version on success, for the workflow to read.
"""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# The one form a release tag takes: "v" and a plain X.Y.Z, nothing after it.
TAG_PATTERN = re.compile(r"v(\d+\.\d+\.\d+)")
_INIT_VERSION = re.compile(r'^__version__\s*=\s*"([^"]+)"\s*$', re.MULTILINE)


class VersionMismatch(Exception):
    """The tag and the package's two version sources don't all agree."""


def pyproject_version(path: Path) -> str:
    with path.open("rb") as f:
        version = tomllib.load(f)["project"]["version"]
    if not isinstance(version, str):
        raise VersionMismatch(f"{path}: [project] version is not a string")
    return version


def init_version(path: Path) -> str:
    match = _INIT_VERSION.search(path.read_text(encoding="utf-8"))
    if match is None:
        raise VersionMismatch(f'{path}: no __version__ = "..." line')
    return match.group(1)


def check(tag: str | None, pyproject: Path, init: Path) -> str:
    """Return the version, or raise VersionMismatch saying what disagrees."""
    project = pyproject_version(pyproject)
    module = init_version(init)
    if project != module:
        raise VersionMismatch(
            f"pyproject.toml says {project} but netkeep/__init__.py says {module}"
        )
    if tag is not None:
        match = TAG_PATTERN.fullmatch(tag)
        if match is None:
            raise VersionMismatch(f"tag {tag!r} is not of the form vX.Y.Z")
        if match.group(1) != project:
            raise VersionMismatch(f"tag {tag} does not match the package version {project}")
    return project


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--tag", help="the pushed tag, such as v0.1.0; omit for a dry run")
    parser.add_argument("--pyproject", type=Path, default=ROOT / "pyproject.toml")
    parser.add_argument("--init", type=Path, default=ROOT / "netkeep" / "__init__.py")
    args = parser.parse_args(argv)
    try:
        version = check(args.tag, args.pyproject, args.init)
    except VersionMismatch as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(version)
    return 0


if __name__ == "__main__":
    sys.exit(main())
