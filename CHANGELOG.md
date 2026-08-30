# Changelog

All notable changes to this project are documented here. The project follows
semantic versioning once a public release is cut; the current branch remains alpha.

## [Unreleased]

### Added

- versioned non-root CTF tool container and `ctf-agent doctor`;
- durable run-settings snapshots and role-specific resume overrides;
- recoverable Accepted evidence/write-up states and `retry-evidence`;
- persisted verification records with solver/artifact integrity hashes;
- scoped JSON/form/multipart HTTP worker actions and explicit runtime events;
- comparison benchmark identity, authorization, license, and execution groups;
- typed Pwn/Rev toolchain harnesses plus stronger Crypto/Forensics/Web observations.

### Changed

- clean reproduction now runs before submission;
- static replay no longer counts as independent verification;
- benchmark success metrics require explicit positive events;
- CI covers Python 3.12/3.13, the CTF tool image, and Playwright screenshots.

## [0.1.0] - Alpha baseline

- Initial deterministic, resumable CTF agent vertical slice.
