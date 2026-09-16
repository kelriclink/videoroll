from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute

from videoroll.apps.orchestrator_api.routers import live as live_router


def test_legacy_live_guard_rejects_mutations_when_disabled() -> None:
    with pytest.raises(HTTPException) as error:
        live_router._legacy_live_mutation_guard(SimpleNamespace(legacy_live_enabled=False))  # type: ignore[arg-type]

    assert error.value.status_code == 410


def test_legacy_live_guard_allows_rollback_when_enabled() -> None:
    assert live_router._legacy_live_mutation_guard(  # type: ignore[arg-type]
        SimpleNamespace(legacy_live_enabled=True)
    ) is None


def test_every_legacy_live_mutation_route_has_the_guard() -> None:
    mutation_methods = {"POST", "PUT", "PATCH", "DELETE"}
    routes = [
        route
        for route in live_router.router.routes
        if isinstance(route, APIRoute)
        and route.path.startswith("/live")
        and mutation_methods.intersection(route.methods or set())
    ]

    assert routes
    for route in routes:
        dependency_calls = {dependency.call for dependency in route.dependant.dependencies}
        assert live_router._legacy_live_mutation_guard in dependency_calls, (
            f"{sorted(route.methods or set())} {route.path} is missing legacy-live guard"
        )
