"""Build hook: ship the built frontend inside the wheel and sdist (#596).

A wheel or sdist without ``frontend/dist`` would install a netkeep whose page says
"The frontend is not built", so a missing build fails here instead. An editable install
(``make install``) is exempt: it serves ``frontend/dist`` from the checkout.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

UI_SOURCE = "frontend/dist"
UI_TARGET = "netkeep/web/ui"


class UiBuildHook(BuildHookInterface):
    PLUGIN_NAME = "custom"

    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        if version == "editable":
            return
        dist = Path(self.root) / UI_SOURCE
        if not (dist / "index.html").is_file():
            raise RuntimeError(
                f"{UI_SOURCE}/index.html not found: run `make build-ui` before building "
                "a wheel or sdist, so the package ships the UI."
            )
        if self.target_name == "wheel":
            build_data["force_include"][UI_SOURCE] = UI_TARGET
