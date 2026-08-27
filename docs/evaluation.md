# Evaluation

Benchmarks validate autonomous solving quality against retired or local CTF challenges. They must not require live private accounts or committed session data.

## Manifest

Command:

```bash
ctf-agent benchmark evals/manifest.yaml
```

The implemented manifest identifies an offline command and expected flag for each retired or local fixture. Future schema versions can add challenge URLs, expected category, flag hashes, time budgets, allowed services, and required tools.

Example:

```yaml
challenges:
  - id: local-web-001
    command: [python3, fixtures/local-web-001/solve.py]
    expected_flag: flag{retired_fixture}
```

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
