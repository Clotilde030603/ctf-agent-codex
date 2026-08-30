# Security Model

`ctf-agent-codex` is intended only for CTF competitions, retired challenges, war games, and training labs where the user has explicit authorization. It is not a general internet attack tool.

## Trust Boundaries

| Boundary | Enforcement |
| --- | --- |
| Target scope | `HostScope` starts from the challenge URL and adds only challenge-declared attachment or service hosts. |
| Private hosts | Private and loopback targets require `--allow-private-host` or `CTF_ALLOW_PRIVATE_HOSTS=true`. |
| Redirects | Redirect destinations are checked against scope after resolution. |
| Solver workspace | Model workers write only relative paths inside their lane workspace. |
| Command execution | Worker commands are argument vectors, not shell strings, and `sh`/`bash`/`zsh`/PowerShell are rejected. |
| Container execution | Default worker/reproduction execution uses no network, read-only mounts where practical, CPU/memory/PID limits, and timeouts. |
| Remote challenge access | A worker can use only the structured `http_request` action. `ScopedAsyncSession` enforces the challenge/service host allowlist, redirect checks, rate limits, request budgets, and retry policy; model-supplied credential headers are rejected. |
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

`--redact-flag` or `CTF_REDACT_FLAG=true` redacts the verified flag in generated Markdown, HTML, and provenance output. It does not change private solver artifacts or platform submissions.

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
