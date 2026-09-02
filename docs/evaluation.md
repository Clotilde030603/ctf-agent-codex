# Evaluation

Benchmarks validate repeatability on authorized retired or local fixtures. Manifest v2 separates harness commands from autonomous workflow runs and makes provenance explicit.

Benchmarking is an optional development/evaluation subsystem, not part of the
required solve path or ordinary pull-request CI. `.github/workflows/full-benchmark.yml`
runs the full B0-B5 evaluation by manual dispatch, nightly at `0 3 * * *`, and
when a GitHub Release is published. It is not triggered by `pull_request` or
ordinary feature-branch pushes.

## Command

```bash
ctf-agent benchmark evals/manifest.v2.yaml \
  --ablation-matrix evals/ablations.yaml \
  --output report.json
```

Use `--solve-k N` for an additional configurable solve@k. Without an ablation matrix, the command retains the v1/v2 single-manifest JSON behavior. `evals/manifest.yaml` remains a v1 harness smoke test.

## Trusted runner classification

Every challenge selects exactly one scorer-controlled execution group:

- `runner: fixture_command` scores expected command output and optional clean replay. It is harness coverage, not autonomous-solving evidence.
- `runner: autonomous_workflow` requires canonical workflow artifacts and scores those artifacts independently of command output.

`expected_solver_capability` is retained as descriptive v1 metadata. It cannot select or spoof the execution group.

## Manifest v2 metadata

```yaml
schema_version: 2
evaluation_id: frozen-evaluation-v1
dataset_revision: dataset-2026-09-01
ablation_revision: b0-b5-v1
repeat_runs: 3
agent:
  commit: 0123456789abcdef
  model: model-name
  reasoning_effort: medium
challenges:
  - id: licensed-retired-example
    case_id: licensed-retired-example
    fixture_sha256: 0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef
    solution_path: fixtures/model-example/run.py
    solution_sha256: fedcba9876543210fedcba9876543210fedcba9876543210fedcba9876543210
    runner: autonomous_workflow
    category: crypto
    difficulty:
      label: null
      source: solve_count
      source_value: 27
    availability: retired
    source: example-event
    license: MIT
    authorized_for_benchmark: true
    redistribution:
      allowed: true
      evidence_url: https://example.invalid/license
    contamination:
      status: unknown
      details: no training-corpus evidence available
    command: [python3, fixtures/model-example/run.py]
    expected_flag_sha256: 0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef
```

V2 rejects challenges that are unauthorized, lack affirmative redistribution permission evidence, omit contamination or availability metadata, or use a scalar such as `difficulty: retired`. `retired` is availability, never difficulty.

Difficulty sources mean:

- `published`: an organizer-published `easy`, `medium`, or `hard` label;
- `points`: original point value, retained in `source_value`;
- `solve_count`: published solve count, retained in `source_value`;
- `empirical`: a post-hoc stratum from a named frozen reference run;
- `unknown`: no defensible label or proxy; both label and source value are null.

Proxy and empirical values must not be presented as organizer labels. Redistribution evidence records permission to ship artifacts, not merely permission to access them.

Contamination is `controlled`, `likely_contaminated`, or `unknown`. It is disclosed with counts, rates, and interpretation; it is never folded into solve scores. Evaluation, dataset, fixture, solution, ablation, and configuration hashes are immutable identities. The runner rejects missing, duplicate, stale, or mismatched identities before execution.

## Frozen B0-B5 matrix

The matrix follows the cumulative progression in the Korean development specification,
section 14. Each condition adds one implemented subsystem while preserving every prior
correction:

- **B0 legacy**: the legacy baseline before capability correction; its legacy capability gate legitimately leaves the Python case unsolved, while later stages differ behaviorally rather than by changing that outcome;
- **B1 capability correction**: B0 plus manifest/provider-backed runtime capability correction;
- **B2 elastic budget**: B1 plus the durable budget broker, progress evidence, reserves, and reporting;
- **B3 lane continuity**: B2 plus provenanced per-lane checkpoints, CAS facts, lifecycle events, and crash recovery;
- **B4 context projection**: B3 plus deterministic role-aware final-request projection;
- **B5 adaptive frontier**: B4 plus evidence-ranked progressive deepening with active width 3 and total hypothesis pool 6.

Every condition freezes model, reasoning, tool-image digest, skills, solver, artifact, capability snapshot, and the required metrics/statistics contract,
seed, and a canonical configuration hash. The scorer applies this configuration to an
actual `AutonomousWorkflow` before launching the evaluated process, then rejects any
observed identity that differs from the frozen condition. Every run is keyed by
`(case_id, condition_id, repeat_index)`.

## Autonomous authoritative scoring

The scorer creates a private invocation nonce and the configured real `AutonomousWorkflow` benchmark authority before process launch. The nonce remains in scorer memory and is never passed to the
evaluated command. Runtime model, reasoning, feature gates, skills, seed, tool image,
solver, and artifact identities are applied to the child environment before execution.

The evaluated command may produce only untrusted candidate material such as `solve.py`
and stdout. SQLite rows, JSONL events, capability/skill artifacts, identity claims, and
cost claims written by that command have no authoritative path. The scorer independently
checks the selected source identity, solver hardcoding, positive replay, source-removal
negative control, expected candidate, final state, costs, metrics, and observed runtime
identity. A clean replay is an additional reproducibility check, not an authority token.

Self-reported metrics remain available separately for diagnostics and never contribute
to authoritative aggregates. Required reports include scorer-owned metrics and deterministic
statistics, including context byte counts; command output and self-reported values cannot
satisfy those fields. Durable workflow event latency is derived from explicit
elapsed fields or relative `created_at` timestamps. The deterministic offline pilot uses
a scorer-owned logical stage clock so repeated reports remain byte-for-byte identical.

## Solve metrics

A run is solved only after authoritative verification and replay. For each challenge:

- **solve@1** is true when the first attempt is a verified solve;
- **solve@3** is true when any of the first three attempts is a verified solve. If fewer than three attempts ran, it uses the attempts that completed.

Aggregate solve@k is reported as numerator, denominator, and rate. Paired reports provide solve@1, solve@3, configurable solve@k, per-category and per-condition summaries, and B0-relative deltas. Report JSON excludes wall-clock timestamps, UUIDs, command output, and expected flags, so equivalent invocations are byte-for-byte reproducible.

## Clean execution and hardcoded checks

Every attempt starts from a fresh fixture copy. Fixture commands may use `replay_command`; autonomous runs always replay promoted `solve.py`. Inline interpreter source, parent traversal, and commands outside the fixture root are rejected. Source checks reject raw, base64, hex, and simple constructed expected flags; these checks complement artifact/hash verification but are not a semantic proof.

## Pilot fixtures

`evals/manifest.v2.yaml` contains 12 local autonomous pilots: three each for crypto, forensics, reverse engineering, and web. The complete B0-B5 matrix produces 216 paired scorer runs at three repeats per case. Fixtures are authorized, MIT-redistributable, deterministic, network-free, and contamination-controlled.

The pilots validate evaluation plumbing and ablation identity, not frontier-model CTF ability. Offline fixture/synthetic cases must not be reported as real-model performance claims. Their model/tool usage events are deterministic scorer instrumentation, and the capability conditions intentionally share the same local solver outcome. A causal model comparison must preserve this identity and artifact contract while executing the actual frozen capabilities.

Never include live credentials, cookies, active private flags, or artifacts without redistribution permission.
