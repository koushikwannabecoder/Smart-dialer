# Architecture decision record

**Accepted design:** Candidate 1 combines Progressive Dialing, a Predictive Pacing Engine, and an Independent Hard Safety Controller. Both policies use the same reservation, dispatch, and recovery path.

## Context and alternatives

The prototype should improve agent utilization while keeping each outbound call within proven staffing capacity. Progressive dialing alone is simple to verify. A predictive dialer that starts unbound calls may use agents more efficiently, but simultaneous answers can exceed staffing. Combining the modes allows the prototype to compare predictive decisions with a progressive baseline under the same safety rules.

## Decision

Use Python and a CLI in one codebase. PostgreSQL is the source of truth for attempts and ownership. It already provides the transactions, row locks, unique indexes, and durable queue records this prototype needs. A separate cache or message broker would add another system to coordinate at approval and recovery time. The controller holds a shared campaign lock during approval. Inside that transaction, the allocator locks agents before borrowers in ID order and uses `SKIP LOCKED` to allow concurrent approvals. Partial unique indexes on both owners protect every nonterminal attempt. The caller's request UUID is the decision's primary key so replays return the original approval. The worker recovers leased jobs directly from PostgreSQL.

If a display cache is added later, the database still decides eligibility under lock. An agent row marked `AVAILABLE` is insufficient if a nonterminal attempt already holds it.

## Guarantees and trade-offs

Approval commits the exclusive ownership and `READY` job together. Skipping locked rows can reduce approvals during contention. The 50-pair batch limit caps transaction size. Lease generations fence stale local writes. The attempt UUID is the stable provider key. The mock providers commit their ledger separately and support lookup, idempotent start, and cancellation that prevents a later start with the same key.

Unknown outcomes retain ownership at the cost of temporary capacity. Recovery backs off if lookup remains inconclusive. Provider cooldown gates new approvals and starts while the worker continues applying callbacks and reconciling existing work. The mock ledger demonstrates recovery across process loss. It shares the PostgreSQL instance and cannot simulate independent data loss at the provider.

Use a deterministic heuristic over the latest 50 known borrower outcomes within 30 minutes. Below 10 samples, use progressive demand. The 85% utilization target and five-second horizon make the forecast concrete. A 20% denominator floor and 50-request cap limit the response to low observed rates. Unknown calls count in full against forecast demand. These defaults may underfill the team when data is sparse or unstable. Store the inputs and intermediate values so a reviewer can reproduce the requested count. The controller still binds each approved call to a currently eligible agent and borrower.

When progressive mode already fills all safe slots, predictive pacing may deliver the same or lower utilization. The seeded runs hold conditions constant across modes while varying the policies. They use a logical clock and the existing PostgreSQL-backed path so that differences still pass through real reservations, events, and recovery. A separate contention run measures elapsed time and ownership invariants using multiple connections on a disposable PostgreSQL database.

With seed 42 in scenario A, progressive utilization was 76.9% and predictive utilization was 73.2%. Predictive requested 353 starts, 103 of which were approved. It can wait when pending calls are forecast to meet the target, even if it requests more work in total. The clean load run used four connections to create 100 distinct agent–borrower pairs and 100 ready jobs with zero invariant violations. That result covers the allocation workload at this specific size.

The answer-rate estimate is also sensitive to when outcomes enter history. In scenario D the configured rate fell from 70% to 10% at t=240 s. The observed rate was 72.7% at the change and 0% in the last 50 terminal outcomes at t=475 s. Unanswered calls can finish sooner than answered calls, so a terminal sample can lag or overshoot a change. The Safety Controller applied the same live ownership checks throughout.

A reserved agent can still disappear after dispatch. The worker requests cancellation and records an answered call without an agent when that evidence arrives. The agent-loss run introduces delayed cancellation to expose this limit. The reservation guarantee covers database ownership and eligibility when checked. It cannot guarantee physical agent presence throughout a call.

## Scaling by evidence

The only measured load size is 100 agents. Larger sizes below are starting hypotheses. Measure approval latency, PostgreSQL connection use, lock waits, and the age of the oldest ready job and inbox event before changing the design.

| Agents | Likely pressure and next step |
| ---: | --- |
| 100 | Start with the current database and worker. Measure connection setup, lock skips, and job lag under a sustained run. The clean four-connection allocation test verifies correctness for one brief batch workload. |
| 1,000 | Connection churn and per-job or per-event round trips may dominate. If they do, reuse bounded connections and batch database work while keeping ownership updates atomic. |
| 10,000 | Large campaigns can repeat candidate, snapshot, and queue scans. Use query plans and wait data to distinguish scan cost from lock contention. Add targeted indexes or split work by campaign if that addresses the measured bottleneck. |
| 100,000 | If a tuned database still cannot keep up with ownership or inbox writes, partition by an ownership boundary that keeps each agent and borrower on one shard. Route callbacks to the owning shard and coordinate any provider-wide limits separately. Verify the same invariants on each shard and test recovery across failures. |

## Assignment design answer

Use prediction to choose how much work to request and when to request it. Preserve progressive safety by committing a reservation for one eligible agent and borrower before every outbound call, with PostgreSQL deciding ownership when requests race. Keep unresolved calls reserved until the provider establishes what happened. Better forecasts can reduce idle time where the workload permits, while the controller's capacity limit stays independent of prediction quality.
