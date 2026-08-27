# Evaluation

Benchmarks validate autonomous solving quality against retired or local CTF challenges. They must not require live private accounts or committed session data.

## Manifest

Target command:

```bash
ctf-agent benchmark evals/manifest.yaml
```

The manifest should identify challenge URLs or local fixtures, expected category, optional known flag hash, time budget, allowed services, and required tools.

Example target shape:

```yaml
challenges:
  - id: local-web-001
    url: http://127.0.0.1:8080/challenges/1
    category: web
    time_budget_seconds: 1800
    allowed_hosts:
      - 127.0.0.1
```

This YAML is an intended contract. It should be tested once the benchmark runner exists.

## Metrics

Collect:

- `Solved@15m`
- `Solved@30m`
- `Solved@60m`
- time to Accepted
- Wrong submission count
- clean reproduction success rate
- tool call count
- model call count
- token or cost data when available
- repeat-run success rate
- write-up fact error count

## Fixtures

Recommended fixture types:

- fake CTFd server;
- local HTTP challenge service;
- static attachment-only challenges;
- mock solver challenges for state, evidence, and write-up validation.

Fixture data must not include real credentials, live cookies, private flags from active competitions, or platform session state.

## Reporting

Each benchmark run should write:

- machine-readable metrics;
- per-challenge event ledger;
- final verdict;
- reproduction result;
- write-up reviewer findings;
- failure category for unsolved challenges.
