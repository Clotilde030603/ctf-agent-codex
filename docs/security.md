# Security

The agent is for authorized CTF challenges only. It must fail closed when scope, credentials, or submission policy are unclear.

## Scope

Allowed network targets are:

- the original challenge host;
- challenge attachment hosts discovered through the platform;
- remote service hosts explicitly recorded in scope.

Forbidden behavior:

- wildcard internet scanning;
- following redirects to unapproved hosts;
- unbounded crawling;
- silent use of unrelated credentials;
- solver containers with unrestricted network access unless the service host is in scope.

## Secrets

Never commit:

- passwords;
- API keys;
- cookies;
- CSRF tokens;
- browser profiles;
- Playwright storage state;
- `state.db` files from real runs;
- terminal logs containing credentials.

Use OS Keychain or user-controlled environment variables for credentials when automatic login is allowed. Store session artifacts outside Git-tracked files.

## Sanitization

Before rendering terminal evidence or writing logs intended for write-up use, redact:

- cookies;
- bearer tokens;
- API keys;
- CSRF tokens;
- passwords;
- private session IDs;
- authorization headers.

The evidence manifest records whether sanitization ran. Before SQLite or JSONL persistence, signed URL query parameters such as tokens, signatures, credentials, sessions, CSRF values, and API keys are replaced with `REDACTED`.

## Sandbox

Solver execution must enforce:

- CPU limit;
- memory limit;
- process limit;
- wall-clock timeout;
- controlled working directory;
- explicit network allowlist;
- read-only original artifacts where practical.

Archive extraction must prevent path traversal, zip bombs, symlink escapes, and excessive recursion.

## External Skills And Tools

Do not auto-install external CTF skills or tools at runtime without review. If external references are used, record source, license, version or commit, purpose, and whether they are instruction-only or executable.
