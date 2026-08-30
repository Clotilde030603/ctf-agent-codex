# Evaluation

Benchmarks validate repeatability on retired or local CTF fixtures. They do not measure live CTF performance unless the fixture author provides an authorized local target and event data.

## Command

```bash
ctf-agent benchmark evals/manifest.yaml
```

The command prints a JSON report to stdout.

## Manifest

The implemented manifest supports global repeat and budget defaults plus per-challenge overrides:

```yaml
repeat_runs: 3
timeout_seconds: 30
total_budget_seconds: 300
challenges:
  - id: local-retired-warmup
    category: warmup
    difficulty: retired
    command: [python3, fixtures/retired-warmup/solve.py]
    expected_flag: flag{retired_fixture_only}
    clean_mode: local
```

Per-challenge fields include:

- `id`
- `category`
- `difficulty`
- `command`
- `expected_flag` or `expected_flag_sha256`
- `repeat_runs`
- `timeout_seconds`
- `total_budget_seconds`
- `workdir`
- `source_files`
- `metrics_file`
- `events_file`
- `clean_replay`
- `clean_mode`
- `replay_command`
- `docker_image`

Benchmark commands must reference fixture scripts inside the fresh work directory. Inline interpreter execution and path traversal are rejected.

## Clean Replay

Each repeat starts from a fresh copy of the fixture directory. When `clean_replay` is true and the fixture command succeeds, the runner executes `replay_command` or `command` again in a clean copied directory. `clean_mode: docker` uses a no-network Docker command when Docker is available; otherwise that replay is marked skipped with a reason.

Older README text said the benchmark does not perform a separate clean-environment replay. That statement is stale.

## Hardcoded Solver Check

If `expected_flag` is present, benchmark rejects solver sources that directly embed the raw flag, base64 flag, hex flag, or Python string constants that construct those forms. This check is local and deterministic; it is not a full semantic proof that a solver is data-dependent.

## Metrics

The JSON report includes:

- solve rate;
- fixture command success rate;
- clean reproduction rate;
- Wrong submission count;
- model call count;
- tool call count;
- hallucinated candidate rate;
- median time to first candidate;
- median time to Accepted;
- replay verification rate;
- independent verification rate;
- resume verification rate;
- write-up validation rate;
- per-challenge repeat records.

Official metrics are derived only from scorer-owned command execution and clean replay observations. When `metrics_source: self_reported` is set, optional `benchmark-metrics.json` or `events.jsonl` values are stored separately under `self_reported_metrics`; they do not contribute to aggregate counts or rates. Token and monetary cost are therefore not authoritative benchmark metrics in this release.

## Fixtures

Recommended fixture types:

- fake CTFd server;
- fake rCTF server;
- local HTTP challenge service;
- static attachment-only challenges;
- mock solver challenges for state, evidence, and write-up validation.

Fixture data must not include real credentials, live cookies, private flags from active competitions, or platform session state. Do not commit copyrighted challenge data unless the license permits redistribution.
