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
3. `BlindVerifier`: copies the solver and preserved `files/` artifacts to a clean temporary directory, runs without giving the expected flag to the verifier, and requires the same candidate to be emitted.
4. `SolverStaticAnalyzer`: rejects solver source that hardcodes the selected candidate.
5. Negative control: runs the solver without preserved source artifacts and rejects candidates that still appear.

Only after those checks pass does the workflow set:

```text
format_match=True
provenance_verified=True
replay_verified=True
independent_verified=True
submission_allowed=True
```

## Submission Gate

Before auto-submit, every candidate must satisfy:

- format match;
- actionable provenance;
- clean replay;
- blind independent verification;
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

Accepted does not complete the run. The controller runs `solve.py` again through `reproduction.reproduce_solver()` after write-up generation. Docker is the default isolation mechanism and uses no network, resource limits, and a read-only source mount. `--allow-local-reproduction` opts into weaker host-local `python3 -I solve.py` replay.

Failure routes back to `SOLVE` because the solver is not reproducible.
