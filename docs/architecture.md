# Architecture

`ctf-agent-codex` is controlled by deterministic Python code. LLM calls are bounded helpers: they classify, plan, summarize, solve within a lane, verify derivations, or review write-ups. They do not choose state transitions, bypass scope checks, submit flags directly, or read unrestricted raw logs.

## Runtime Layout

Target package structure:

```text
src/ctf_agent/
|-- cli.py
|-- config.py
|-- engine.py
|-- state.py
|-- scheduler.py
|-- events.py
|-- scope.py
|-- platforms/
|-- ingestion/
|-- triage/
|-- specialists/
|-- models/
|-- verification/
|-- evidence/
`-- writeup/
```

## Controller

`engine.py` owns the run. It loads configuration, creates the run directory, opens SQLite state, appends ledger events, and advances through the state machine.

Rules:

- Only controller code changes state.
- Every transition writes an event.
- Every resumable task has an idempotency key.
- Work that already completed successfully is not repeated on resume.
- Wrong submissions become planner evidence and are never retried.

## Event Ledger

Each run keeps both:

- `state.db`: queryable current state, checkpoints, candidates, budgets, and verified facts.
- `events.jsonl`: append-only timeline for auditability and write-up inputs.

Event records should include state transition, command, cwd, start/end times, exit code, artifact paths, network metadata, hypothesis updates, candidate decisions, submissions, evidence, and write-up review results.

## Platform Boundary

Adapters implement the platform contract in [platform-adapters.md](platform-adapters.md). Prefer platform APIs over browser DOM automation. Playwright is an authentication, JavaScript, and evidence tool, not the default HTTP engine.

## Triage Before Reasoning

The triage pipeline runs deterministic scans before any model inspects challenge content:

- recursive file walk;
- SHA-256, size, MIME, magic, entropy;
- safe archive extraction with depth, size, and path checks;
- strings, URLs, IPs, flag-like patterns, constants;
- optional tools such as `file`, `strings`, `exiftool`, `binwalk`, and `checksec`;
- artifact paths for full raw output.

Models receive summaries and artifact references, not unbounded raw dumps.

## Hypothesis Scheduler

The planner creates at most three independent hypotheses. The scheduler runs only independent lanes in parallel. Lanes that need to edit files use isolated working directories.

Specialists return structured results:

```json
{
  "hypothesis_id": "H1",
  "status": "confirmed",
  "facts": [],
  "artifacts": [],
  "commands": [],
  "reproduction_command": "",
  "flag_candidates": [],
  "next_action": "",
  "confidence": 0.0
}
```

The scheduler does not stop other lanes just because a flag-like string appears. Unneeded lanes stop only after verifier approval.

## Model Backend

Model backends expose one interface:

```python
class ModelBackend(Protocol):
    async def run_agent(
        self,
        role: str,
        context: dict,
        output_schema: type[BaseModel],
    ) -> BaseModel: ...
```

The first required backend is Codex. Additional backends, such as Claude, should use the same interface or provide a tested stub until connected.

## Evidence And Write-Up

Evidence generation is fact-bound:

- screenshot the challenge page;
- render sanitized exploit output;
- screenshot Accepted/Solved verdict;
- write `evidence/manifest.json` with SHA-256, type, event ID, timestamp, and sanitizer status.

Write-ups may use only recorded inputs: challenge metadata, triage results, event ledger, verified database facts, final solver, verifier result, Accepted result, and evidence manifest.
