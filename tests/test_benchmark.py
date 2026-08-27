from __future__ import annotations

from pathlib import Path

from ctf_agent.benchmark import benchmark


def test_yaml_benchmark_manifest(tmp_path: Path) -> None:
    solver = tmp_path / "solve.py"
    solver.write_text("print('flag{benchmark_ok}')\n", encoding="utf-8")
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text(
        """challenges:
  - id: retired-warmup
    command: [python3, solve.py]
    expected_flag: flag{benchmark_ok}
""",
        encoding="utf-8",
    )

    result = benchmark(manifest)

    assert result["solved_count"] == 1
    assert result["clean_reproduction_rate"] == 1
