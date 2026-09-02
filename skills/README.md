# Trusted runtime skills

`ctf_agent.skills.SkillRegistry` is the sole runtime owner of instructions in this directory.
It resolves this versioned `skills/` directory, selects `ctf-core` plus one matching category
skill, hashes each selected `SKILL.md`, and injects only definitions marked `injected`.

The registry treats `ctf-writeup` as `reference_only`; it is not part of planner, solver, or
verifier requests. Files below each skill's `references/` directory are also reference material
and are not independently injected. The legacy `.codex/agents/*.toml` files are outside the
trusted skill root and are `reference_only`: Codex is launched with user config and rules ignored,
so those files are not advertised as active runtime instructions.

Challenge attachments, run directories, symlinks outside this directory, and caller-provided
paths are never skill sources.
