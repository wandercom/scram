"""Two-person rule enforcement.

Per ADR-001 and SPEC.md, kill actions that affect more than one tenant
or are otherwise irreversible (global-readonly, mass tenant-quarantine,
audit-chain integrity break) require two distinct operators to
authorize the fire before dispatch happens. The mechanism uses witness;
witness logs the decision and tessera persists the audit trail.

V1 STATUS — STUB. The witness component is a Wave 2 sibling that does
not exist yet (~/WanderRepos/repos/witness/SPEC.md is drafted, no implementation).
This module documents the integration point and provides a working
default that prevents accidental dispatch:

- Default behavior: ``ask_witness`` returns ``"pending"`` immediately;
  the evaluator records the fire with ``two_person_status='pending'``
  and DOES NOT dispatch. The condition stays armed; an operator must
  manually approve via the API (V2: via witness).
- Override for ops emergencies: setting ``SCRAM_WITNESS_AUTO_APPROVE=1``
  flips the default to ``"approved"``. Documented as break-glass only;
  every auto-approve is logged at ERROR level.
- Test-only: callers can inject a custom :class:`WitnessClient`.

V2 INTEGRATION POINT: replace :class:`StubWitnessClient` with a real
HTTP client to witness's ``ask`` endpoint. The ``Decision`` shape is
already locked to match witness's SPEC; only transport changes.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Literal, Protocol

logger = logging.getLogger("scram.two_person")

TwoPersonOutcome = Literal["approved", "rejected", "pending", "timed-out"]


@dataclass(frozen=True)
class Decision:
    """Structured ask sent to witness.

    Mirrors the shape from witness's SPEC.md so the V2 swap is byte-
    compatible. ``kind`` is fixed to ``"scram-confirm"`` so witness can
    route this to the right reviewer pool.
    """

    id: str  # the kill_fires.id (UUID as str)
    kind: str  # always "scram-confirm" for scram-originated decisions
    condition_id: str
    action_kind: str
    reason: str
    fired_by: str
    state_snapshot: dict[str, object]


class WitnessClient(Protocol):
    """Minimal interface scram needs from witness.

    Returning ``"pending"`` means "no answer yet, do NOT dispatch."
    Returning ``"approved"`` means "second operator confirmed, dispatch
    now." ``"rejected"``/``"timed-out"`` are terminal failures: the fire
    record stays in ``kill_fires`` for audit, but no dispatch occurs.
    """

    async def ask(self, decision: Decision) -> tuple[TwoPersonOutcome, str | None]:
        """Return (outcome, second_operator_id_or_None)."""
        ...


class StubWitnessClient:
    """V1 stub. Logs the ask and returns the default outcome.

    Default outcome is ``"pending"`` (safe; blocks dispatch). When the
    env var ``SCRAM_WITNESS_AUTO_APPROVE=1`` is set, the stub returns
    ``"approved"`` with a synthetic operator id ``"break-glass"`` —
    documented as ops-only break-glass; logged at ERROR level. Tests
    override the default by passing ``default_outcome=`` to the ctor.
    """

    def __init__(
        self,
        *,
        default_outcome: TwoPersonOutcome | None = None,
        default_second_operator: str | None = None,
    ) -> None:
        if default_outcome is None:
            if os.environ.get("SCRAM_WITNESS_AUTO_APPROVE") == "1":
                self._default_outcome: TwoPersonOutcome = "approved"
                self._default_second_op = default_second_operator or "break-glass"
            else:
                self._default_outcome = "pending"
                self._default_second_op = None
        else:
            self._default_outcome = default_outcome
            self._default_second_op = default_second_operator

    async def ask(self, decision: Decision) -> tuple[TwoPersonOutcome, str | None]:
        if self._default_outcome == "approved" and self._default_second_op == "break-glass":
            logger.error(
                "witness[stub] BREAK-GLASS auto-approve: condition=%s fired_by=%s",
                decision.condition_id,
                decision.fired_by,
            )
        else:
            logger.warning(
                "witness[stub] ask: condition=%s outcome=%s",
                decision.condition_id,
                self._default_outcome,
            )
        return self._default_outcome, self._default_second_op


async def evaluate_two_person(
    client: WitnessClient,
    *,
    fire_id: str,
    condition_id: str,
    action_kind: str,
    reason: str,
    fired_by: str,
    state_snapshot: dict[str, object],
) -> tuple[TwoPersonOutcome, str | None]:
    """Run the two-person handshake and return (outcome, second_op).

    Convenience wrapper around :class:`WitnessClient.ask`. Encapsulated
    so the evaluator/API don't construct ``Decision`` objects inline,
    keeping the witness shape change a single-edit when V2 lands.
    """
    decision = Decision(
        id=fire_id,
        kind="scram-confirm",
        condition_id=condition_id,
        action_kind=action_kind,
        reason=reason,
        fired_by=fired_by,
        state_snapshot=state_snapshot,
    )
    return await client.ask(decision)


__all__ = [
    "Decision",
    "StubWitnessClient",
    "TwoPersonOutcome",
    "WitnessClient",
    "evaluate_two_person",
]
