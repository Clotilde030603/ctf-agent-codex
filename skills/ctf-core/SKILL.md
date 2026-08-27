---
name: ctf-core
description: Core CTF agent guidance for scope, triage, hypotheses, verification, and safe autonomous execution.
---

# CTF Core

Use this skill for all autonomous CTF runs before loading a category-specific skill.

## Rules

- Stay inside the challenge scope and explicitly allowed service hosts.
- Run deterministic triage before model-led reasoning.
- Treat flag-like strings as candidates until verification approves them.
- Preserve provenance for every fact, command, candidate, and artifact.
- Submit only after replay and independent verification pass.
- Record failures as evidence for replanning.

## References

Read [references/run-contract.md](references/run-contract.md) when planning or resuming a full solve.
