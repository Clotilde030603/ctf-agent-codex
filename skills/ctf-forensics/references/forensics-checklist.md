# Forensics Checklist

- Hash original artifacts before extraction.
- Prevent path traversal, symlink escapes, and archive bombs.
- Record metadata, timestamps, embedded files, and protocol streams.
- Prefer deterministic extraction before speculative stego tools.
- Tie every recovered candidate to its parent artifact and offset or extraction path.
