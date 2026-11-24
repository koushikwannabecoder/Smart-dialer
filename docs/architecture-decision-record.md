# Architecture decision record

**Accepted design:** Candidate 1 combines Progressive Dialing, a Predictive Pacing Engine, and an Independent Hard Safety Controller. Phase 1 establishes the database and domain contracts.

## Context and alternatives

The prototype should improve agent utilization while keeping each outbound call within proven staffing capacity. Progressive dialing alone is simple to verify. A predictive dialer that starts unbound calls may use agents more efficiently, but simultaneous answers can exceed staffing. Combining the modes allows the prototype to compare predictive decisions with a progressive baseline under the same safety rules.

## Decision

Use Python and a CLI in one codebase. Use PostgreSQL as the source of truth for attempts and ownership. Row locks, uniqueness constraints, and transactions will let multiple workers reserve agents and borrowers and enqueue approved work together. A PostgreSQL-backed worker can recover durable jobs without introducing a separate queue service. Pacing logic has read-only inputs and submits a request to the separate controller.

## Guarantees and trade-offs

Phase 1 enforces campaign membership and allowed stored state values. The lifecycle validators reject invalid state changes. The next phase adds exclusive active ownership and the atomic approval transaction. The provider and recovery phase adds stable-key reconciliation and durable event handling for `UNKNOWN` outcomes.

At approval time each active attempt needs its own eligible agent and borrower. Unknown outcomes will retain that ownership at the cost of temporary capacity. The mock provider contract must support idempotent starts and lookup by stable key for recovery after ambiguous dispatch.

When progressive mode already fills all safe slots, predictive pacing may deliver the same utilization. The seeded comparison will measure the effect. A reserved agent can still disappear after dispatch, so the system will request cancellation and record any answered call without an agent. The reservation guarantee covers database ownership and eligibility when checked. It cannot guarantee physical agent presence throughout a call.
