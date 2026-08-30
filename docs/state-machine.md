# State Machine

The state machine is deterministic. Model output can influence hypotheses, facts, candidates, and generated solver files, but cannot skip states or authorize submission.

## States

| State | Purpose | Required Output |
| --- | --- | --- |
| `AUTHENTICATE` | Establish or reuse a scoped platform session. | Auth session metadata, no plaintext secrets in artifacts. |
| `INGEST` | Detect the platform, fetch challenge metadata, flag policy, and attachments. | `challenge.json`, downloaded files, network events. |
| `TRIAGE` | Recursively inspect files with deterministic scans and optional local tools. | `triage.json`, artifact outputs, classification evidence. |
| `PLAN` | Generate up to three independent hypotheses through the configured planner backend, with static fallback if allowed. | `hypotheses.json`, planner source. |
| `SOLVE` | Run low-cost deterministic specialists, then controlled model-worker lanes when needed. | `artifacts/specialist-results.json`, optional promoted `solve.py`, worker artifacts. |
| `VERIFY` | Validate format, provenance, process replay, data dependency, and independent review. | Durable verification record plus `flag.verified` or failure events. |
| `REPRODUCE` | Run the verified solver in the clean Docker tool image before submission. | `solver.reproduced` event. |
| `SUBMIT` | Submit only verifier-approved candidates when `auto_submit` is true. | Submission result, pending-attempt reservation, budget update. |
| `READY` | Stop safely after verification without external submission. | `verified-candidate.json`, `flag.ready` event. |
| `EVIDENCE_PENDING` | Capture each Accepted artifact independently and preserve sanitized fallbacks. | Evidence files, manifest, and per-capture events. |
| `WRITEUP_PENDING` | Generate and validate Markdown, HTML, and provenance; remain retryable on failure. | `writeup.md`, `writeup.html`, `provenance.json`. |
| `DONE` | Mark completed accepted run. | Final state event. |
| `DONE_WITH_WARNINGS` | Accepted and documented, with non-critical capture failures recorded. | Manifest failures and fallback evidence. |
| `FAILED` | Stop after unrecoverable errors, timeout, or exceeded state-step budget. | Last error in state and ledger. |

## Normal Transitions

```text
AUTHENTICATE -> INGEST -> TRIAGE -> PLAN -> SOLVE -> VERIFY
-> REPRODUCE -> SUBMIT -> EVIDENCE_PENDING -> WRITEUP_PENDING -> DONE
```

Manual or dry-run transition:

```text
VERIFY -> SUBMIT -> READY
```

The `SUBMIT` handler creates `READY` when the run has a verified candidate but `auto_submit` is false. The CLI sets `auto_submit` false when `--dry-run` is used.

## Recovery Transitions

```text
SOLVE inconclusive -> PLAN
VERIFY failed -> PLAN
SUBMIT Wrong -> PLAN
SUBMIT repeated Wrong -> TRIAGE
SUBMIT auth expired -> AUTHENTICATE
pre-submit REPRODUCE failed -> fail closed or SOLVE before submission
Accepted evidence partial -> WRITEUP_PENDING -> DONE_WITH_WARNINGS
write-up validation failed -> remain WRITEUP_PENDING
process interruption -> resume from last durable state
```

Unknown or rate-limited pending submissions fail closed instead of being repeated.

## Resume

`ctf-agent resume <run-id>`:

- locates `state.db` under the configured `--runs-dir`;
- loads the saved `RunRecord`;
- restores the versioned settings snapshot and explicit overrides;
- appends `run.resumed` only after challenge URL validation;
- keeps credential-bearing challenge URLs redacted on disk;
- requires `--challenge-url` only when the stored URL contains a redacted query secret;
- continues until `DONE`, `DONE_WITH_WARNINGS`, `READY`, or unrecoverable `FAILED`.

`ctf-agent retry-evidence <run-id>` moves only a durably Accepted run back to
`EVIDENCE_PENDING`; it cannot reopen `SOLVE` or `SUBMIT`.

Resume must not repeat Accepted submissions or resubmit rejected candidates.

## Stop Conditions

A run stops when:

- `DONE` is reached after pre-submit reproduction, Accepted, evidence, and write-up validation;
- `DONE_WITH_WARNINGS` records Accepted with non-critical capture gaps;
- `READY` is reached after verification without external submission;
- `FAILED` is reached after an unrecoverable exception, total timeout, or state-step budget exhaustion;
- scope validation blocks required network access;
- authentication cannot be established;
- all planned solving lanes fail to produce a verifiable candidate within budgets.
