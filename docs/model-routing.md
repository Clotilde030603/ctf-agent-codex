# Model Routing

Model routing is configuration-driven. The code passes user-supplied model identifiers and reasoning-effort strings to the Codex CLI; it does not assume that any model is available for a given account.

## Roles

| Role | Setting | Used by | Purpose |
| --- | --- | --- | --- |
| Planner | `CTF_PLANNER_MODEL`, `CTF_PLANNER_EFFORT` | `ModelHypothesisPlanner` | Creates up to six total frontier hypotheses from challenge, flag policy, triage, files, services, and prior failures; at most three lanes are active concurrently. |
| Solver | `CTF_SOLVER_MODEL`, `CTF_SOLVER_EFFORT` | `ModelSolverSpecialist` / `WorkerCore` | Chooses bounded worker actions, writes solver files, runs allowlisted tools, records facts, and emits candidates. |
| Verifier | `CTF_VERIFIER_MODEL`, `CTF_VERIFIER_EFFORT` | `ModelBlindReviewer` | Re-derives candidates in a clean directory from only `solve.py`, original files, file hashes, and the flag policy; it is not given the solver candidate. |

CLI flags override the same settings for a run:

```bash
ctf-agent solve "<challenge-url>" \
  --backend codex \
  --planner-model "<planner-model>" \
  --solver-model "<solver-model>" \
  --reviewer-model "<reviewer-model>" \
  --reasoning-effort high \
  --max-workers 3
```

`--reasoning-effort` remains a shorthand for all roles. `--planner-effort`,
`--solver-effort`, and `--reviewer-effort` override it per role and are validated
before execution. These values are persisted in the run snapshot and can be
overridden explicitly by `resume`.

## Default Path

With `CTF_BACKEND=codex`, `AutonomousWorkflow.plan()` calls `ModelHypothesisPlanner` unless the model-call budget is exhausted or the backend fails and `CTF_ALLOW_STATIC_FALLBACK=true`. `AutonomousWorkflow.solve()` runs deterministic preflight/category specialists, passes those results to the model, and always starts controlled model-worker lanes. `AutonomousWorkflow.verify()` then requires both deterministic blind replay and a separate verifier-model derivation. Planner, solver, and verifier calls share the run-wide model-call budget.

With `CTF_BACKEND=static`, the workflow skips Codex planner/worker calls and uses deterministic fallback behavior only.

## Budgets

| Setting | Default | Effect |
| --- | --- | --- |
| `CTF_MODEL_TIMEOUT_SECONDS` | `180` | Timeout for each Codex CLI backend call. |
| `CTF_MODEL_CALL_BUDGET` | `20` | Shared run budget for planner, solver, and verifier model requests. |
| `CTF_MAX_MODEL_CONTEXT_BYTES` | `196608` | Backend hard ceiling for the final rendered request. |
| `CTF_MAX_WORKERS` | `3` | Maximum concurrent solver lanes. |
| `CTF_WORKER_MAX_STEPS` | `12` | Maximum model decisions per worker. |
| `CTF_WORKER_MAX_COMMANDS` | `8` | Maximum command executions per worker. |
| `CTF_WORKER_MAX_HTTP_REQUESTS` | `8` | Maximum host-scoped HTTP actions per worker. |
| `CTF_WORKER_WALL_TIME_SECONDS` | `600` | Per-worker wall-clock budget. |
| `CTF_WORKER_NO_PROGRESS_LIMIT` | `3` | Stops a worker after repeated no-progress actions. |

## Suggested Local Policy

For difficult web, pwn, or reverse-engineering challenges, choose a stronger security-oriented solver model if the local Codex account supports one. For orchestration, triage, and write-up review, choose a general Codex model with enough reasoning effort for structured JSON and concise plans.

The repository defaults are examples, not compatibility guarantees:

```dotenv
CTF_PLANNER_MODEL=gpt-5.6-sol
CTF_SOLVER_MODEL=gpt-5.6-sol
CTF_VERIFIER_MODEL=gpt-5.6-sol
CTF_PLANNER_EFFORT=high
CTF_SOLVER_EFFORT=xhigh
CTF_VERIFIER_EFFORT=high
```

Verify model availability with the installed Codex CLI and your account before relying on a model name in automation. The official OpenAI Codex CLI documentation is available at <https://learn.chatgpt.com/docs/codex/cli>.

## Failure Handling

The Codex backend fails closed on:

- missing executable;
- timeout;
- non-zero CLI exit;
- missing final-message file;
- malformed JSON;
- response that fails schema validation;
- prompt or output byte limits.

Every Codex call passes through the deterministic `ContextProjector`. Role-specific projected budgets are planner/replan/verifier/reviewer `131072` bytes and solver `196608` bytes; the backend hard ceiling is `196608`. Each budget covers the final rendered role, system/developer and trusted packaged-skill instructions, projected context serialization, and task. Safety, authorized scope, credential
redaction, and active hypothesis/lane sections are mandatory; projection fails closed
when those sections alone cannot fit. Each response carries a credential-free
`projection_manifest` with deterministic section actions, byte counts, policy version,
provenance/trust labels, and input/output hashes. Challenge, tool, and model text is
always serialized as untrusted data, never promoted into instruction positions.

Planner failures use static fallback only when `CTF_ALLOW_STATIC_FALLBACK=true`. Worker failures become inconclusive specialist results and are recorded in lane artifacts.

## Current Limits

- The Claude backend is a deterministic stub for tests, not a production Claude integration.
- The verifier model can inspect and execute files through its Codex sandbox; exact candidate comparison remains deterministic workflow code outside the reviewer prompt.
- Token and cost metrics are not guaranteed because they depend on backend output.
