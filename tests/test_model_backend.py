from __future__ import annotations

import asyncio
import json
import stat
from pathlib import Path

import pytest

from ctf_agent.config import Settings
from ctf_agent.models.base import ModelBackendError, ModelRequest
from ctf_agent.models.codex import CodexCliBackend
from ctf_agent.models.factory import create_codex_backend


def test_codex_backend_uses_final_message_contract(tmp_path: Path) -> None:
    executable = _write_fake_codex(
        tmp_path,
        """
import json
import pathlib
import sys

args = sys.argv[1:]
prompt = sys.stdin.read()
final = pathlib.Path(args[args.index("--output-last-message") + 1])
schema = pathlib.Path(args[args.index("--output-schema") + 1])
payload = {
    "args": args,
    "prompt": prompt,
    "schema": json.loads(schema.read_text()),
}
final.write_text(json.dumps({"content": "flag{ok}", "metadata": payload}))
""",
    )
    backend = CodexCliBackend(
        executable=str(executable),
        model="gpt-test",
        reasoning_effort="high",
        cwd=tmp_path,
        sandbox="read-only",
        timeout_seconds=5,
    )

    response = asyncio.run(
        backend.complete(
            ModelRequest(
                role="ctf-planner",
                system="Return JSON only.",
                prompt="Solve the challenge.",
                context={"url": "https://ctf.test/challenges/1"},
            )
        )
    )

    assert response.content == "flag{ok}"
    args = response.metadata["args"]
    assert args[:2] == ["exec", "--ephemeral"]
    assert ["--sandbox", "read-only"] == [args[2], args[3]]
    assert "--ignore-user-config" in args
    assert "--ignore-rules" in args
    assert "--output-schema" in args
    assert "--output-last-message" in args
    assert ["--model", "gpt-test"] == [args[args.index("--model")], args[args.index("--model") + 1]]
    assert '-c' in args
    assert 'model_reasoning_effort="high"' in args
    assert ["--cd", str(tmp_path)] == [args[args.index("--cd")], args[args.index("--cd") + 1]]
    assert args[-1] == "-"
    assert "Role:\nctf-planner" in response.metadata["prompt"]
    assert "System instructions:\nReturn JSON only." in response.metadata["prompt"]
    assert '"url": "https://ctf.test/challenges/1"' in response.metadata["prompt"]
    assert response.metadata["schema"]["required"] == ["content"]


def test_codex_backend_factory_uses_role_settings(tmp_path: Path) -> None:
    executable = _write_fake_codex(
        tmp_path,
        """
import json
import pathlib
import sys

args = sys.argv[1:]
final = pathlib.Path(args[args.index("--output-last-message") + 1])
final.write_text(json.dumps({"content": "ok", "metadata": {"args": args}}))
""",
    )
    settings = Settings(
        codex_binary=str(executable),
        solver_model="gpt-solver",
        solver_effort="xhigh",
        request_timeout_seconds=5,
    )
    backend = create_codex_backend(settings, "solver", tmp_path)

    response = asyncio.run(backend.complete(ModelRequest(prompt="solve")))

    args = response.metadata["args"]
    assert ["--model", "gpt-solver"] == [
        args[args.index("--model")],
        args[args.index("--model") + 1],
    ]
    assert 'model_reasoning_effort="xhigh"' in args
    assert ["--cd", str(tmp_path)] == [args[args.index("--cd")], args[args.index("--cd") + 1]]


def test_codex_backend_accepts_schema_json_without_content_key(tmp_path: Path) -> None:
    executable = _write_fake_codex(
        tmp_path,
        """
import json
import pathlib
import sys

args = sys.argv[1:]
final = pathlib.Path(args[args.index("--output-last-message") + 1])
final.write_text(json.dumps({"hypotheses": [{"id": "h1"}]}))
""",
    )
    schema = {
        "type": "object",
        "properties": {"hypotheses": {"type": "array"}},
        "required": ["hypotheses"],
    }

    response = asyncio.run(
        CodexCliBackend(executable=str(executable), cwd=tmp_path).complete(
            ModelRequest(prompt="Plan.", output_schema=schema)
        )
    )

    assert json.loads(response.content) == {"hypotheses": [{"id": "h1"}]}
    assert response.raw == {"hypotheses": [{"id": "h1"}]}


def test_codex_backend_rejects_oversized_prompt(tmp_path: Path) -> None:
    backend = CodexCliBackend(
        executable=str(tmp_path / "unused"),
        max_prompt_bytes=4,
    )

    with pytest.raises(ModelBackendError, match="prompt exceeds"):
        asyncio.run(backend.complete(ModelRequest(prompt="too large")))


def test_codex_backend_wraps_missing_binary() -> None:
    backend = CodexCliBackend(executable="/definitely/missing/codex")

    with pytest.raises(ModelBackendError, match="executable not found"):
        asyncio.run(backend.complete(ModelRequest(prompt="hello")))


def test_codex_backend_wraps_nonzero_exit(tmp_path: Path) -> None:
    executable = _write_fake_codex(
        tmp_path,
        """
import sys

print("boom", file=sys.stderr)
raise SystemExit(42)
""",
    )

    with pytest.raises(ModelBackendError, match="exited with 42: boom"):
        asyncio.run(
            CodexCliBackend(executable=str(executable), timeout_seconds=5).complete(
                ModelRequest(prompt="hello")
            )
        )


def test_codex_backend_times_out_and_kills_process(tmp_path: Path) -> None:
    executable = _write_fake_codex(
        tmp_path,
        """
import time

time.sleep(5)
""",
    )

    with pytest.raises(ModelBackendError, match="timed out"):
        asyncio.run(
            CodexCliBackend(executable=str(executable), timeout_seconds=0.1).complete(
                ModelRequest(prompt="hello")
            )
        )


def test_codex_backend_rejects_missing_final_file(tmp_path: Path) -> None:
    executable = _write_fake_codex(tmp_path, "print('no final file')\n")

    with pytest.raises(ModelBackendError, match="did not write final message"):
        asyncio.run(
            CodexCliBackend(executable=str(executable), timeout_seconds=5).complete(
                ModelRequest(prompt="hello")
            )
        )


def test_codex_backend_rejects_malformed_final_message(tmp_path: Path) -> None:
    executable = _write_fake_codex(
        tmp_path,
        """
import pathlib
import sys

args = sys.argv[1:]
final = pathlib.Path(args[args.index("--output-last-message") + 1])
final.write_text("not-json")
""",
    )

    with pytest.raises(ModelBackendError, match="valid JSON"):
        asyncio.run(
            CodexCliBackend(executable=str(executable), timeout_seconds=5).complete(
                ModelRequest(prompt="hello")
            )
        )


def test_codex_backend_rejects_oversized_final_message(tmp_path: Path) -> None:
    executable = _write_fake_codex(
        tmp_path,
        """
import pathlib
import sys

args = sys.argv[1:]
final = pathlib.Path(args[args.index("--output-last-message") + 1])
final.write_text('{"content": "' + ("x" * 200) + '"}')
""",
    )

    with pytest.raises(ModelBackendError, match="final message exceeds"):
        asyncio.run(
            CodexCliBackend(
                executable=str(executable),
                timeout_seconds=5,
                max_output_bytes=50,
            ).complete(ModelRequest(prompt="hello"))
        )


def _write_fake_codex(tmp_path: Path, body: str) -> Path:
    executable = tmp_path / "fake_codex.py"
    executable.write_text("#!/usr/bin/env python3\n" + body.lstrip(), encoding="utf-8")
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    return executable
