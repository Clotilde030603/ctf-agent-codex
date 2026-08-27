from __future__ import annotations

from pathlib import Path


class UnsafeArtifactPathError(ValueError):
    pass


def safe_existing_artifact_path(run_dir: Path, source: str) -> str:
    """Return a verified run-relative existing file path.

    Triage paths may be absolute or relative. Absolute paths must resolve under
    run_dir. Relative paths may not contain parent traversal. The returned value
    is always relative to run_dir so generated solvers remain portable and cannot
    reach outside preserved run artifacts.
    """

    if not source:
        raise UnsafeArtifactPathError("empty artifact path")

    root = run_dir.resolve()
    candidate = Path(source)
    if candidate.is_absolute():
        resolved = candidate.resolve()
    else:
        if any(part == ".." for part in candidate.parts):
            raise UnsafeArtifactPathError(f"relative artifact path escapes run_dir: {source}")
        resolved = (root / candidate).resolve()

    if resolved != root and root not in resolved.parents:
        raise UnsafeArtifactPathError(f"artifact path is outside run_dir: {source}")
    if not resolved.is_file():
        raise UnsafeArtifactPathError(f"artifact path is not an existing file: {source}")
    return resolved.relative_to(root).as_posix()
