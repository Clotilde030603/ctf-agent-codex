# ctf-agent-codex

`ctf-agent-codex` is the project scaffold for a Codex-controlled CTF solving agent. The intended user flow is:

```bash
ctf-agent solve "https://ctf.example.com/challenges/123" --auto-submit --writeup
```

This repository is currently documenting the implementation contract and configuration surface. Until the Python package lands, the command examples below are target behavior rather than verified runnable commands.

## Goals

For one user-provided challenge URL, the agent should:

- authenticate to the CTF platform and reuse valid sessions safely;
- collect challenge metadata, flag policy, attachments, and evidence;
- run deterministic recursive triage before model reasoning;
- generate up to three independent hypotheses and run specialist lanes;
- verify flag candidates before submission;
- submit only verified candidates when `--auto-submit` is set;
- capture challenge, exploit proof, and accepted verdict evidence;
- regenerate the final flag in a clean environment;
- write `writeup.md` only from recorded facts and artifacts;
- resume interrupted runs from the latest checkpoint.

## Architecture

The controller is a deterministic Python state machine. Models never choose state transitions directly. They receive bounded structured context and must return schema-validated results.

```text
AUTHENTICATE -> INGEST -> TRIAGE -> PLAN -> SOLVE -> VERIFY
-> SUBMIT -> EVIDENCE -> WRITEUP -> REPRODUCE -> DONE
```

Failure transitions are documented in [docs/state-machine.md](docs/state-machine.md).

Primary components:

- `engine`: owns state transitions, checkpoints, retry budgets, and resume.
- `platforms`: CTFd, generic HTML/JSON, and rCTF adapters.
- `triage`: deterministic artifact extraction, scanning, summaries, and classification.
- `specialists`: category-specific solver lanes for web, pwn, rev, crypto, forensics, misc, and mixed challenges.
- `verification`: flag provenance, replay, independent review, and submission budget checks.
- `evidence`: screenshots, sanitized terminal renders, and manifest generation.
- `writeup`: fact-bound write-up generation and review.

See [docs/architecture.md](docs/architecture.md) for the full module map.

## Security And Scope

The agent must only access:

- the host from the challenge URL;
- attachment hosts discovered from the challenge page or platform API;
- remote service hosts explicitly added to scope.

It must reject out-of-scope redirects, wildcard internet scanning, unbounded recursion, and unbounded solver execution. All network requests, commands, state transitions, candidates, submissions, and evidence files are recorded in the event ledger.

Secrets must not be committed. This includes cookies, passwords, API keys, CSRF tokens, Playwright profiles, browser storage, and session databases. See [docs/security.md](docs/security.md).

## Install

Target stack:

- Python 3.12+
- `asyncio`
- Typer-compatible CLI
- Pydantic schemas
- SQLite state and append-only `events.jsonl`
- `httpx`
- Playwright
- Jinja2
- pytest
- Ruff
- mypy or pyright
- Docker for solver sandbox and clean reproduction

Planned local setup:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
playwright install chromium
```

The setup command is not runnable until packaging files are implemented.

## Codex Authentication And Model Settings

Codex role defaults live in:

```text
.codex/config.toml
.codex/agents/
```

Model settings should be configurable through environment variables:

```text
CTF_PLANNER_MODEL
CTF_SOLVER_MODEL
CTF_VERIFIER_MODEL
CTF_PLANNER_EFFORT
CTF_SOLVER_EFFORT
CTF_VERIFIER_EFFORT
```

Do not hard-code model names throughout the codebase. Keep policy in configuration and pass role-specific context through the model backend interface.

## Playwright Login

Playwright is reserved for:

- first login;
- JavaScript-only challenge pages;
- session acquisition;
- evidence screenshots;
- UI actions that no API can perform.

Login completion must be detected from URL, cookies, or post-login selectors. The user should not need to press Enter in a terminal after logging in. If MFA or CAPTCHA appears, the first authentication can be manual; after the authenticated session is captured, the run resumes automatically.

## Docker

Docker is required for:

- running untrusted solvers with CPU, memory, process, and time limits;
- clean reproduction after Accepted;
- benchmark isolation.

Solver containers must receive only challenge artifacts, generated solver files, and explicit environment values. Session cookies and platform credentials must not enter the solver container.

## Usage

Target solve command:

```bash
ctf-agent solve "https://ctf.example.com/challenges/123" --auto-submit --writeup
```

Target resume command:

```bash
ctf-agent resume <run-id>
```

Target benchmark command:

```bash
ctf-agent benchmark evals/manifest.yaml
```

## Result Directory

Successful runs should create:

```text
runs/<event>/<challenge-id>/
|-- challenge.json
|-- state.db
|-- triage.json
|-- hypotheses.json
|-- events.jsonl
|-- files/
|-- artifacts/
|-- solve.py
|-- requirements.txt
|-- evidence/
|   |-- 01-challenge.png
|   |-- 02-exploit-proof.png
|   |-- 03-accepted.png
|   `-- manifest.json
`-- writeup.md
```

## Verification Gate

No flag candidate may be submitted unless it:

- matches the challenge flag policy;
- is not a sample, placeholder, comment, or decoy;
- has artifact or command-output provenance;
- is reproduced by a fresh solver process;
- is reproduced against a fresh remote connection when applicable;
- is reviewed by a verifier that did not generate it;
- was not previously rejected;
- fits the remaining submission budget.

Details are in [docs/verification.md](docs/verification.md).

## Evaluation

Benchmarks live under `evals/` once implemented. The runner should report `Solved@15m`, `Solved@30m`, `Solved@60m`, time to Accepted, Wrong submissions, clean reproduction rate, tool calls, model calls, token or cost data, repeat success rate, and write-up fact errors.

See [docs/evaluation.md](docs/evaluation.md).

## Known Limitations

- The current documentation/config scaffold does not implement the Python package.
- Command examples are target CLI contracts until executable code lands.
- Real CTF credentials, MFA, CAPTCHA, and platform-specific policies require user-controlled authentication.
- External tools such as Ghidra, ReVa, binwalk, exiftool, and checksec are optional capabilities and must be detected before use.
