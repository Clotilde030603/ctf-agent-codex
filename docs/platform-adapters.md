# Platform Adapters

Platform adapters isolate CTF platform behavior from solver logic. Solver lanes never submit flags directly.

## Detection

`platforms.detect.detect_platform()` probes in this order:

1. CTFd challenge or user API signatures.
2. rCTF `/api/v2/challs` and `/api/v1/challs` list signatures.
3. Generic HTML fallback.

Detection uses the scoped HTTP session, so redirects and probed hosts still pass `HostScope` validation. Unknown sites become `generic`; the agent does not guess platform-specific submit endpoints.

## Interface

```python
class PlatformAdapter(Protocol):
    async def authenticate(self) -> AuthSession: ...
    async def fetch_challenge(self, url: str) -> Challenge: ...
    async def download_attachments(
        self,
        challenge: Challenge,
        destination: Path,
    ) -> list[Artifact]: ...
    async def extract_flag_policy(
        self,
        challenge: Challenge,
    ) -> FlagPolicy: ...
    async def submit_flag(
        self,
        challenge: Challenge,
        flag: str,
    ) -> SubmissionResult: ...
    async def resolve_submission(
        self,
        challenge: Challenge,
        flag: str,
    ) -> SubmissionResult | None: ...
    async def capture_challenge(
        self,
        challenge: Challenge,
        output: Path,
    ) -> Path | None: ...
    async def capture_verdict(
        self,
        challenge: Challenge,
        output: Path,
    ) -> Path | None: ...
```

## CTFd

`CTFdPlatformAdapter` is the most complete adapter.

- Authentication checks `/api/v1/users/me`.
- If a browser storage-state path is configured, Playwright can acquire and reuse login state.
- Challenge fetch uses `/api/v1/challenges/<id>` and falls back to generic HTML for unsupported URLs.
- Attachments are made absolute and downloaded through scoped HTTP.
- Submission uses `/api/v1/challenges/attempt`.
- Pending submissions can be resolved by refetching the challenge and checking solved state.
- Challenge and verdict screenshots use scoped Playwright page capture when storage state exists.

## rCTF

`RCTFPlatformAdapter` is experimental and fake-integration-tested.

- Authentication checks `/api/v1/auth/test`.
- Challenge collection reads `/api/v2/challs` or `/api/v1/challs`.
- Challenge selection supports URL id, slug-like paths, and normalized names.
- Attachments are mapped to absolute URLs or `/api/v1/challs/<id>/files/<file>`.
- Submission uses `/api/v1/challs/<id>/submit`.
- Pending submissions can be resolved from the challenge list solved state.

The adapter does not yet cover every deployed rCTF variant, theme customization, MFA pattern, or dynamic instance lifecycle.

## Generic HTML

`GenericPlatformAdapter` supports public/basic HTTP collection only.

- It fetches the page, parses title/description/link-like attachments, and extracts any visible flag policy hints.
- It can download in-scope attachments.
- It authenticates as true because there is no platform session contract to check.
- It cannot submit flags.
- It cannot capture challenge or verdict screenshots.

If a generic target needs submission, implement a platform adapter instead of guessing a URL or form.

## Authentication

Adapter authentication must:

- reuse valid stored sessions headlessly;
- detect login completion from URL, cookies, selectors, or platform API state;
- support manual first login for MFA or CAPTCHA where the platform requires it;
- continue automatically after manual authentication completes;
- avoid plaintext credential files.

The repository does not manage Codex credentials or CTF passwords. Browser storage state can contain live cookies and must not be committed.

## Scope Checks

Every adapter request must pass scope validation:

- the initial allowed host comes from the challenge URL;
- attachment and remote service hosts are added only when challenge data declares them;
- redirects are validated after resolution;
- private and loopback hosts require explicit opt-in;
- request metadata is written to the ledger.

## Verdicts

Submission parsing distinguishes:

- Accepted or solved;
- Wrong or incorrect;
- already solved;
- rate limited;
- authentication expired;
- unknown verdict requiring manual inspection.

Unknown and rate-limited verdicts are not reported as Accepted and do not authorize duplicate submission.
