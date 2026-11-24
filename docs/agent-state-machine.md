# Agent state machine

Phase 2 executes the `AVAILABLE → RESERVED` transition during approval. The other lifecycle edges are defined by the validator for later operational flows.

```mermaid
stateDiagram-v2
    [*] --> OFFLINE
    OFFLINE --> AVAILABLE: present and eligible
    AVAILABLE --> RESERVED: approval
    AVAILABLE --> PAUSED: pause
    AVAILABLE --> OFFLINE: departure
    RESERVED --> DIALING: start dispatch
    RESERVED --> AVAILABLE: safe release
    RESERVED --> PAUSED: pause during setup
    RESERVED --> OFFLINE: departure
    DIALING --> CONNECTED: confirmed agent bridge
    DIALING --> WRAP_UP: terminal evidence also confirms bridge
    DIALING --> AVAILABLE: definitive end without bridge
    DIALING --> PAUSED: pause during setup
    DIALING --> OFFLINE: departure
    CONNECTED --> WRAP_UP: call ends
    CONNECTED --> OFFLINE: departure
    WRAP_UP --> AVAILABLE: wrap-up ends
    WRAP_UP --> PAUSED: pause
    WRAP_UP --> OFFLINE: departure
    PAUSED --> AVAILABLE: resume while present and eligible
    PAUSED --> OFFLINE: departure
```

`OFFLINE → RESERVED` and `AVAILABLE → CONNECTED` are invalid. An answering borrower does not move an agent from `DIALING` to `CONNECTED`. That transition requires evidence the agent joined the call.

The allocator requires an `AVAILABLE` agent with a heartbeat no older than five seconds and no nonterminal attempt. It locks candidate rows and skips any held by another transaction. PostgreSQL also prevents two nonterminal attempts from owning the same agent. Only paired agents are updated. If a later write fails, the reservation is rolled back and the previous state remains visible.

Presence and ownership are separate. An agent may become `OFFLINE` before the provider confirms a call has ended. That attempt still owns the agent and borrower. A returning agent becomes `AVAILABLE` only after the unresolved attempt is cleared and the agent is otherwise eligible. If a pause or departure happens during setup, the planned worker will cancel or reconcile the attempt before releasing ownership.
