# Call state machine

An eligible borrower represents queued work. The attempt begins in `RESERVED` when an approval is committed. Phase 1 defines the states and validates their edges. The later worker and event handler will enforce the evidence required for each edge.

```mermaid
stateDiagram-v2
    [*] --> RESERVED
    RESERVED --> DISPATCHING: commit intent before provider start
    RESERVED --> CANCELLED: cancel before dispatch
    DISPATCHING --> UNKNOWN: ambiguous start result
    UNKNOWN --> DISPATCHING: confirmed absence and revalidated same-key retry
    DISPATCHING --> INITIATED: provider accepts
    UNKNOWN --> INITIATED: lookup confirms acceptance
    INITIATED --> RINGING: provider reports ringing
    RINGING --> ANSWERED: borrower answers
    ANSWERED --> CONNECTED: provider confirms agent bridge
    CONNECTED --> COMPLETED: call ends
    DISPATCHING --> FAILED: definitive rejection
    UNKNOWN --> CANCELLED: reconciliation confirms cancellation
    COMPLETED --> [*]
    FAILED --> [*]
    CANCELLED --> [*]
    note right of ANSWERED
        Duplicate or older progress events leave state unchanged.
        Definitive evidence can skip intermediate states.
    end note
```

The diagram shows the normal and recovery paths. The validator also allows a valid later provider milestone to arrive first.

| Current state | Additional valid targets beyond the arrows shown |
| --- | --- |
| `DISPATCHING` | `RINGING`, `ANSWERED`, `CONNECTED`, `COMPLETED`, `CANCELLED` |
| `UNKNOWN` | `RINGING`, `ANSWERED`, `CONNECTED`, `COMPLETED`, `FAILED` |
| `INITIATED` | `ANSWERED`, `CONNECTED`, `COMPLETED`, `FAILED`, `CANCELLED` |
| `RINGING` | `CONNECTED`, `COMPLETED`, `FAILED`, `CANCELLED` |
| `ANSWERED` | `COMPLETED`, `FAILED`, `CANCELLED` |
| `CONNECTED` | `FAILED`, `CANCELLED` |

A timeout can mean that the provider already created the call. The planned worker keeps both reservations, looks up the same key, and waits for authoritative evidence. A retry also requires a fresh safety check. A confirmed cancellation must prevent a late start for the cancelled key before the worker releases ownership.

The event handler will treat repeated `ANSWERED` deliveries as one state change. If `COMPLETED` arrives before `ANSWERED` and `RINGING`, the attempt stays completed and the later deliveries can only enrich its history. After a crash following `ANSWERED`, replay will resume from persisted evidence. The transition validator rejects backward edges, self-transitions, and all transitions out of terminal states. Event handling will recognize duplicates and apply them as no-ops before invoking the validator.
