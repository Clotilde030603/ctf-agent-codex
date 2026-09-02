# Security Model

`ctf-agent-codex` is intended only for CTF competitions, retired challenges, war games, and training labs where the user has explicit authorization. It is not a general internet attack tool.

## Trust Boundaries

| Boundary | Enforcement |
| --- | --- |
| Target scope | `HostScope` starts from the challenge URL and adds only challenge-declared attachment or service hosts. |
| Private hosts | Private and loopback targets require `--allow-private-host` or `CTF_ALLOW_PRIVATE_HOSTS=true`. |
| Redirects | Redirect destinations are checked against scope after resolution. |
| Solver workspace | Model workers write only relative paths inside their lane workspace. |
| Command execution | Worker commands are argument vectors, not shell strings, and `sh`/`bash`/`zsh`/PowerShell are rejected. The worker enforces the run's container-derived capability snapshot; installed and allowed are independent states. |
| Container execution | Default worker/reproduction execution uses no network, read-only mounts where practical, CPU/memory/PID limits, and timeouts. |
| Remote challenge access | A worker can use only the structured `http_request` action. `ScopedAsyncSession` enforces the challenge/service host allowlist, redirect checks, rate limits, request budgets, and retry policy; model-supplied credential headers are rejected. The controller's `AuthSessionBroker` gives lanes opaque handles and strips credentials from cross-origin requests. |
| Raw TCP | `tcp-controller` is explicitly `unavailable`. Worker TCP actions fail without connecting or retaining model-provided payloads; Docker networking remains disabled. |
| Submission | Only the workflow submit handler can submit, and only after verification gates pass. |

## Secrets

The repository must not contain:

- CTF passwords;
- cookies;
- CSRF tokens;
- bearer tokens;
- API keys;
- Playwright storage state;
- real `state.db` files;
- unreviewed run directories.

Codex credentials are managed by the Codex CLI. The project only stores `CTF_CODEX_BINARY` and role/model settings.

## Redaction

The sanitizer removes common cookie, bearer token, API key, CSRF token, password, session, and authorization-header patterns from terminal evidence and generated write-up inputs. Credential-bearing challenge URLs are redacted before SQLite/JSONL persistence.

`--redact-flag` or `CTF_REDACT_FLAG=true` redacts the verified flag in generated Markdown, HTML, and provenance output. Durable redaction is applied at persistence boundaries, while private mode retains protected solver artifacts separately and never exposes them to public outputs or benchmark authority.

## Submission Safety

The default Wrong budget is one attempt per run: `CTF_SUBMISSION_BUDGET=1`.

The workflow blocks submission unless the candidate has:

- flag-format match;
- actionable provenance;
- fresh replay;
- blind independent verification;
- a separate verifier-model derivation that is not given the expected candidate when `backend=codex`;
- hardcode rejection;
- negative-control rejection;
- no previous Wrong verdict;
- remaining submission budget.

Unknown, pending, and rate-limited verdicts do not count as Accepted and do not authorize duplicate submission.

## Evidence Integrity

Evidence capture must create real evidence files or record explicit failures in `evidence/manifest.json`. The workflow does not create empty or fake PNG files to make a run appear successful.

The manifest records SHA-256 hashes, labels, media types, timestamps, source, producer, command metadata, model metadata when available, redaction state, and capture failures.

## Known Limits

- Pwn and reverse-engineering support is currently model-worker based and experimental.
- rCTF support is fake-integration-tested but not validated across every deployed variant.
- Generic HTML does not submit flags because it has no safe platform contract.
- Token and monetary cost reporting depend on backend-provided metrics.
- Native Windows is not validated; WSL2 is the safer Windows path.
- A bounded DNS-rechecking TCP controller proxy is not available; unrestricted container networking is not used as a substitute.
- Authentication, scoped HTTP, browser, and trusted-skill availability are reported by one controller-owned capability snapshot.
- Auth handles are controller-owned and process-local. A protected-work restart persists only the intended return state, reacquires credentials from the user/controller source into a new opaque handle, and pauses in `NEEDS_AUTHENTICATION` when that source is unavailable; cookies and handles are never persisted in run artifacts.

## Reproduction Contract

The controller reconstructs each reproduction specification from the exact successful command-report argv and the owning lane or run context. The canonical default is `python3 -I solve.py` with cwd and solver path equal to the controller-owned run root/workspace and no selected environment keys; the fixed environment contains only approved locale/path/temp keys plus `PYTHONUNBUFFERED=1`. Model-provided cwd, solver path, environment names, network policy, mounts, and authentication metadata are discarded. The canonical cwd and `solve.py` must resolve beneath the run root without symlinks; final replay uses the promoted run-root `solve.py`. Reproduction receives only a fixed minimal process environment with no controller-selected secrets. Docker replay uses `--network=none` with non-root, read-only, resource-limited execution; explicit host replay runs the unchanged argv in a fresh user and network namespace and fails closed when that isolation is unavailable.
