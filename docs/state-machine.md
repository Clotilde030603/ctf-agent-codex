# State Machine

The state machine is deterministic. Model output can influence facts, hypotheses, and candidate scores, but cannot skip required states or authorize submission.

## States

| State | Purpose | Required Output |
| --- | --- | --- |
| `AUTHENTICATE` | Establish or reuse a scoped platform session. | Auth session metadata, no plaintext secrets in artifacts. |
| `INGEST` | Fetch challenge metadata, flag policy, and attachments. | `challenge.json`, downloaded files, network events. |
| `TRIAGE` | Recursively inspect files and services with deterministic tools. | `triage.json`, artifact outputs, classification evidence. |
| `PLAN` | Generate up to three independent hypotheses. | `hypotheses.json`. |
| `SOLVE` | Run specialist lanes and produce solver artifacts. | Structured lane results, optional `solve.py`. |
| `VERIFY` | Validate flag provenance, replay, and independent review. | Candidate verification records. |
| `SUBMIT` | Submit only verifier-approved candidates when allowed. | Submission result and budget update. |
| `EVIDENCE` | Capture challenge, exploit proof, and verdict evidence. | Evidence files and manifest. |
| `WRITEUP` | Generate a fact-bound write-up and review it. | `writeup.md` plus reviewer record. |
| `REPRODUCE` | Re-run final solver in a clean environment. | Reproduction result. |
| `DONE` | Mark completed accepted run. | Final state event. |

## Transitions

```text
AUTHENTICATE -> INGEST -> TRIAGE -> PLAN -> SOLVE -> VERIFY
-> SUBMIT -> EVIDENCE -> WRITEUP -> REPRODUCE -> DONE
```

Failure transitions:

```text
VERIFY failed -> SOLVE or PLAN
SUBMIT Wrong -> PLAN
REPRODUCE failed -> WRITEUP or SOLVE
process interruption -> resume from last checkpoint
```

## Resume

`ctf-agent resume <run-id>` should:

- load the run directory and `state.db`;
- read the latest committed checkpoint;
- verify required artifacts still exist and match recorded hashes;
- skip idempotent tasks that completed successfully;
- continue from the next required state;
- append resume events to `events.jsonl`.

Resume must not repeat Accepted submissions or resubmit rejected candidates.

## Stop Conditions

A run stops when:

- `DONE` is reached after clean reproduction;
- submission budget is exhausted;
- scope validation blocks required network access;
- authentication requires missing human credentials, MFA, or CAPTCHA;
- all hypotheses fail and the critic cannot produce a new bounded plan.
