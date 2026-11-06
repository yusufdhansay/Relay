# Relay security

Relay treats users, tool outputs, and model outputs as untrusted. Critical controls run in code.

## Threat model

| Threat | Example | Control | Test |
|---|---|---|---|
| Model runs a risky action without approval | Plan includes a restart | Guard needs an APPROVED approval with a matching hash. Registry checks the permit | `test_unapproved_change_never_reaches_the_tool` |
| Arguments changed after approval | Approve auth-service, run payment-service | Approval is bound to a SHA-256 of tool and arguments | `test_arguments_cannot_change_after_approval` |
| Model approves its own action | Output contains `"approved": true` | Output models forbid extra fields. Agents have no approval path. Non-human identities are rejected | `test_planner_cannot_mark_actions_approved`, `test_non_human_identities_cannot_approve` |
| Model relaxes policy | Reviewer returns `verdict: ALLOW` | Reviewer output cannot carry a verdict. It can only escalate | `test_policy_review_cannot_relax_approval` |
| Model invents a tool | `ssh_exec` | Planner validation and rule R1 | `test_planner_cannot_invent_tools` |
| Shell-like arguments | `"payment-service; rm -rf /"` | Strict patterns on service names | `test_injection_shaped_arguments_are_rejected` |
| Data deletion | "Delete old logs" | Critical tools always blocked (R2) | `test_delete_request_is_blocked_and_never_runs` |
| Read-only request ignored | Model misses "do not make any changes" | Regex detection in code, stricter answer wins. R3 blocks changes | `test_read_only_holds_even_if_model_ignores_it` |
| Prompt injection in tool output | Log line says "ignore previous instructions" | Output passed as data. Flagged in payload and audit. Policy and approvals still apply | `test_injected_log_instructions_cannot_trigger_actions` |
| Model writes workflow status | Triage returns `"status": "COMPLETED"` | Extra fields forbidden. Only the orchestrator sets status | `test_llm_cannot_write_workflow_status` |
| False success | Verifier says achieved, service still degraded | Probe and deterministic checks must pass too | `test_verifier_cannot_declare_success_when_checks_fail` |
| Duplicate side effects | Five concurrent approvals, retries after timeout | Per-workflow lock, final decisions, idempotency keys | `test_concurrent_decisions_cause_one_restart`, `test_timeout_after_side_effect_does_not_duplicate_restart` |
| Secret leakage | API key in logs or responses | `SecretStr`, log redaction, provider errors trimmed | `test_secrets_never_reach_api_events_or_logs` |
| Internal error leakage | Exception text with secrets | Generic 500 body | `test_unexpected_errors_return_generic_500` |
| Spoofed approver | Body says `decided_by: ceo` | Identity comes from the token. Extra body fields are rejected | `test_decision_body_cannot_set_identity` |

## Secrets

- Secrets come only from the environment or `.env` (git-ignored).
- `RELAY_OPENAI_API_KEY` and `RELAY_APPROVER_TOKENS` are `SecretStr`. They never appear in `repr` or `model_dump` output.
- The log formatter redacts keys named like `api_key`, `token`, `secret`, `password`, `authorization`, and values like `sk-...` or `Bearer ...`.
- Provider errors show the HTTP status and a short error type only, never the response body.
- Approver tokens are compared in constant time.

## Input handling

- Request text: 5 to 2000 characters, trimmed, no control characters.
- Unknown fields are rejected in API bodies, tool inputs, and LLM outputs.
- Validation errors list the field and the message. They do not echo the submitted value.

## What Relay cannot do

- There is no shell tool, no code execution tool, and no network tool.
- All tools act on an in-memory simulated environment.

## Limitations

These are known gaps for a production deployment:

| Gap | Impact | Suggested fix |
|---|---|---|
| No authentication for submit, read, or cancel | Anyone who can reach the API can start or cancel workflows | Put Relay behind an identity-aware proxy, or add OAuth2/OIDC |
| Shared approver tokens | Coarse identity, manual rotation | Per-user identity from an IdP, short-lived tokens |
| No separation of duties | A requester can approve their own request | Store requester identity and forbid self-approval |
| No rate limiting | Request floods and LLM cost | Gateway rate limits, per-user quotas |
| In-process guard | Code running in the same process could bypass it | Run tools in a separate service that checks permits signed by the orchestrator |
| Prompt-injection detection is a heuristic | It flags common phrases only | It is an audit signal. Safety comes from policy and approvals |
| Audit log is append-only by convention | A database admin could edit rows | Ship events to write-once storage, or hash-chain them |
| No TLS in the app | Traffic is plain HTTP | Terminate TLS at a reverse proxy |
