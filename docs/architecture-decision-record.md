# Architecture decision record

**Accepted design:** Candidate 1 combines Progressive Dialing, a Predictive Pacing Engine, and an Independent Hard Safety Controller. Phase 5 measures both policies and checks actual PostgreSQL allocation contention.

## Context and alternatives

The prototype should improve agent utilization while keeping each outbound call within proven staffing capacity. Progressive dialing alone is simple to verify. A predictive dialer that starts unbound calls may use agents more efficiently, but simultaneous answers can exceed staffing. Combining the modes allows the prototype to compare predictive decisions with a progressive baseline under the same safety rules.

## Decision

Use Python and a CLI in one codebase. Use PostgreSQL as the source of truth for attempts and ownership. The controller holds a shared campaign lock during approval. Inside that transaction, the allocator locks agents before borrowers in ID order and uses `SKIP LOCKED` to allow concurrent approvals. Partial unique indexes on both owners protect every nonterminal attempt. The caller's request UUID is the decision's primary key so replays return the original approval. The worker recovers leased jobs directly from PostgreSQL.

## Guarantees and trade-offs

Approval commits the exclusive ownership and `READY` job together. Skipping locked rows can reduce approvals during contention. The 50-pair batch limit caps transaction size. Lease generations fence stale local writes. The attempt UUID is the stable provider key. The mock providers commit their ledger separately and support lookup, idempotent start, and cancellation that prevents a later start with the same key.

Unknown outcomes retain ownership at the cost of temporary capacity. Recovery backs off if lookup remains inconclusive. Provider cooldown gates new approvals and starts while the worker continues applying callbacks and reconciling existing work. The mock ledger demonstrates recovery across process loss. It shares the PostgreSQL instance and cannot simulate independent data loss at the provider.

Use a deterministic heuristic over the latest 50 known borrower outcomes within 30 minutes. Below 10 samples, use progressive demand. The 85% utilization target and five-second horizon make the forecast concrete. A 20% denominator floor and 50-request cap limit the response to low observed rates. Unknown calls count in full against forecast demand. These defaults may underfill the team when data is sparse or unstable. Store the inputs and intermediate values so a reviewer can reproduce the requested count. The controller still binds each approved call to a currently eligible agent and borrower.

When progressive mode already fills all safe slots, predictive pacing may deliver the same or lower utilization. The seeded runs hold conditions constant across modes while varying the policies. They use a logical clock and the existing PostgreSQL-backed path so that differences still pass through real reservations, events, and recovery. A separate contention run measures elapsed time and ownership invariants using multiple connections on a disposable PostgreSQL database.

With seed 42 in scenario A, progressive utilization was 76.9% and predictive utilization was 73.2%. Predictive requested 353 starts, 103 of which were approved. The two policies can produce different utilization even when predictive requests more work because it sometimes forecasts enough connections from pending calls and waits. In the separate load run, four connections created 100 distinct agent–borrower pairs and 100 jobs with zero invariant violations. That result covers the allocation workload at this specific size.

A reserved agent can still disappear after dispatch. The worker requests cancellation and records an answered call without an agent when that evidence arrives. The agent-loss run introduces delayed cancellation to expose this limit. The reservation guarantee covers database ownership and eligibility when checked. It cannot guarantee physical agent presence throughout a call.
