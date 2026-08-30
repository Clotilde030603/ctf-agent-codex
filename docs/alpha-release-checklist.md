# Alpha Release Checklist

- [ ] Full pytest suite passes on Python 3.12 and 3.13.
- [ ] Ruff, strict mypy, and compileall pass.
- [ ] CTF tool image builds and every required command is available as non-root.
- [ ] Playwright produces a real PNG in CI.
- [ ] `pipx install .` and `ctf-agent --help` succeed on a clean environment.
- [ ] README command smoke tests pass.
- [ ] `ctf-agent doctor` distinguishes missing CLI, stopped daemon, missing image,
      missing tools, missing Codex authentication, and missing Chromium.
- [ ] Fake CTFd and rCTF integration tests pass without live credentials.
- [ ] Resume settings, verification hashes, Accepted recovery, and evidence retry tests pass.
- [ ] No active-competition data, credentials, cookies, or private flags are packaged.
- [ ] CHANGELOG and package version are updated from the single `__version__` source.
- [ ] Linux and macOS limitations are current; native Windows remains unsupported.
- [ ] Release tag/image publication is reviewed separately. This checklist does not
      authorize merging to `main` or publishing a release.
