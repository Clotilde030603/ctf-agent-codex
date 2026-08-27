"""Render terminal transcripts into durable, sanitized evidence files."""

from __future__ import annotations

import subprocess
import tempfile
from dataclasses import dataclass
from html import escape
from pathlib import Path

from ctf_agent.evidence.sanitizer import SecretSanitizer


@dataclass(frozen=True)
class TerminalRenderResult:
    html_path: Path
    png_path: Path | None
    redacted: bool
    screenshot_status: str


class TerminalRenderer:
    def __init__(self, sanitizer: SecretSanitizer | None = None) -> None:
        self._sanitizer = sanitizer or SecretSanitizer()

    def render(
        self,
        transcript: str | bytes,
        output_dir: Path,
        *,
        stem: str = "02-exploit-proof",
        title: str = "Exploit proof",
        command: str | None = None,
        make_png: bool = True,
    ) -> TerminalRenderResult:
        output_dir.mkdir(parents=True, exist_ok=True)
        sanitized = self._sanitizer.sanitize(transcript)
        command_result = self._sanitizer.sanitize(command or "")

        html_path = output_dir / f"{stem}.html"
        html_path.write_text(
            self._html(title=title, command=command_result.text, transcript=sanitized.text),
            encoding="utf-8",
        )

        png_path: Path | None = None
        status = "skipped"
        if make_png:
            png_path = output_dir / f"{stem}.png"
            status = self._render_png_with_playwright(html_path, png_path)
            if status != "created":
                png_path = None

        return TerminalRenderResult(
            html_path=html_path,
            png_path=png_path,
            redacted=sanitized.redacted or command_result.redacted,
            screenshot_status=status,
        )

    def _html(self, *, title: str, command: str, transcript: str) -> str:
        command_block = f'<div class="command">$ {escape(command)}</div>' if command else ""
        return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>{escape(title)}</title>
  <style>
    :root {{ color-scheme: dark; }}
    body {{
      margin: 0;
      background: #111827;
      color: #e5e7eb;
      font: 14px/1.45 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    }}
    main {{ padding: 24px; }}
    h1 {{ margin: 0 0 16px; font: 600 18px/1.3 system-ui, sans-serif; }}
    .terminal {{
      background: #030712;
      border: 1px solid #374151;
      border-radius: 8px;
      box-shadow: 0 18px 60px rgb(0 0 0 / 0.35);
      overflow: hidden;
    }}
    .bar {{ height: 34px; background: #1f2937; border-bottom: 1px solid #374151; }}
    .command {{ padding: 14px 18px 0; color: #93c5fd; white-space: pre-wrap; }}
    pre {{ margin: 0; padding: 18px; white-space: pre-wrap; word-break: break-word; }}
  </style>
</head>
<body>
  <main>
    <h1>{escape(title)}</h1>
    <section class="terminal">
      <div class="bar"></div>
      {command_block}
      <pre>{escape(transcript)}</pre>
    </section>
  </main>
</body>
</html>
"""

    def _render_png_with_playwright(self, html_path: Path, png_path: Path) -> str:
        script = (
            "import asyncio, sys\n"
            "from pathlib import Path\n"
            "from playwright.async_api import async_playwright\n"
            "async def main():\n"
            "    html = Path(sys.argv[1]).resolve().as_uri()\n"
            "    out = sys.argv[2]\n"
            "    async with async_playwright() as p:\n"
            "        browser = await p.chromium.launch()\n"
            "        page = await browser.new_page(\n"
            "            viewport={'width': 1280, 'height': 720}, device_scale_factor=1\n"
            "        )\n"
            "        await page.goto(html)\n"
            "        await page.screenshot(path=out, full_page=True)\n"
            "        await browser.close()\n"
            "asyncio.run(main())\n"
        )
        with tempfile.NamedTemporaryFile(
            "w", suffix=".py", encoding="utf-8", delete=False
        ) as handle:
            handle.write(script)
            script_path = Path(handle.name)
        try:
            completed = subprocess.run(
                ["python3", str(script_path), str(html_path), str(png_path)],
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired):
            return "playwright-unavailable"
        finally:
            script_path.unlink(missing_ok=True)

        if completed.returncode == 0 and png_path.exists():
            return "created"
        return "playwright-unavailable"
