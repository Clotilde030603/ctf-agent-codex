from pathlib import Path

import pytest

from ctf_agent.reproduction import reproduce_solver


@pytest.mark.asyncio
async def test_reproduction_fails_closed_when_docker_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "solve.py").write_text("print('flag{local}')\n", encoding="utf-8")
    monkeypatch.setattr("ctf_agent.reproduction.shutil.which", lambda _name: None)

    result = await reproduce_solver(tmp_path, "flag{local}")

    assert result.success is False
    assert result.exit_code == 127
    assert "Docker is unavailable" in result.stderr


@pytest.mark.asyncio
async def test_local_reproduction_requires_explicit_opt_in(tmp_path: Path) -> None:
    (tmp_path / "solve.py").write_text("print('flag{local}')\n", encoding="utf-8")

    result = await reproduce_solver(tmp_path, "flag{local}", use_docker=False)

    assert result.success is True
