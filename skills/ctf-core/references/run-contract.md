# Run Contract

## Required Outputs

Each accepted solve should produce:

- `challenge.json`
- `state.db`
- `triage.json`
- `hypotheses.json`
- `events.jsonl`
- `files/`
- `artifacts/`
- `solve.py`
- `requirements.txt`
- `evidence/manifest.json`
- `writeup.md`

## State Discipline

Do not skip states. On failure, return to the nearest state that can produce new evidence:

- verification failure returns to `SOLVE` or `PLAN`;
- Wrong submission returns to `PLAN`;
- reproduction failure returns to `WRITEUP` or `SOLVE`;
- interruption resumes from the latest checkpoint.

## Candidate Discipline

Do not submit candidates found only in sample text, comments, placeholders, unverified strings output, or prior Wrong submissions.
