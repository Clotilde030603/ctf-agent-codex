# Verification

Verification prevents false positives and unsafe submissions. A flag-like string is only a candidate until the gate approves it.

## Candidate Schema

```json
{
  "value": "",
  "source_artifact": "",
  "source_location": "",
  "derivation": [],
  "solver_command": "",
  "format_match": true,
  "replay_verified": false,
  "independent_verified": false,
  "confidence": 0.0
}
```

## Submission Gate

Before auto-submit, every candidate must satisfy all checks:

1. Matches the challenge flag policy.
2. Is not a sample, placeholder, comment, or decoy string.
3. Has provenance in a specific artifact, offset, line, endpoint, or command output.
4. Replays from a fresh solver process.
5. Replays against a fresh remote connection for remote challenges.
6. Passes independent verifier review.
7. Has not previously received Wrong.
8. Fits the remaining submission budget.

Any failed check blocks submission and records the reason.

## Wrong Handling

On Wrong:

- mark the candidate and derivation as rejected;
- prevent resubmission of the same value;
- record the platform verdict as evidence;
- lower confidence for related hypotheses;
- trigger re-triage and reclassification after two consecutive Wrong results;
- preserve remaining submission budget.

## Independent Review

The verifier must not share the specialist lane context that generated the candidate. It receives:

- challenge metadata;
- triage summary;
- candidate provenance;
- solver command;
- relevant artifacts;
- replay result.

The verifier approves only if the derivation is reproducible and the candidate is tied to the actual challenge.

## Clean Reproduction

Accepted does not complete the run. The controller must run `solve.py` in a clean Docker container or equivalent isolated environment, install recorded dependencies, and confirm the same flag is produced.

Failure routes back to `SOLVE` or `WRITEUP` depending on whether the solver or documentation is inconsistent.
