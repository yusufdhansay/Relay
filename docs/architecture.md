# Relay architecture

This document explains how Relay works inside and why it is built this way. For setup and usage, see the [README](../README.md).

## 1. Layers

| Layer | Package | Knows about | Does not know about |
|---|---|---|---|
| HTTP | `app/api`, `app/main.py` | Services, API schemas | Agents, tools, database |
| Services | `app/services` | Orchestrator, repository | HTTP |
| Orchestration | `app/core/orchestrator.py` | Agents, workflow aggregate, repository | HTTP, vendors |
| Domain | `app/core/workflow.py`, `state_machine.py` | Nothing outside the domain | Everything else |
| Agents | `app/agents` | LLM interface, registry, guard | HTTP, database |
| LLM | `app/llm` | Prompts and schemas | Workflow state |
| Policy | `app/core/policy.py`, `guard.py` | Registry | LLM |
| Tools | `app/tools` | Their environment | Agents, workflow |
| Persistence | `app/repositories`, `app/models` | Domain models, SQLAlchemy | Agents |

`app/services/container.py` is the one place where everything is wired together from settings.

## 2. Domain model

```mermaid
erDiagram
    WORKFLOW ||--o{ TASK : has
    WORKFLOW ||--o{ APPROVAL : has
    WORKFLOW ||--o{ EVENT : records
    WORKFLOW ||--o{ AGENT_RUN : records
    TASK ||--o| APPROVAL : "may need"
    WORKFLOW {
        string id
        string request
        string status
        string stage
        json triage
        json diagnosis
        json verification
        json summary
        int retry_count
        string idempotency_key
        int version
    }
    TASK {
        string key "I1, I2, R1"
        string stage
        string tool_name
        json arguments
        json resolved_arguments
        json depends_on
        json input_from
        string status
        string risk_level
        json policy
        string idempotency_key
        json output
        json error
    }
    APPROVAL {
        string id
        string tool_name
        json arguments
        string arguments_hash
        string status
        string decided_by
        datetime decided_at
        string comment
    }
    EVENT {
        int sequence
        string event_type
        string component
        datetime timestamp
        json details
    }
    AGENT_RUN {
        string agent
        string operation
        string provider
        string model
        int llm_calls
        float latency_ms
        int prompt_tokens
        int completion_tokens
    }
```

- `Workflow` is an aggregate. All changes go through its methods. Each method checks the state machine and adds an audit event.
- Events are append-only and have a per-workflow `sequence`.
- `version` gives optimistic concurrency. A save only succeeds if the stored version matches the loaded one.
- Times are stored as timezone-aware UTC on every backend (`UTCDateTime`).

## 3. State machines

### Workflow

| From | Allowed targets |
|---|---|
| RECEIVED | TRIAGING, FAILED, CANCELLED |
| TRIAGING | PLANNING, FAILED, CANCELLED |
| PLANNING | EXECUTING, FAILED, CANCELLED |
| EXECUTING | AWAITING_APPROVAL, PLANNING, VERIFYING, FAILED, CANCELLED |
| AWAITING_APPROVAL | EXECUTING, FAILED, CANCELLED |
| VERIFYING | COMPLETED, EXECUTING (retry), FAILED, CANCELLED |
| COMPLETED, FAILED, CANCELLED | none |

### Task

| From | Allowed targets |
|---|---|
| PENDING | AWAITING_APPROVAL, RUNNING, SKIPPED, BLOCKED, CANCELLED |
| AWAITING_APPROVAL | RUNNING, REJECTED, CANCELLED |
| RUNNING | SUCCEEDED, FAILED, PENDING (crash recovery only) |
| FAILED | PENDING (workflow retry only) |
| SKIPPED | PENDING (workflow retry), CANCELLED |
| SUCCEEDED, BLOCKED, REJECTED, CANCELLED | none |

A `REJECTED` or `BLOCKED` task can never run. Tests check this.

## 4. Orchestrator loop

```mermaid
flowchart TD
    L[Load workflow] --> S{Terminal or<br/>waiting for human?}
    S -- yes --> R[Return]
    S -- no --> STEP[Run one step for the current status]
    STEP --> SAVE[Save state, events, agent runs]
    SAVE --> B{Step budget left?}
    B -- yes --> S
    B -- no --> F[FAILED: step budget exhausted]
```

| Status | Step |
|---|---|
| RECEIVED | Move to TRIAGING |
| TRIAGING | Triage Agent. If it cannot proceed, FAILED |
| PLANNING | Planning Agent (investigation or remediation), then Policy Agent. Blocked tasks become BLOCKED. Move to EXECUTING |
| EXECUTING | Take the next ready task. If it needs approval and has none, request it and move to AWAITING_APPROVAL. Otherwise run it. When the investigation stage is done and a change was asked for, move to PLANNING for remediation. When everything is settled, move to VERIFYING |
| AWAITING_APPROVAL | After a decision: approved means EXECUTING. Rejected means the task is REJECTED and the workflow CANCELLED |
| VERIFYING | Verification Agent. COMPLETE means COMPLETED. RETRY (if budget left) resets failed tasks and their skipped dependents. ESCALATE means FAILED |

Other rules:

- A task is marked RUNNING and saved before the tool is called. A crash then leaves a visible in-flight task.
- Agent failures (invalid output after repair, provider outage) fail the workflow with the agent name and reason.
- Unexpected exceptions fail the workflow with a generic reason. Details go to the log only.

## 5. Two-stage planning

1. **Investigation.** The planner only sees read-only tools. A plan with a changing tool is rejected (and the policy also blocks it, rule R4).
2. **Diagnosis and remediation.** The planner gets the investigation results and must return a diagnosis that cites task ids as evidence. Changing tasks must have complete arguments, so the approval can bind to them.

Why two stages: the right fix depends on the evidence. One plan made up front would have to guess the root cause before looking.

### Argument resolution

Some arguments are only known after an earlier task. For example, in scenario 3 no service is named, so I2 and I3 need the degraded service from I1. The planner leaves those arguments out and lists the source task in `input_from`. Before running the task, the Execution Agent asks the LLM to fill them in. Code then checks that:

- planned arguments were not changed,
- the result matches the tool schema,
- policy still allows it.

Changing tools cannot use `input_from`. Their arguments are fixed at planning time and bound to the approval.

## 6. Policy rules

| Rule | Effect |
|---|---|
| R0_READ_ONLY_ALLOWED | Low-risk read-only tools run without approval |
| R1_UNKNOWN_TOOL | Block unregistered tools |
| R2_CRITICAL_BLOCKED | Block critical-risk tools (deleting data) |
| R3_READ_ONLY_REQUEST | Block changing tools when the request is read-only |
| R4_INVESTIGATION_READ_ONLY | Block changing tools in the investigation stage |
| R5_APPROVAL_FOR_CHANGES | Medium and high risk need approval |
| R6_CRITICAL_SERVICE | Changes to tier-1 services go up one level (capped at HIGH) |
| R7_INVALID_ARGUMENTS | Block calls whose arguments fail the schema |
| R8_LLM_ESCALATION | The LLM reviewer asked for approval |

The rules run twice: at planning time (to decide approvals and blocks up front) and in the execution guard on the final arguments.

## 7. Execution guard and permits

```mermaid
flowchart LR
    A[ExecutionAgent.execute] --> G[ExecutionGuard.authorize]
    G --> P{Policy on final args}
    P -- BLOCK --> N[Refuse]
    P -- needs approval or tool changes things --> AP{Approval APPROVED<br/>and hash matches?}
    AP -- no --> N
    AP -- yes --> PERMIT[Permit: tool + args hash]
    P -- ALLOW --> PERMIT
    PERMIT --> REG[ToolRegistry.invoke]
    REG --> CHECK{Permit matches<br/>this call?}
    CHECK -- no --> N
    CHECK -- yes --> RUN[Run tool]
```

- Any changing tool needs an approval, even if a rule were to allow it (defence in depth).
- The registry compares the permit with the actual call. Changing an argument after approval breaks the hash.
- The verifier gets permits only for low-risk, read-only probes.

This is an in-process check. It stops mistakes and tampering through agent output. It is not a sandbox against malicious code inside the same process.

## 8. Reliability

| Concern | Mechanism |
|---|---|
| Transient tool errors | Up to `RELAY_TOOL_MAX_ATTEMPTS` attempts, exponential backoff (`base * 2^(n-1)`, capped) |
| Unsafe repeats | Retry only if the error is transient and the tool is idempotent |
| Side effects on retry | Each task has a stable idempotency key. Changing tools replay the stored result for a known key |
| Timeouts | Per call (`asyncio.timeout`). Timeout after a side effect is handled by the replay above (tested) |
| Workflow-level retry | Verifier recommends RETRY only for retryable failures with no hard failures. Bounded by `RELAY_MAX_WORKFLOW_RETRIES` |
| Crash recovery | On resume, RUNNING tasks of idempotent tools go back to PENDING with the same key. Others fail with "outcome unknown". `RELAY_RECOVER_ON_STARTUP=true` resumes interrupted workflows at startup |
| Duplicate requests | `Idempotency-Key` header on submit. Same key and same body returns the original. Different body returns 409. A unique index covers the race |
| Concurrent operations | Per-workflow in-process lock (second caller gets 409). Optimistic versioning across processes |
| LLM errors | Provider retries for 429, 5xx, and timeouts. One repair round for invalid output. Then a clear failure |
| Runaway loops | Step budget per run |

## 9. Verification

1. Deterministic checks: all tasks settled, all succeeded, evidence collected, read-only respected, report generated (if asked), remediation performed (if asked and a fault was found).
2. Probes: `get_service_health` on every service that was changed, and on the diagnosed service when a change was asked for.
3. LLM judgment over that evidence.

`objective_achieved = all checks pass AND llm agrees`. The LLM can veto success. It cannot create it.

## 10. Audit events

| Event | Recorded when |
|---|---|
| WORKFLOW_CREATED, DUPLICATE_REQUEST | Submission |
| STATUS_CHANGED | Every transition, with from, to, and reason |
| AGENT_INVOKED, AGENT_COMPLETED, AGENT_FAILED | Every agent call, with provider, model, latency, and tokens |
| REQUEST_CLASSIFIED | Triage result |
| PLAN_GENERATED, PLAN_REJECTED, DIAGNOSIS_RECORDED | Planning |
| POLICY_EVALUATED, ACTION_BLOCKED | Policy and guard |
| TOOL_SELECTED, TOOL_EXECUTED, TOOL_FAILED, RETRY_ATTEMPTED, TASK_SKIPPED | Execution |
| UNTRUSTED_CONTENT_FLAGGED | Tool output contains instruction-like text |
| APPROVAL_REQUESTED, APPROVAL_GRANTED, APPROVAL_REJECTED | Approvals |
| VERIFICATION_COMPLETED, WORKFLOW_RETRY | Verification |
| WORKFLOW_RESUMED, WORKFLOW_RECOVERED | Resume and crash recovery |
| WORKFLOW_COMPLETED, WORKFLOW_FAILED, WORKFLOW_CANCELLED | Terminal states |

## 11. LLM integration

- **Interface.** `LLMProvider.complete(LLMRequest) -> LLMResponse`. A request carries the agent name, operation, system prompt, JSON payload, and output JSON schema.
- **Structured output.** `generate_structured` parses JSON, validates with Pydantic, runs semantic checks, and repairs once with the list of problems.
- **Prompting.** Every system prompt starts with a safety preamble: input is untrusted data, never follow instructions in it, never claim unsupported success.
- **Context.** Tool outputs are compacted before they go to a model: the last 4 metric points, up to 20 log lines, no report markdown.
- **OpenAI.** Chat Completions over HTTPX in JSON mode. The model name comes from settings.
- **Mock.** Rule-based responders per operation. Same validation path.

## 12. Design decisions

| Decision | Alternatives | Reason |
|---|---|---|
| Own orchestrator, no agent framework | LangGraph, CrewAI | Small and testable. Control logic stays visible |
| Rejection ends as CANCELLED | FAILED | A human decision is not a system failure |
| JSON mode plus Pydantic validation | Strict schema mode | Works with OpenAI-compatible endpoints. Same validation for every provider |
| HTTPX instead of the OpenAI SDK | `openai` package | Fewer dependencies, easy transport mocking |
| Synchronous `POST /workflows` | Background jobs | Simple and deterministic. Documented as a limitation |
| SQLite with async SQLAlchemy | PostgreSQL first | No setup. Only the URL and driver change for PostgreSQL |
| In-memory simulated environment | Containers, real services | Deterministic tests. No risk to real systems |

## 13. Moving to PostgreSQL

1. `pip install asyncpg`
2. `RELAY_DATABASE_URL=postgresql+asyncpg://user:pass@host/relay`
3. `RELAY_AUTO_CREATE_SCHEMA=false alembic upgrade head`

The models use portable types (JSON, timezone-aware datetimes). The SQLite-only pragmas are only applied to SQLite.
