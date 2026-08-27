---
name: ctf-pwn
description: Pwn CTF specialist guidance for binary provenance, checksec, primitives, pwntools solvers, and stable replay.
---

# CTF Pwn

Use this skill only for pwn or mixed challenges with native binaries or remote exploitation.

## Focus

- Preserve original binary, libc, loader, and remote endpoint details.
- Run `file`, `checksec`, imports, and strings when available.
- Verify exploitation primitives before building the final chain.
- Support local and remote modes in `solve.py`.
- Reproduce unstable exploits multiple times before verification.

## References

Read [references/pwn-checklist.md](references/pwn-checklist.md) when executing a pwn hypothesis.
