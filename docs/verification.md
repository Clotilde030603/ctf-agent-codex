# Verification

Verification prevents false positives and unsafe submissions. A flag-like string is only a candidate until the gate proves where it came from, replays it, independently regenerates it, and confirms that submission is allowed.

## Candidate Schema

```json
{
  "value": "",
  "source_artifact": "",
  "source_location": "",
  "derivation": [],
  "solver_command": "",
  "format_match": false,
  "provenance_verified": false,
  "replay_verified": false,
  "data_dependency_verified": false,
  "independent_verified": false,
  "submission_allowed": false,
  "confidence": 0.0
}
```

These booleans are independent. Replay success alone does not imply independent verification, and a candidate without all required gates cannot be submitted automatically.

## Gate Order

`AutonomousWorkflow.verify()` evaluates each candidate through:

1. `FlagGate`: checks flag policy, length, prefix, examples, placeholder/sample text, rejected history, and actionable provenance.
2. `ReplayVerifier`: runs the promoted `solve.py` in a fresh process and requires the candidate to appear in output.
3. `BlindVerifier`: copies the solver and preserved `files/` artifacts to a clean temporary directory and reruns it without supplying an expected flag.
4. `SolverStaticAnalyzer`: rejects solver source that hardcodes the selected candidate.
5. Negative control: runs the solver without preserved source artifacts and rejects candidates that still appear.

The negative control sets `data_dependency_verified`; it does not by itself set
`independent_verified`. With `backend=codex`, a separate reviewer receives only
the policy, original files, hashes, and solver. Every reviewer finding must include
the candidate, source artifact, source location, reproduction command, and evidence.
With `backend=static`, `independent_verified` remains false.

The durable candidate record stores every boolean separately plus the solver and
source-artifact SHA-256. Resume restores those exact values and invalidates the
record if either file changes.

The full model-reviewed path sets:

```text
format_match=True
provenance_verified=True
replay_verified=True
data_dependency_verified=True
independent_verified=True
submission_allowed=True
```

## Submission Gate

Before auto-submit, every candidate must satisfy:

- format match;
- actionable provenance;
- clean replay;
- a data-dependency negative control;
- independent model review, or explicit `--approve-static-submit` operator approval;
- no hardcoded solver output;
- no negative-control match;
- no previous Wrong verdict for the same value;
- remaining submission budget.

The default `CTF_SUBMISSION_BUDGET` is `1`. A pending submission is recorded before the platform request. If the process stops after the request but before a final verdict, resume asks the adapter to resolve platform state and refuses to submit the same value again when the verdict is unknown or rate-limited.

## Wrong Handling

On Wrong:

- mark the candidate as rejected in durable state;
- prevent resubmission of the same value;
- record the platform verdict as evidence for future planning;
- route back to `PLAN`, or to `TRIAGE` after repeated Wrong results;
- preserve the remaining submission budget.

## READY And Dry Run

If `--auto-submit` is not active, or the CLI is invoked with `--dry-run`, the workflow writes `verified-candidate.json`, records `flag.ready`, and stops at `READY`. `READY` means the candidate passed verification but was not submitted externally.

## Clean Reproduction

The controller runs `solve.py` through `reproduction.reproduce_solver()` before
submission. Docker uses the versioned CTF tool image, no network, resource limits,
non-root execution, and a read-only challenge/run mount. Docker failure is
fail-closed before submission. `--allow-local-reproduction` is the only opt-in to
weaker host-local `python3 -I solve.py` replay.

After Accepted, no evidence or write-up failure can return to `SOLVE` or `SUBMIT`.
