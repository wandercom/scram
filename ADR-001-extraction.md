# ADR-001: scram architecture

**Status:** Accepted (2026-05-06; Claude/Codex/sim collaboration)
**Source spec:** `~/WanderRepos/repos/scram/SPEC.md`

## Context

scram is the emergency kill-switch — the last-resort termination
mechanism when something catastrophic is in progress that no slice-5
canary, slice-6 alarm, slice-0.5 degraded-mode, or witness operator
intervention can stop. Named after nuclear-reactor SCRAM: the
emergency shutdown control.

scram does not exist anywhere yet (no private impl in Reeve).
Reeve's existing detection logic (cross-tenant lateral movement,
audit-chain integrity break) gets registered AS scram conditions
once scram exists.

## Decision

**Python service, hosted standalone. Out-of-band kill-switch via
SCRAM_FORCE_READONLY env-var (boot check). Predicate registry +
periodic evaluator + action dispatcher with stack-component
integrations (Baton, Tessera, witness). Two-person rule for
non-tenant-scoped automatic actions.**

### Why Python

- Sits below all language worlds; needs to fire across the stack.
- The actions (process-exit a Reeve instance, route traffic away via
  Baton, etc.) are infrastructure-level; co-locating with Baton
  (Python) reduces serialization overhead.
- numpy/scipy not needed; standard library is enough.

### Why standalone service

- A scram embedded inside any one component (e.g., Reeve) couldn't
  reliably kill-switch the OTHER components.
- Standalone gives it independent lifecycle: scram restarts don't
  perturb application services; application services dying don't
  perturb scram.

### Two layers of authority

1. **Out-of-band emergency override** — env-var
   `SCRAM_FORCE_READONLY=1` (or Fly secret) checked at boot by EVERY
   component. If set, components flip to read-only mode regardless
   of scram's runtime state. This is the "operator pulled the lever
   physically" case; it doesn't depend on scram being reachable.
2. **scram service runtime** — registered kill conditions, periodic
   evaluation, action dispatch. Can be paused, restarted, even
   killed; the env-var override remains.

### Repo layout

```
~/WanderRepos/repos/scram/
├── SPEC.md
├── ADR-001-extraction.md
├── pyproject.toml
├── migrations/
│   └── 001_kill_conditions.sql
├── src/scram/
│   ├── __init__.py
│   ├── types.py             # KillCondition, KillAction, TriggerResult
│   ├── registry.py          # in-memory + persisted condition registry
│   ├── evaluator.py         # periodic predicate eval loop
│   ├── dispatch.py          # Baton/Tessera/witness integration
│   ├── two_person.py        # two-person rule enforcement (witness API)
│   ├── api.py               # FastAPI: register/list/fire endpoints
│   └── cli.py               # 'scram run', 'scram fire', 'scram list'
├── tests/
│   ├── test_registry.py
│   ├── test_evaluator.py
│   ├── test_dispatch.py
│   ├── test_two_person.py
│   └── test_api.py
├── docs/
│   ├── kill-condition-catalog.md  # known conditions across stack
│   ├── two-person-policy.md
│   └── force-readonly.md          # the env-var override pattern
├── Dockerfile
└── fly.toml
```

### Schema (single table)

```sql
CREATE TABLE kill_conditions (
  id              text PRIMARY KEY,
  description     text NOT NULL,
  -- Action shape: one of 'process-exit' | 'rollback-to-checkpoint' |
  -- 'circuit-break-component' | 'tenant-quarantine' | 'global-readonly'
  action_kind     text NOT NULL,
  action_config   jsonb NOT NULL DEFAULT '{}'::jsonb,
  -- Whether this condition fires automatically (predicate-based) or
  -- requires manual scram fire() invocation.
  auto_fire       boolean NOT NULL DEFAULT false,
  -- Two-person required? Set per condition. For non-tenant-scoped
  -- destructive actions (global-readonly, multi-tenant rollback), TRUE.
  requires_two_person boolean NOT NULL DEFAULT false,
  -- Live or dry-run. dry-run mode logs the trigger but doesn't dispatch.
  live            boolean NOT NULL DEFAULT false,
  registered_at   timestamptz NOT NULL DEFAULT now(),
  registered_by   text NOT NULL
);

CREATE TABLE kill_fires (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  condition_id    text NOT NULL REFERENCES kill_conditions(id),
  fired_at        timestamptz NOT NULL DEFAULT now(),
  fired_by        text NOT NULL,        -- 'auto' or operator id
  reason          text NOT NULL,
  state_snapshot  jsonb NOT NULL,       -- forensic context
  second_operator text,                 -- for two-person rule
  second_at       timestamptz,
  dispatched      boolean NOT NULL DEFAULT false,
  dispatch_result text                  -- output of action dispatch
);
```

### Predicate evaluation

scram polls all registered conditions where `auto_fire=true AND
live=true` every N seconds (config: 5s default). Each condition's
predicate is a Python callable registered by component owners
(Reeve owns cross-tenant lateral movement; ledger owns
classification-fail-closed cascade; etc.). When a predicate returns
true, scram:

1. Records `kill_fires` row with auto-fire context.
2. Checks two-person rule: if required, blocks dispatch awaiting a
   second operator via witness; if not, dispatches immediately.
3. Dispatches the action via `dispatch.py`.
4. Writes to tessera (every fire is auditable forever).

### Manual fire path

Operator-initiated fires hit `POST /v1/fire` with auth + reason.
Same flow as auto except `fired_by=operator_id`.

### Dispatch actions

- **process-exit** — POST to Baton's adapter control: "drain and
  restart adapter X." Reeve's `process-exit` boot code observes
  SIGTERM, follows slice 2.5's graceful-shutdown protocol.
- **rollback-to-checkpoint** — POST to Baton's canary control: "roll
  back current canary; pin to last-known-good." Reeve's slice 5
  thresholds are advisory; scram's rollback is definitive.
- **circuit-break-component** — POST to Baton: "all adapters for
  component X return 503 until manual unbreak."
- **tenant-quarantine** — POST to Baton: "all traffic for tenant X
  returns 503 until manual unquarantine." Tenant-scoped; doesn't
  require two-person rule.
- **global-readonly** — POST to a stack-shared control plane that
  flips all components to read-only. Requires two-person.

### Reeve integration (Wave 3)

Reeve registers these conditions via scram's API at startup:
- `cross-tenant-lateral-movement` — auto-fire, tenant-quarantine
  action, two-person false (tenant-scoped).
- `audit-chain-integrity-break` — auto-fire, global-readonly action,
  two-person TRUE (cluster-wide destructive).
- `emergency-readonly-on-pg-down` — auto-fire, global-readonly,
  two-person TRUE.

The registration is idempotent; Reeve calls it on every boot.

### Out-of-band override (BOOT-ONLY, sim-vetted)

`SCRAM_FORCE_READONLY=1` env var checked at every component's boot:

```python
# Reeve, Baton, Apprentice, Chronicler — each on startup:
if os.environ.get("SCRAM_FORCE_READONLY") == "1":
  app.read_only = True
  logger.error("SCRAM_FORCE_READONLY=1 set; running read-only")
```

**Why boot-only**: mid-runtime flip would invert the failsafe — it
would require polling/signal-handling, which means readonly
enforcement depends on working code in the running process — the
exact thing we're trying to protect against. A wedged or corrupted
process won't see the flip. Boot-only means "the next thing that
starts obeys the rule" — what you want when pulling the emergency
brake.

For faster propagation, use scram's `process-exit` action: forces
restart, which picks up the env-var on boot.

This is independent of scram service. If scram is down AND a human
operator decides "stop everything," they set the secret and let
boots cycle.

## Consequences

**Positive**
- Last-resort termination with audit trail.
- Two-person rule prevents single-operator catastrophic actions.
- Out-of-band override means "stop the world" works even when scram
  is down.
- Standalone service has independent lifecycle; component failures
  don't take it down.

**Negative**
- One more service to deploy + monitor.
- Two-person rule UX requires witness component (Wave 2 dependency).
- Predicate evaluation costs a query per condition per cycle; bound
  by aegis budget.

## Migration plan

scram is net-new; no migration. First milestone (V1):

1. Init `~/WanderRepos/repos/scram/`.
2. Schema migration applied (decide hosting: dedicated Postgres or
   shared cluster).
3. Implement registry + evaluator + dispatch (mock Baton/Tessera
   integration in V1).
4. Implement FastAPI for register/list/fire endpoints.
5. Implement out-of-band env-var documentation; reference impl in
   the `docs/force-readonly.md`.
6. Reeve registers its three conditions at startup.
7. Deploy to staging.
8. Two-person rule wires to witness (V2 — depends on witness
   landing).

### Predicate runtime hosting (sim-vetted: LOCAL CALLABLE)

Predicates are Python callables registered via `scram.register(fn)`.
Sim REJECTED remote HTTP for V1: adds network dependency, auth
surface, deployment coordination for zero architectural benefit at
this scale. Three components register predicates (Reeve, Baton,
Apprentice) — all Python services we control, all booting after
scram. They import scram's SDK and `register(fn)`. Done. Remote
HTTP becomes load-bearing only when non-Python or external systems
need to inject predicates; defer until that's a concrete need.

### Cross-component authority (sim-vetted: OPT-IN COORDINATOR)

scram does NOT have privileged authority. Trust model:

- Each component opts into scram's authority by registering predicates.
  No registration → scram has no power over that component.
- Action dispatch calls the target component's existing internal API
  (e.g., Reeve's `/internal/circuit/<name>/open`). Those APIs are
  designed to be safe under any internal-network call (idempotent,
  validated, logged) — same protections regardless of caller.
- Security boundary is **network topology** (Fly private network) +
  the fact that scram runs as a separate process with no elevated
  privileges. If scram is compromised, the attacker has the same
  reach as any internal service — no privilege escalation.
- No RPC tokens, no shared identity vouching. Trust is opt-in,
  not imposed.

## Open questions

- Action latency: poll-to-dispatch budget. Bound via aegis; if
  dispatch needs > 30s for global-readonly, document the propagation
  delay so operators know.
- Predicate ownership: when a component owner deletes the function
  that backed a registered predicate, scram's registry has a stale
  entry. Lean: predicate registration is a startup-only action; on
  every restart the registry is re-built from registrants' fresh
  registrations.
