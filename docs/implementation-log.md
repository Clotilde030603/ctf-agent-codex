# Implementation Log

## Operational hardening after 0.1.0 baseline

- persisted versioned, credential-free runtime settings and resume override diffs;
- moved clean reproduction before submission and added recoverable
  `EVIDENCE_PENDING`, `WRITEUP_PENDING`, and `DONE_WITH_WARNINGS` states;
- persisted exact verification fields with solver/source SHA-256 invalidation;
- separated replay, data dependency, independent review, and submission approval;
- added the non-root `ctf-agent-codex-tools:0.1.0` image and `ctf-agent doctor`;
- expanded scoped structured HTTP actions and explicit worker/model/evidence events;
- added comparison benchmark identity/authorization groups and stronger specialist harnesses;
- expanded CI to Python 3.12/3.13, pipx, Docker tools, and Playwright screenshot smoke tests.

Branch: `feat/autonomous-ctf-agent-v2`

Each row records a tested logical milestone and the confirmed push result at the time it was completed.

| Milestone | Commit | Verification | Push |
| --- | --- | --- | --- |
| Package, state/event foundation, docs, Codex config, skills | `1c018b9` | 4 focused tests; `git diff --check` | pushed to `origin/feat/autonomous-ctf-agent-v2` |
| CTFd/generic/rCTF adapter surface, scoped session, safe downloader | `343aa04` | 10 tests; Ruff; mypy | pushed |
| Recursive triage and deterministic classification | `337e893` | 6 tests; Ruff; mypy | pushed |
| Hypothesis scheduler, Codex CLI backend, Claude stub | `ec0b988` | 7 tests; Ruff; mypy | pushed |
| Flag candidate verification, replay, independent gate, budget | `555789e` | 9 tests; Ruff | pushed |
| Evidence sanitizer/manifest/rendering and fact-bound write-up | `8890617` | 4 tests; Ruff; mypy | pushed |
| Integrated state workflow, automatic submit, Playwright session/capture | `99e2f54` | full 44-test suite; Ruff; strict mypy; compileall | pushed |
| Model planner workflow wiring | `9439524` | model workflow tests | pushed |
| Sandboxed model solver workers | `269f806` | worker tests | pushed |
| Category-aware solving specialists | `2a9f2d6` | category specialist tests | pushed |
| Blind independent flag verification | `5e8e6aa` | blind verification tests | pushed |
| rCTF platform integration | `5406379` | fake rCTF integration tests | pushed |
| Accepted-solve evidence and Markdown/HTML/provenance write-ups | `b21d3c8` | write-up format and evidence tests | pushed |
| Runtime execution budgets | `51f7644` | budget enforcement tests | pushed |
| Repeatable clean benchmark runner | `355e9be` | benchmark tests | pushed |

The final test/documentation commit is recorded by its Git history because a commit cannot include its own hash.

An independent post-implementation review then hardened the external submission crash window with durable pending attempts, restored VERIFY failure recovery transitions, redacted sensitive URL query data at the persistence boundary, and made Docker reproduction fail closed unless local replay is explicitly enabled.

This documentation pass is intentionally uncommitted. It updates the public guides to match the branch head above, including role-specific context budgets, six-total/three-active frontier semantics, scorer-owned AutonomousWorkflow authority, exact fail-closed reproduction, packaged skills, and restart-safe authentication.

The cumulative benchmark progression is fixed as: B0 legacy -> B1 capability correction -> B2 elastic budget -> B3 lane continuity -> B4 context projection -> B5 adaptive frontier. Budget extensions require persisted `ProgressEvidence`, preserve verifier/planner reserves, and appear in budget reports. Lane facts and artifacts are provenance/CAS-backed, with lifecycle, frontier, and crash-recovery events. Durable run state is schema v7; run settings snapshots accept schema versions 1 and 2 through the migration path; the extracted `workflow_parts`, `context_projector`, and `lanes` modules own the corresponding state boundaries. Controller command receipts, durable redaction/private modes, and the real reauthentication route are part of the persisted lifecycle.
