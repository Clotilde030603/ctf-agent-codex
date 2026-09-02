# Architecture

`ctf-agent-codex` is a deterministic Python controller with bounded model helpers. Python owns state transitions, scope checks, verification gates, submission decisions, resume behavior, and evidence generation. Models can propose hypotheses or choose worker actions, but they cannot skip required states, widen network scope, submit directly, or mark a candidate verified.

## Runtime Layout

```text
src/ctf_agent/
|-- cli.py
|-- config.py
|-- engine.py
|-- workflow.py
|-- workflow_parts/        # extracted state handlers
|-- context_projector/     # role-aware deterministic projection
|-- lanes/                 # durable checkpoints and lifecycle
|-- scheduler.py
|-- platforms/
|-- ingestion/
|-- triage/
|-- specialists/
|-- models/
|-- workers/
|-- verification/
|-- evidence/
`-- writeup/
```

## Controller

`engine.Controller` creates a run directory, opens `state.db` (state schema v7), appends `events.jsonl`, enforces `CTF_TOTAL_RUN_TIMEOUT_SECONDS` and `CTF_MAX_STATE_STEPS`, and advances through the registered state handlers in `workflow.AutonomousWorkflow`.

Rules:

- Only controller code changes `RunState`.
- Every state start, completion, transition, error, and resume appends an event.
- `DONE`, `READY`, and `FAILED` are terminal states.
- `READY` is the safe manual/dry-run stop after verification and before external submission.
- Wrong submissions are stored and are not resubmitted.
- Pending submissions are resolved through the platform adapter or fail closed.

## Default Workflow

The default `CTF_BACKEND=codex` path is:

```text
AutonomousWorkflow
-> ModelHypothesisPlanner
-> CodexCliBackend
-> ArtifactSignalSpecialist preflight
-> category deterministic specialist when applicable
-> Scheduler(max_workers <= 3)
-> ModelSolverSpecialist
-> WorkerCore
-> ReplayVerifier
-> BlindVerifier
-> platform submission
-> evidence/write-up/reproduction
```

`StaticHypothesisPlanner` remains as a fallback source or as the scheduler wrapper for already-created model hypotheses. Static fallback is controlled by `CTF_ALLOW_STATIC_FALLBACK`.

## Model Backend

Model backends expose `complete(ModelRequest) -> ModelResponse`. The Codex backend invokes:

```text
codex exec --ephemeral --sandbox read-only --ignore-user-config --ignore-rules
```

It writes a JSON schema to a temporary file, passes that schema through `--output-schema`, reads `--output-last-message`, validates byte limits, and turns malformed output, missing final-message files, timeouts, and non-zero Codex exits into `ModelBackendError`.

The model name and reasoning effort are not hardcoded in the backend. `models.factory.create_codex_backend()` selects role-specific settings from:

- `CTF_PLANNER_MODEL` / `CTF_PLANNER_EFFORT`
- `CTF_SOLVER_MODEL` / `CTF_SOLVER_EFFORT`
- `CTF_VERIFIER_MODEL` / `CTF_VERIFIER_EFFORT`

The project passes those strings to Codex and does not assume account-level model availability.

## Worker Lanes

`workers.WorkerCore` runs a bounded observe-decide-act loop in a lane workspace under `artifacts/lanes/<hypothesis-id>-<fingerprint>/`. Extracted lane lifecycle/checkpoint modules persist provenanced facts and CAS-backed artifact references for pause/resume and crash recovery; native model sessions are never the source of truth.

Allowed worker actions are schema-validated:

- `run`: executes an argument vector, never a shell string.
- `write_file`: writes a relative file inside the lane workspace.
- `finish`: returns facts and candidates.

Each lane has limits for steps, model calls, commands, command timeout, wall-clock time, and no-progress streaks. Duplicate command fingerprints are skipped. Command stdout, stderr, metadata, exit code, timeout status, redaction status, generated files, and controller-issued command receipts are recorded as artifacts.

Default commands are restricted to tools in the versioned
`ctf-agent-codex-tools:0.1.0` image, including Python, file, binutils, ExifTool,
binwalk, checksec, foremost, and tshark. Workers run non-root with
`--network=none`, CPU/memory/PID limits, a read-only root filesystem, a writable
lane mount, and original challenge files mounted read-only. Local execution exists
only for tests or the explicit weaker reproduction mode.

## Specialist Order

Solving starts with low-cost deterministic work:

1. `ArtifactSignalSpecialist` looks for direct preserved artifact signals.
2. `CryptoSpecialist` handles deterministic base64, hex, and single-byte XOR recovery for crypto-like classifications.
3. `ForensicsSpecialist` handles strings, metadata/tool output, nested extraction, and PNG text chunks for forensics/misc classifications.
4. `StaticWebSpecialist` extracts route, parameter, auth/session, CSRF, endpoint, and direct source flag facts from downloaded web source/assets.
5. `ModelSolverSpecialist` runs controlled model lanes when the deterministic specialists do not confirm a candidate.

Pwn and reverse-engineering deep solving currently route through the generic model worker and optional tools. They are experimental rather than production-grade category solvers.

## Platform Boundary

Adapters implement the platform contract in [platform-adapters.md](platform-adapters.md). Platform detection probes CTFd and rCTF API signatures before falling back to generic HTML. Adapters prefer HTTP APIs over browser DOM automation. Playwright is used for first login, storage-state reuse, JavaScript/session handling, and evidence screenshots. Authentication, HTTP, browser, and trusted-skill availability share the controller-owned capability snapshot.

## Verification And Evidence

Candidates move through separate checks:

```text
format_match
provenance_verified
replay_verified
independent_verified
submission_allowed
```

Replay success alone does not set `independent_verified`. `BlindVerifier` copies
only the solver and preserved source artifacts into a clean temporary directory,
runs without exposing the expected flag, rejects hardcoded solvers, and records a
separate `data_dependency_verified` negative control. A distinct Codex reviewer is
required for `independent_verified`; static mode is submission-blocked unless the
operator supplies explicit approval.

Evidence generation records real files or explicit capture failures. It does not create fake PNGs when browser or terminal capture fails.

## Write-Up Inputs

Write-ups use only recorded run facts:

- `challenge.json`
- `triage.json`
- `hypotheses.json`
- `events.jsonl`
- final `solve.py`
- submission outcome
- `evidence/manifest.json`

The generator writes `writeup.md`, `writeup.html`, and `provenance.json`. The validator checks required sections, evidence hashes, unsupported flag-looking values, generated-output provenance, and secret-like material. Controller-owned facts, CAS identities, lifecycle/frontier events, and crash migrations remain authoritative; model text is untrusted.
