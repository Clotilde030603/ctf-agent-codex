import json
import sys
from pathlib import Path

import pytest

import ctf_agent.reproduction as reproduction
from ctf_agent.reproduction import ReproductionSpec, reproduce_solver


def _reproduction_spec():
    spec_type = getattr(reproduction, "ReproductionSpec", None)
    assert callable(spec_type), "ReproductionSpec must be callable"
    return spec_type


def _network_capability():
    capability_type = getattr(reproduction, "NetworkCapability", None)
    assert capability_type is not None, "NetworkCapability must exist"
    return capability_type


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


@pytest.mark.asyncio
async def test_reproduction_uses_same_stdout_stderr_contract_as_replay(
    tmp_path: Path,
) -> None:
    (tmp_path / "solve.py").write_text(
        "import sys\nprint('flag{stderr}', file=sys.stderr)\n",
        encoding="utf-8",
    )

    result = await reproduce_solver(tmp_path, "flag{stderr}", use_docker=False)

    assert result.success is True


@pytest.mark.asyncio
async def test_reproduction_preserves_exact_argv_with_solver_not_last(tmp_path: Path) -> None:
    solver = tmp_path / "solve.py"
    solver.write_text(
        "import sys\nprint(' '.join(sys.argv[1:]))\nprint('flag{exact}')\n",
        encoding="utf-8",
    )
    argv = ("python3", "solve.py", "--host", "example", "--port", "31337")
    spec = _reproduction_spec()(
        argv=argv,
        cwd=tmp_path,
        env_keys=(),
        solver_path=solver,
        network=_network_capability().UNAVAILABLE,
        requires_auth_handle=False,
    )

    result = await reproduce_solver(
        tmp_path,
        "flag{exact}",
        spec=spec,
        use_docker=False,
    )

    assert tuple(result.command) == argv
    assert "--host example --port 31337" in result.stdout


def test_reproduction_spec_rejects_shell_and_requires_solve_py_anywhere(tmp_path: Path) -> None:
    solver = tmp_path / "solve.py"
    solver.write_text("print('ok')\n", encoding="utf-8")

    spec = _reproduction_spec()(
        argv=("python3", "-I", "solve.py", "--port", "31337"),
        cwd=tmp_path,
        env_keys=(),
        solver_path=solver,
        network=_network_capability().UNAVAILABLE,
        requires_auth_handle=False,
    )

    assert spec.argv[-2:] == ("--port", "31337")
    with pytest.raises(ValueError, match="solve.py"):
        _reproduction_spec()(
            argv=("python3", "solver.py"),
            cwd=tmp_path,
            env_keys=(),
            solver_path=solver,
            network=_network_capability().UNAVAILABLE,
            requires_auth_handle=False,
        )
    with pytest.raises(ValueError, match="shell"):
        _reproduction_spec()(
            argv=("bash", "-lc", "python3 solve.py"),
            cwd=tmp_path,
            env_keys=(),
            solver_path=solver,
            network=_network_capability().UNAVAILABLE,
            requires_auth_handle=False,
        )


@pytest.mark.parametrize(
    "argv",
    [
        ("python3", "-c", "print('forged')", "solve.py"),
        ("python3", "solve.py", "flag{model_payload}"),
        ("python3", "solve.py", "--candidate", "flag{model_payload}"),
        ("python3", "solve.py", "--extra-model-argument"),
    ],
)
def test_reproduction_spec_rejects_noncanonical_solver_argv(
    tmp_path: Path,
    argv: tuple[str, ...],
) -> None:
    # Given: a canonical lane solver and a vector that smuggles code or model payload data.
    solver = tmp_path / "solve.py"
    solver.write_text("print('ok')\n", encoding="utf-8")

    # When / Then: the reproduction boundary rejects anything outside its canonical grammar.
    with pytest.raises(ValueError, match="canonical"):
        ReproductionSpec(argv=argv, cwd=tmp_path, solver_path=solver)


@pytest.mark.asyncio
async def test_reproduction_rejects_forged_cwd_outside_run_root(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    outside = tmp_path / "outside"
    run_dir.mkdir()
    outside.mkdir()
    solver = run_dir / "solve.py"
    solver.write_text("print('flag{cwd}')\n", encoding="utf-8")
    spec = ReproductionSpec(argv=("python3", "solve.py"), cwd=outside, solver_path=solver)

    result = await reproduce_solver(run_dir, "flag{cwd}", spec=spec, use_docker=False)

    assert result.success is False
    assert result.exit_code == 126
    assert "cwd" in result.stderr


@pytest.mark.asyncio
async def test_reproduction_rejects_forged_solver_path(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    outside = tmp_path / "outside"
    run_dir.mkdir()
    outside.mkdir()
    (run_dir / "solve.py").write_text("print('flag{run}')\n", encoding="utf-8")
    forged_solver = outside / "solve.py"
    forged_solver.write_text("print('flag{outside}')\n", encoding="utf-8")
    spec = ReproductionSpec(argv=("python3", "solve.py"), cwd=run_dir, solver_path=forged_solver)

    result = await reproduce_solver(run_dir, "flag{outside}", spec=spec, use_docker=False)

    assert result.success is False
    assert result.exit_code == 126
    assert "solver" in result.stderr


@pytest.mark.asyncio
async def test_reproduction_rejects_symlinked_solver(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    outside = tmp_path / "outside"
    run_dir.mkdir()
    outside.mkdir()
    forged_solver = outside / "solve.py"
    forged_solver.write_text("print('flag{symlink}')\n", encoding="utf-8")
    solver = run_dir / "solve.py"
    solver.symlink_to(forged_solver)
    spec = ReproductionSpec(argv=("python3", "solve.py"), cwd=run_dir, solver_path=solver)

    result = await reproduce_solver(run_dir, "flag{symlink}", spec=spec, use_docker=False)

    assert result.success is False
    assert result.exit_code == 126
    assert "symlink" in result.stderr


def test_reproduction_spec_rejects_secret_environment_key(tmp_path: Path) -> None:
    solver = tmp_path / "solve.py"
    solver.write_text("print('flag{env}')\n", encoding="utf-8")

    with pytest.raises(ValueError, match="environment"):
        ReproductionSpec(
            argv=("python3", "solve.py"),
            cwd=tmp_path,
            env_keys=("CONTROLLER_SECRET",),
            solver_path=solver,
        )


@pytest.mark.asyncio
async def test_local_reproduction_enforces_network_unavailable(tmp_path: Path) -> None:
    solver = tmp_path / "solve.py"
    solver.write_text(
        "import socket\n"
        "try:\n"
        "    socket.create_connection(('1.1.1.1', 53), timeout=0.2)\n"
        "except OSError:\n"
        "    print('flag{network_blocked}')\n",
        encoding="utf-8",
    )
    spec = ReproductionSpec(argv=("python3", "solve.py"), cwd=tmp_path, solver_path=solver)

    result = await reproduce_solver(
        tmp_path, "flag{network_blocked}", spec=spec, use_docker=False
    )

    assert result.success is True
    assert tuple(result.command) == spec.argv


def test_cli_preserves_argv_when_network_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    solver = tmp_path / "solve.py"
    solver.write_text("print('must not run')\n", encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        ["ctf_agent.reproduction", "--solver", str(solver), "--", "--host", "example"],
    )

    exit_code = reproduction.main()

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 3
    assert payload == {
        "status": "network_unavailable",
        "argv": ["python3", "solve.py", "--host", "example"],
    }


def test_cli_fails_closed_when_host_sandbox_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    solver = tmp_path / "solve.py"
    solver.write_text("print('must not run')\n", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["ctf_agent.reproduction", "--solver", str(solver)])
    monkeypatch.setattr("ctf_agent.reproduction.shutil.which", lambda _name: None)

    exit_code = reproduction.main()

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 4
    assert payload["status"] == "reproduction_unavailable"
    assert payload["argv"] == ["python3", "solve.py"]
