# Architecture decision record

**Accepted design:** Candidate 1 combines Progressive Dialing, a Predictive Pacing Engine, and an Independent Hard Safety Controller. Phase 2 adds progressive approval and durable reservations to the database and domain foundation.

## Context and alternatives

The prototype should improve agent utilization while keeping each outbound call within proven staffing capacity. Progressive dialing alone is simple to verify. A predictive dialer that starts unbound calls may use agents more efficiently, but simultaneous answers can exceed staffing. Combining the modes allows the prototype to compare predictive decisions with a progressive baseline under the same safety rules.

## Decision

Use Python and a CLI in one codebase. Use PostgreSQL as the source of truth for attempts and ownership. The controller holds a shared campaign lock during approval. Inside that transaction, the allocator locks agents before borrowers in ID order and uses `SKIP LOCKED` to allow concurrent approvals. Partial unique indexes on both owners protect every nonterminal attempt. The caller's request UUID is the decision's primary key so replays return the original approval. A PostgreSQL-backed worker can later recover the jobs without a separate queue service.

## Guarantees and trade-offs

Phase 2 adds exclusive active ownership and commits each approval, its reservations, and its dispatch jobs together. Skipping locked rows can reduce approvals for a tick during contention. The 50-pair batch limit caps transaction size. A stored `READY` job represents an approved reservation awaiting dispatch. The provider and recovery phase will use the attempt UUID as the provider idempotency key and reconcile ambiguous outcomes.

At approval time each active attempt needs its own eligible agent and borrower. Unknown outcomes will retain that ownership at the cost of temporary capacity. The mock provider contract must support idempotent starts and lookup by stable key for recovery after ambiguous dispatch.

When progressive mode already fills all safe slots, predictive pacing may deliver the same utilization. The seeded comparison will measure the effect. A reserved agent can still disappear after dispatch, so the system will request cancellation and record any answered call without an agent. The reservation guarantee covers database ownership and eligibility when checked. It cannot guarantee physical agent presence throughout a call.
