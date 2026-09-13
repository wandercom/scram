"""FastAPI surface for scram.

Endpoints:

- ``POST /v1/conditions`` — register a condition (auth required).
  Predicate-less by design: the API can persist metadata and arm
  manual-fire conditions, but auto-fire conditions only become live
  once the owning component re-registers in-process via
  :class:`scram.registry.Registry.register` (LOCAL CALLABLES; ADR-001).
- ``GET /v1/conditions`` — list registered conditions (in-memory + DB).
- ``DELETE /v1/conditions/{id}`` — unregister both in memory and DB.
- ``POST /v1/fire`` — manually fire a condition (auth required).
- ``GET /v1/fires`` — recent kill_fires for ops dashboards.
- ``GET /v1/health`` — liveness for Baton's adapter polling.

Auth: V1 uses an env-var-derived bearer token (``SCRAM_API_TOKEN``).
This is an internal-network service behind Fly's private network; the
token is a defense-in-depth measure, not the primary boundary.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from .dispatch import is_action_kind_valid, valid_action_kinds
from .evaluator import Evaluator
from .registry import Registry
from .types import KillAction, KillCondition

logger = logging.getLogger("scram.api")


# ----------------------------------------------------------------------
# Request / response models
# ----------------------------------------------------------------------


class ActionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str = Field(..., description="One of the valid action kinds.")
    config: dict[str, Any] = Field(default_factory=dict)


class ConditionPayload(BaseModel):
    """Request body for ``POST /v1/conditions``.

    Predicates are NOT accepted via the API (sim-vetted: predicates
    are local callables, not remote-HTTP). API-registered conditions
    with ``auto_fire=true`` will fail at evaluator time unless the
    same id is also registered in-process by the owning component.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., min_length=1, max_length=128)
    description: str = Field(..., min_length=1)
    action: ActionPayload
    auto_fire: bool = False
    requires_two_person: bool = False
    live: bool = False
    registered_by: str = Field(..., min_length=1)


class FirePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    condition_id: str = Field(..., min_length=1)
    reason: str = Field(..., min_length=1)
    operator: str = Field(..., min_length=1)
    state_snapshot: dict[str, Any] = Field(default_factory=dict)


class ConditionView(BaseModel):
    id: str
    description: str
    action: ActionPayload
    auto_fire: bool
    requires_two_person: bool
    live: bool
    registered_by: str
    has_predicate: bool


class FireView(BaseModel):
    id: str
    condition_id: str
    fired_at: str
    fired_by: str
    reason: str
    dispatched: bool
    dispatch_result: str | None
    two_person_status: str
    second_operator: str | None


# ----------------------------------------------------------------------
# Auth
# ----------------------------------------------------------------------


def require_token(authorization: str | None = Header(default=None)) -> None:
    """Bearer-token check against ``SCRAM_API_TOKEN``.

    Returns 503 (not 500) when the env-var is unset, so a misconfigured
    deploy fails closed: every authed endpoint refuses requests until a
    token is present. 503 (not 401) signals "service not ready" to
    Baton's adapter so it can route around without surfacing as auth
    failures in dashboards.
    """
    expected = os.environ.get("SCRAM_API_TOKEN")
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="SCRAM_API_TOKEN unset; service unauthenticated",
        )
    if authorization is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="missing Authorization")
    expected_header = f"Bearer {expected}"
    if authorization != expected_header:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="invalid token")


# ----------------------------------------------------------------------
# App factory
# ----------------------------------------------------------------------


def _condition_to_view(c: KillCondition) -> ConditionView:
    return ConditionView(
        id=c.id,
        description=c.description,
        action=ActionPayload(kind=c.action.kind, config=c.action.config),
        auto_fire=c.auto_fire,
        requires_two_person=c.requires_two_person,
        live=c.live,
        registered_by=c.registered_by,
        has_predicate=c.predicate is not None,
    )


def create_app(
    *,
    registry: Registry,
    evaluator: Evaluator | None = None,
    pool: object | None = None,
) -> FastAPI:
    """Build a FastAPI app bound to a registry and (optionally) evaluator.

    The pool is the asyncpg pool for ``GET /v1/fires`` / DB-backed
    operations. Tests pass an in-memory shim that supports ``acquire()``
    + ``fetch()``; production passes a real ``asyncpg.Pool``. In V1 we
    lean on duck-typing rather than introducing a Protocol since both
    callers exercise the same handful of methods.
    """

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if evaluator is not None:
            await registry.reload_metadata(evaluator.pool)
        yield

    app = FastAPI(
        title="scram",
        version="0.1.0",
        description="Emergency kill-switch service. See README.md.",
        lifespan=lifespan,
    )

    async def refresh_metadata() -> None:
        if pool is not None:
            await registry.reload_metadata(pool)  # type: ignore[arg-type]

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------
    @app.get("/v1/health")
    async def health() -> JSONResponse:
        await refresh_metadata()
        return JSONResponse(
            {
                "status": "ok",
                "registered_conditions": len(registry.list()),
                "valid_action_kinds": valid_action_kinds(),
            }
        )

    # ------------------------------------------------------------------
    # Conditions
    # ------------------------------------------------------------------
    @app.get("/v1/conditions")
    async def list_conditions() -> list[ConditionView]:
        await refresh_metadata()
        return [_condition_to_view(c) for c in registry.list()]

    @app.post(
        "/v1/conditions",
        status_code=status.HTTP_201_CREATED,
        dependencies=[Depends(require_token)],
    )
    async def create_condition(payload: ConditionPayload) -> ConditionView:
        if not is_action_kind_valid(payload.action.kind):
            raise HTTPException(
                422,
                detail=f"unknown action kind: {payload.action.kind}",
            )
        condition = KillCondition(
            id=payload.id,
            description=payload.description,
            action=KillAction(kind=payload.action.kind, config=payload.action.config),
            auto_fire=payload.auto_fire,
            requires_two_person=payload.requires_two_person,
            live=payload.live,
            registered_by=payload.registered_by,
            predicate=None,  # API never accepts predicates; ADR-001
        )
        if condition.auto_fire:
            # API-only registration of an auto_fire condition is a metadata
            # placeholder; warn loudly so the operator knows the in-process
            # register() call still has to happen.
            logger.warning(
                "condition %s registered via API with auto_fire=True but "
                "no predicate; the owning component must call "
                "Registry.register(...) in-process before auto-fire works",
                condition.id,
            )
        registry.register(condition)
        if pool is not None:
            await registry.persist(pool, condition)  # type: ignore[arg-type]
        return _condition_to_view(condition)

    @app.delete(
        "/v1/conditions/{condition_id}",
        dependencies=[Depends(require_token)],
    )
    async def delete_condition(condition_id: str) -> JSONResponse:
        existed_in_memory = registry.unregister(condition_id)
        existed_in_db = False
        if pool is not None:
            existed_in_db = await registry.delete_persisted(pool, condition_id)  # type: ignore[arg-type]
        if not (existed_in_memory or existed_in_db):
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="not found")
        return JSONResponse(
            {
                "id": condition_id,
                "removed_from_memory": existed_in_memory,
                "removed_from_db": existed_in_db,
            }
        )

    # ------------------------------------------------------------------
    # Manual fire
    # ------------------------------------------------------------------
    @app.post("/v1/fire", dependencies=[Depends(require_token)])
    async def manual_fire(payload: FirePayload) -> JSONResponse:
        if evaluator is None:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="evaluator not configured; manual fire unavailable",
            )
        condition = registry.get(payload.condition_id)
        if condition is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="condition not found")
        result = await evaluator.fire(
            condition,
            fired_by=payload.operator,
            reason=payload.reason,
            state_snapshot=payload.state_snapshot,
        )
        return JSONResponse(
            {
                "condition_id": result.condition_id,
                "fire_id": str(result.fire_id) if result.fire_id else None,
                "dispatched": result.dispatched,
                "dispatch_descriptor": result.dispatch_descriptor,
                "error": result.error,
            }
        )

    # ------------------------------------------------------------------
    # Fires listing
    # ------------------------------------------------------------------
    @app.get("/v1/fires")
    async def list_fires(limit: int = 50) -> list[FireView]:
        if pool is None:
            return []
        limit = max(1, min(limit, 500))
        async with pool.acquire() as conn:  # type: ignore[union-attr]
            rows = await conn.fetch(
                """
                SELECT id, condition_id, fired_at, fired_by, reason,
                       dispatched, dispatch_result, two_person_status,
                       second_operator
                FROM kill_fires
                ORDER BY fired_at DESC
                LIMIT $1
                """,
                limit,
            )
        return [
            FireView(
                id=str(r["id"]),
                condition_id=r["condition_id"],
                fired_at=r["fired_at"].isoformat(),
                fired_by=r["fired_by"],
                reason=r["reason"],
                dispatched=r["dispatched"],
                dispatch_result=r["dispatch_result"],
                two_person_status=r["two_person_status"],
                second_operator=r["second_operator"],
            )
            for r in rows
        ]

    return app


__all__ = ["create_app", "require_token"]
