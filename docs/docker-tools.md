# CTF Tool Image

Build the versioned local image:

```bash
docker build -t ctf-agent-codex-tools:0.1.0 \
  -f docker/ctf-tools/Dockerfile .
```

Validate the complete runtime, including daemon connectivity and container tools:

```bash
ctf-agent doctor --backend static
```

The image contains Python, file, GNU binutils, ExifTool, binwalk, checksec,
foremost, and tshark. It defaults to UID/GID `10001:10001`. Worker runs add
`--network=none`, a read-only root filesystem, CPU/memory/PID limits, a bounded
tmpfs, a writable lane mount, and a read-only challenge mount. The Docker socket,
privileged mode, host networking, and host PID namespace are never provided.

Tool package versions are resolved from Debian bookworm during the build and are
printed by the CI smoke job. See `docker/ctf-tools/TOOLS.md` for provenance and the
current tool list.

If the Docker CLI exists but the daemon is stopped, `ctf-agent doctor` returns a
non-zero status. Local reproduction is used only with the explicit
`--allow-local-reproduction` option and is weaker than the container gate.
