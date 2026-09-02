# /// script
# requires-python = ">=3.12"
# ///
# --- How to run ---
# uv run run.py INPUT
"""Produce only an untrusted solver candidate for scorer-owned verification."""

from __future__ import annotations

import os
import sys
from pathlib import Path

_REQUIRED_RUNTIME_KEYS = (
    "CTF_BENCHMARK_MODEL",
    "CTF_BENCHMARK_REASONING",
    "CTF_BENCHMARK_TOOL_IMAGE_DIGEST",
    "CTF_BENCHMARK_SKILLS",
    "CTF_BENCHMARK_SOLVER_ID",
    "CTF_BENCHMARK_ARTIFACT_ID",
    "CTF_BENCHMARK_SEED",
    "CTF_BENCHMARK_CONFIG_SHA256",
    "CTF_BENCHMARK_CAPABILITY_MODE",
    "CTF_BENCHMARK_BUDGET_MODE",
    "CTF_BENCHMARK_LANE_CONTINUITY",
    "CTF_BENCHMARK_CONTEXT_PROJECTION",
    "CTF_BENCHMARK_FRONTIER_MODE",
)


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: run.py INPUT", file=sys.stderr)
        return 2
    missing = [key for key in _REQUIRED_RUNTIME_KEYS if key not in os.environ]
    if missing:
        print("missing scorer runtime configuration: " + ", ".join(missing), file=sys.stderr)
        return 2
    input_path = Path(sys.argv[1])
    token = input_path.read_text(encoding="utf-8").strip()
    solve_source = (
        "from pathlib import Path\n"
        f"print('flag{{' + Path({str(input_path)!r}).read_text(encoding='utf-8').strip() + '}}')\n"
    )
    Path("solve.py").write_text(solve_source, encoding="utf-8")
    print(f"flag{{{token}}}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
