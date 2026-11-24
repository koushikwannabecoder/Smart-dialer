# Architecture decision record

**Accepted design:** Candidate 1 combines Progressive Dialing, a Predictive Pacing Engine, and an Independent Hard Safety Controller. Phase 3 connects progressive approvals to mock provider dispatch, event processing, and crash recovery.

## Context and alternatives

The prototype should improve agent utilization while keeping each outbound call within proven staffing capacity. Progressive dialing alone is simple to verify. A predictive dialer that starts unbound calls may use agents more efficiently, but simultaneous answers can exceed staffing. Combining the modes allows the prototype to compare predictive decisions with a progressive baseline under the same safety rules.

## Decision

Use Python and a CLI in one codebase. Use PostgreSQL as the source of truth for attempts and ownership. The controller holds a shared campaign lock during approval. Inside that transaction, the allocator locks agents before borrowers in ID order and uses `SKIP LOCKED` to allow concurrent approvals. Partial unique indexes on both owners protect every nonterminal attempt. The caller's request UUID is the decision's primary key so replays return the original approval. The worker recovers leased jobs directly from PostgreSQL.

## Guarantees and trade-offs

Approval commits the exclusive ownership and `READY` job together. Skipping locked rows can reduce approvals during contention. The 50-pair batch limit caps transaction size. Lease generations fence stale local writes. The attempt UUID is the stable provider key. The mock providers commit their ledger separately and support lookup, idempotent start, and cancellation that prevents a later start with the same key.

Unknown outcomes retain ownership at the cost of temporary capacity. Recovery backs off if lookup remains inconclusive. Provider cooldown gates new approvals and starts while the worker continues applying callbacks and reconciling existing work. The mock ledger demonstrates recovery across process loss. It shares the PostgreSQL instance and cannot simulate independent data loss at the provider.

When progressive mode already fills all safe slots, predictive pacing may deliver the same utilization. The seeded comparison will measure the effect. A reserved agent can still disappear after dispatch. The worker requests cancellation and records an answered call without an agent when that evidence arrives. The reservation guarantee covers database ownership and eligibility when checked. It cannot guarantee physical agent presence throughout a call.
