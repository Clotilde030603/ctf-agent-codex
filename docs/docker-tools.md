# CTF Tool Image

Build the versioned local image:

```bash
docker build -t ctf-agent-codex-tools:0.1.0 \
  -f docker/ctf-tools/Dockerfile .
```

Validate the complete runtime, including daemon connectivity and container tools:

```bash
ctf-agent doctor --backend static --json
```

The doctor probes commands and versions inside the configured image, never from the
host PATH. Its typed capability snapshot records installed, policy-allowed, reachable,
authenticated, version, source, status, image digest, and reason separately. Each run
persists the same data at `artifacts/runtime-capabilities.json`; planners, workers, and
toolchain specialists consume that snapshot rather than probing independently.

The image contains Python, file, GNU binutils, ExifTool, binwalk, checksec,
foremost, and tshark. It defaults to UID/GID `10001:10001`. Worker runs add
`--network=none`, a read-only root filesystem, CPU/memory/PID limits, a bounded
tmpfs, a writable lane mount, and a read-only challenge mount. The Docker socket,
privileged mode, host networking, and host PID namespace are never provided.

Tool package versions are resolved from Debian bookworm during the build and are
printed by the CI smoke job. See `docker/ctf-tools/TOOLS.md` for provenance and the
current tool list. Installation does not grant execution permission: a command can be
installed in the image while its runtime status remains `disallowed` by policy.

If the Docker CLI exists but the daemon is stopped, `ctf-agent doctor` returns a
non-zero status. Local reproduction is used only with the explicit
`--allow-local-reproduction` option. It preserves the exact solver argv inside a fresh
user and network namespace, fails closed if namespace isolation is unavailable, and
remains weaker than the container gate's filesystem and resource controls.
