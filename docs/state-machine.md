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
| `VERIFY` | Validate format, provenance, replay, hardcode checks, blind verification, and negative control. | `flag.verified` or `flag.verification_failed` events. |
| `SUBMIT` | Submit only verifier-approved candidates when `auto_submit` is true. | Submission result, pending-attempt reservation, budget update. |
| `READY` | Stop safely after verification without external submission. | `verified-candidate.json`, `flag.ready` event. |
| `EVIDENCE` | Capture challenge, exploit proof, and Accepted/Solved verdict evidence. | Evidence PNG/HTML files and manifest, or explicit capture failure. |
| `WRITEUP` | Generate Markdown, HTML, and provenance from recorded facts and validate them. | `writeup.md`, `writeup.html`, `provenance.json`. |
| `REPRODUCE` | Re-run final solver in a clean environment. | Reproduction event. |
| `DONE` | Mark completed accepted run. | Final state event. |
| `FAILED` | Stop after unrecoverable errors, timeout, or exceeded state-step budget. | Last error in state and ledger. |

## Normal Transitions

```text
AUTHENTICATE -> INGEST -> TRIAGE -> PLAN -> SOLVE -> VERIFY
-> SUBMIT -> EVIDENCE -> WRITEUP -> REPRODUCE -> DONE
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
REPRODUCE failed -> SOLVE
process interruption -> resume from last durable state
```

Unknown or rate-limited pending submissions fail closed instead of being repeated.

## Resume

`ctf-agent resume <run-id>`:

- locates `state.db` under the configured `--runs-dir`;
- loads the saved `RunRecord`;
- appends a `run.resumed` event;
- keeps credential-bearing challenge URLs redacted on disk;
- requires `--challenge-url` only when the stored URL contains a redacted query secret;
- continues from the saved state until `DONE`, `READY`, or `FAILED`.

Resume must not repeat Accepted submissions or resubmit rejected candidates.

## Stop Conditions

A run stops when:

- `DONE` is reached after Accepted evidence, write-up validation, and clean reproduction;
- `READY` is reached after verification without external submission;
- `FAILED` is reached after an unrecoverable exception, total timeout, or state-step budget exhaustion;
- scope validation blocks required network access;
- authentication cannot be established;
- all planned solving lanes fail to produce a verifiable candidate within budgets.
