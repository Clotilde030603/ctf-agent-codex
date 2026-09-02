"""Shared benchmark path helpers."""

from pathlib import Path


def _under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _relative_label(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root.resolve()))
    except ValueError:
        return str(path.name)
