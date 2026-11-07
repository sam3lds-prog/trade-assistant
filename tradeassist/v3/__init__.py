"""Trade Assistant 3.0: the workstation's services. `install(svc)` wires every 3.0 area onto the service before the
/v3 router is built. Each area is a module here with an `install(svc)` of its own."""

from __future__ import annotations

from typing import Any

AREAS: tuple[str, ...] = ("workspaces", "research")


def install(svc: Any) -> None:
    if getattr(svc, "_v3_installed", False):
        return
    import importlib
    for name in AREAS:
        importlib.import_module(f"{__name__}.{name}").install(svc)
    svc._v3_installed = True
