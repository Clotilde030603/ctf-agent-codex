---
name: ctf-crypto-binary
description: Implementation-focused crypto CTF guidance for custom binaries, side channels, encodings, and protocol bugs.
---

# CTF Crypto Binary

Use this skill for crypto challenges where the weakness is in implementation, binary behavior, protocol parsing, or custom encoding.

## Focus

- Identify the implementation boundary before choosing math attacks.
- Inspect constants, mode usage, nonce handling, padding, and parsing.
- Preserve input/output examples and protocol traces.
- Reproduce the bug with a solver script.

## References

Read [references/crypto-binary-checklist.md](references/crypto-binary-checklist.md) when executing an implementation crypto hypothesis.
