# CTF tool image 0.1.0

The image is built from the multi-architecture index
`python:3.12.11-slim-bookworm@sha256:519591d6871b7bc437060736b9f7456b8731f1499a57e22e6c285135ae657bf7`.
Direct Debian 12 dependencies are version-pinned:

- Python 3.12.11;
- `file=1:5.44-3`;
- `binutils=2.40-2` (`strings`, `objdump`, `readelf`);
- `libimage-exiftool-perl=12.57+dfsg-1`;
- `binwalk=2.3.4+dfsg1-1`;
- `checksec=2.6.0-2`;
- `foremost=1.5.7-11+b2`;
- `tshark=4.0.17-0+deb12u3`;
- `tini=0.19.0-1+b3`;
- `ca-certificates=20250419~deb12u1`.

The published/local image tag is `ctf-agent-codex-tools:0.1.0`. CI records the
complete transitive dependency set is recorded by the CI build log; CI also runs a
command-availability smoke test. The container defaults to UID/GID `10001:10001`; it does not receive the Docker
socket, privileged mode, host networking, or host PID access.
