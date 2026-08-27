---
name: ctf-rev
description: Reverse engineering CTF guidance for validation paths, transformations, and verified solver reconstruction.
---

# CTF Rev

Use this skill only for reverse engineering or mixed challenges with binaries, bytecode, obfuscated source, or validation logic.

## Focus

- Start with strings, imports, entry points, file type, and obvious encodings.
- Use Ghidra headless or ReVa only when installed and useful.
- Extract the validation path and reconstruct transformations in Python.
- Verify the reconstructed solver against the original artifact.

## References

Read [references/rev-checklist.md](references/rev-checklist.md) when executing a reverse engineering hypothesis.
