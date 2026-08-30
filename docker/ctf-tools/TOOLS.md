# CTF tool image 0.1.0

The image is built from `python:3.12.11-slim-bookworm` and installs tools from the
Debian 12 (bookworm) repositories used at image-build time:

- Python 3.12.11;
- `file`;
- GNU binutils (`strings`, `objdump`, `readelf`);
- ExifTool (`libimage-exiftool-perl`);
- `binwalk`;
- `checksec`;
- `foremost`;
- `tshark`;
- `tini`.

The published/local image tag is `ctf-agent-codex-tools:0.1.0`. CI records the
resolved package versions during every build and runs a command-availability smoke
test. The container defaults to UID/GID `10001:10001`; it does not receive the Docker
socket, privileged mode, host networking, or host PID access.
