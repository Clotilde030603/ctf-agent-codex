"""Offline benchmark runner for retired or local CTF challenge manifests."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any


def _load_manifest(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".json":
        return dict(json.loads(text))
    try:
        import yaml
    except ImportError as exc:
        raise RuntimeError("YAML manifests require the optional PyYAML package") from exc
    return dict(yaml.safe_load(text))


async def run_benchmark(manifest: Path) -> dict[str, Any]:
    config = _load_manifest(manifest)
    results: list[dict[str, Any]] = []
    for challenge in config.get("challenges", []):
        started = time.monotonic()
        command = list(challenge.get("command", []))
        if not command:
            results.append({"id": challenge.get("id"), "solved": False, "error": "missing command"})
            continue
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=manifest.parent,
        )
        stdout, stderr = await process.communicate()
        elapsed = time.monotonic() - started
        expected = str(challenge.get("expected_flag", ""))
        solved = process.returncode == 0 and expected and expected in stdout.decode(errors="replace")
        results.append(
            {
                "id": challenge.get("id"),
                "solved": solved,
                "seconds_to_result": elapsed,
                "solved_at_15m": solved and elapsed <= 900,
                "solved_at_30m": solved and elapsed <= 1800,
                "solved_at_60m": solved and elapsed <= 3600,
                "wrong_submissions": 0,
                "clean_reproduction": solved,
                "exit_code": process.returncode,
                "stderr": stderr.decode(errors="replace")[-1000:],
            }
        )
    solved_count = sum(bool(item["solved"]) for item in results)
    return {
        "manifest": str(manifest),
        "challenge_count": len(results),
        "solved_count": solved_count,
        "clean_reproduction_rate": solved_count / len(results) if results else 0,
        "results": results,
    }


def benchmark(manifest: Path) -> dict[str, Any]:
    return asyncio.run(run_benchmark(manifest))
