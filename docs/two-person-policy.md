# Two-person policy

scram requires two distinct operators to authorize certain destructive
kill actions before dispatch. This document explains the policy, the
mechanism, and the V1 gap.

## When two-person is required

Per `KillCondition.requires_two_person`. The default policy:

- **Required** for actions whose blast radius exceeds one tenant or are
  otherwise irreversible:
  - `global-readonly` (cluster-wide; affects all tenants)
  - `rollback-to-checkpoint` when the checkpoint is older than N hours
    or affects shared infrastructure (component team's call)
  - any condition explicitly flagged by codeowners on PR review
- **Not required** for tenant-scoped actions:
  - `tenant-quarantine` (one tenant; reversible by unquarantine)
  - `process-exit` of a single host (Baton restarts)
  - `circuit-break-component` of a single non-shared component

For automatic predicates, the architectural review IS the
"two-person": predicate registration requires PR review by an operator
on the component's security codeowner team.

## Mechanism

When a condition with `requires_two_person=true` fires (auto or manual):

1. scram records a `kill_fires` row with `two_person_status='pending'`
   and `dispatched=false`.
2. scram calls witness's `ask` endpoint with a structured `Decision`
   carrying the condition id, action kind, reason, fire id, and state
   snapshot.
3. witness routes the decision to the configured reviewer pool
   (PagerDuty/inbox/Slack per witness's config).
4. A second operator (NOT the original `fired_by`) approves or rejects
   in witness.
5. witness returns the outcome to scram:
   - `approved` → scram updates `two_person_status='approved'`,
     `second_operator=<id>`, `second_at=now()`, then calls the action
     dispatcher. Dispatch outcome lands in `dispatch_result`.
   - `rejected` → scram updates `two_person_status='rejected'`; NO
     dispatch.
   - `timed-out` → scram updates `two_person_status='timed-out'`;
     NO dispatch. Operator must re-fire if still warranted.
   - `pending` → witness has no answer yet. scram persists pending and
     does not dispatch; the next eval tick will not re-fire because the
     fire is already recorded. Reconciliation logic (V2) sweeps stale
     pending rows.

## V1 STATUS: STUB

witness does not yet exist (~/WanderRepos/repos/witness/SPEC.md is drafted; no
implementation). scram's V1 ships with `StubWitnessClient`:

- Default outcome: `pending`. This is the SAFE default — it means the
  fire is recorded but no dispatch happens. An operator must manually
  intervene (V2: via witness; today: by directly observing the row and
  taking out-of-band action).
- Break-glass override: `SCRAM_WITNESS_AUTO_APPROVE=1` flips the
  default to `approved` with `second_operator='break-glass'`. Logged
  at ERROR level on every use. Only set this in genuine ops emergencies
  where waiting for witness V2 is itself a worse risk than auto-approve.

## V2 integration plan

When witness V1 lands:

1. Replace `scram.two_person.StubWitnessClient` with a real HTTP client
   to witness's `ask` endpoint.
2. Wire mTLS or a shared bearer token (TBD in witness ADR-001).
3. Update `docs/kill-condition-catalog.md` to remove the "stub" caveat
   from this row.
4. Add an integration test that exercises the full handshake against a
   witness test container.

The `Decision` shape in `scram/two_person.py` already mirrors witness's
SPEC, so V2 is a transport swap, not a redesign.
