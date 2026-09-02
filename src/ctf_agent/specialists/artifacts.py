"""Run-relative artifact discovery for specialist result payloads."""

from pathlib import Path
from typing import assert_never

type JsonValue = str | int | float | bool | None | list[JsonValue] | dict[str, JsonValue]


def result_artifacts(run_dir: Path, payload: JsonValue) -> list[str]:
    root = run_dir.resolve()
    paths: list[str] = []

    def visit(value: JsonValue) -> None:
        match value:
            case dict() as items:
                for key, item in items.items():
                    if key.endswith("_artifact") or key in {
                        "written_path",
                        "written_files",
                    }:
                        visit(item)
                    elif isinstance(item, dict | list):
                        visit(item)
            case list() as items:
                for item in items:
                    visit(item)
            case str() as item:
                path = Path(item)
                if any(part == ".." for part in path.parts):
                    return
                resolved = (path if path.is_absolute() else root / path).resolve()
                if not resolved.is_file() or (resolved != root and root not in resolved.parents):
                    return
                relative = resolved.relative_to(root).as_posix()
                if relative not in paths:
                    paths.append(relative)
            case int() | float() | bool() | None:
                return
            case unreachable:
                assert_never(unreachable)
    visit(payload)
    return paths
