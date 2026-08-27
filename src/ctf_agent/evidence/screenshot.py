"""Playwright screenshot helper with a non-fatal dependency fallback."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ScreenshotResult:
    path: Path | None
    status: str


async def capture_page_screenshot(page: object, output: Path) -> ScreenshotResult:
    """Capture a Playwright page object without importing Playwright at module load."""

    screenshot = getattr(page, "screenshot", None)
    if screenshot is None:
        return ScreenshotResult(path=None, status="unsupported-page-object")
    output.parent.mkdir(parents=True, exist_ok=True)
    await screenshot(path=str(output), full_page=True)
    return ScreenshotResult(path=output, status="created")
