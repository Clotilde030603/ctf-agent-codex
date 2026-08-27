# Platform Adapters

Platform adapters isolate CTF platform behavior from solver logic.

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
    async def capture_challenge(
        self,
        challenge: Challenge,
        output: Path,
    ) -> Path: ...
    async def capture_verdict(
        self,
        challenge: Challenge,
        output: Path,
    ) -> Path: ...
```

## Priority

1. CTFd
2. Generic HTML/JSON
3. rCTF

Adapters should prefer APIs over browser DOM automation. Browser automation is reserved for login, JavaScript-only pages, session capture, evidence screenshots, and UI actions that have no API equivalent.

## Authentication

Adapter authentication must:

- reuse valid stored sessions headlessly;
- detect login completion from URL, cookies, or selectors;
- support manual first login for MFA or CAPTCHA;
- continue automatically after manual authentication completes;
- avoid plaintext credential files.

Optional OS Keychain integration may be used for platforms without MFA or CAPTCHA. The adapter must never write secrets into the repository or run artifacts.

## Scope Checks

Every adapter request must pass scope validation:

- initial allowed host comes from the challenge URL;
- attachment and remote service hosts are added explicitly;
- redirects are validated after resolution;
- out-of-scope requests fail closed;
- request metadata is written to the ledger.

## Verdicts

Submission parsing must distinguish:

- Accepted or solved;
- Wrong or incorrect;
- duplicate accepted;
- rate limited;
- authentication expired;
- challenge closed or unavailable;
- unknown verdict requiring manual inspection.

Unknown verdicts must not be reported as Accepted.
