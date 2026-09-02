"""Canonical solver command grammar."""

from __future__ import annotations

import re
from pathlib import Path


def canonical_network_host(argv: tuple[str, ...]) -> str | None:
    """Return the optional host from a structurally canonical solver vector."""
    interpreter = Path(argv[0]).name if argv else ""
    if re.fullmatch(r"python(?:3(?:\.\d+)?)?", interpreter) is None:
        raise ValueError("canonical solver argv must start with a Python interpreter")
    index = 1
    if len(argv) > index and argv[index] == "-I":
        index += 1
    if len(argv) <= index or argv[index] != "solve.py":
        raise ValueError("canonical solver argv must invoke solve.py directly")
    trailing = argv[index + 1 :]
    values: dict[str, str] = {}
    trailing_index = 0
    while trailing_index < len(trailing):
        key = trailing[trailing_index]
        if key not in {"--host", "--port"} or key in values:
            raise ValueError("canonical solver argv permits only one --host and --port")
        if trailing_index + 1 >= len(trailing):
            raise ValueError("canonical solver network argument requires a value")
        values[key] = trailing[trailing_index + 1]
        trailing_index += 2
    port = values.get("--port")
    if port is not None and (not port.isdecimal() or not 1 <= int(port) <= 65535):
        raise ValueError("canonical solver port must be between 1 and 65535")
    return values.get("--host")
