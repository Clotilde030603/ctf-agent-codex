# Pwn Checklist

- Record binary SHA-256, architecture, mitigations, libc, and loader.
- Keep crashes, offsets, leaks, and gadgets tied to artifacts or command output.
- Do not treat a connection, banner, or truthy process exit as success.
- Save exploit parameters and remote host details.
- Final solver should emit the candidate flag, not only open an interactive shell.
