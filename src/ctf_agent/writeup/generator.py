"""Generate evidence-backed CTF writeups."""

from __future__ import annotations

import json
from hashlib import sha256
from html import escape
from pathlib import Path
from typing import Any, cast

from ctf_agent.evidence.manifest import EvidenceManifest
from ctf_agent.evidence.provenance import build_provenance_index, save_provenance_index
from ctf_agent.evidence.sanitizer import SecretSanitizer
from ctf_agent.security import secure_write_text
from ctf_agent.writeup.context import WriteupContext, WriteupOutputs, load_writeup_context


class WriteupGenerator:
    """Creates a Markdown write-up from persisted run facts."""

    def __init__(
        self,
        *,
        template_dir: Path | None = None,
        sanitizer: SecretSanitizer | None = None,
    ) -> None:
        self._template_dir = template_dir or Path(__file__).with_name("templates")
        self._sanitizer = sanitizer or SecretSanitizer()

    def build_context(self, run_dir: Path) -> WriteupContext:
        return load_writeup_context(run_dir)

    def generate(
        self, run_dir: Path, output: Path | None = None, *, redact_flags: bool = False
    ) -> Path:
        context = self.build_context(run_dir)
        markdown = self.render(context, redact_flags=redact_flags)
        if redact_flags:
            flag = self._select_verified_flag(
                context.events, context.evidence, context.challenge
            )
            markdown = self._redact_flag_text(markdown, flag, True)
        sanitized = self._sanitizer.sanitize(markdown).text
        output_path = output or run_dir / "writeup.md"
        secure_write_text(output_path, sanitized)
        return output_path

    def generate_all(self, run_dir: Path, *, redact_flags: bool = True) -> WriteupOutputs:
        context = self.build_context(run_dir)
        markdown_path = run_dir / "writeup.md"
        html_path = run_dir / "writeup.html"
        provenance_path = run_dir / "provenance.json"

        markdown = self.render(context, redact_flags=redact_flags)
        html = self.render_html(context, redact_flags=redact_flags)
        flag = self._select_verified_flag(context.events, context.evidence, context.challenge)
        if redact_flags:
            markdown = self._redact_flag_text(markdown, flag, True)
            html = self._redact_flag_text(html, flag, True)
        secure_write_text(markdown_path, self._sanitizer.sanitize(markdown).text)
        secure_write_text(html_path, self._sanitizer.sanitize(html).text)

        flag_reference = self._flag_reference(flag, redact_flags=redact_flags)
        source_files = [
            run_dir / "challenge.json",
            run_dir / "triage.json",
            run_dir / "hypotheses.json",
            run_dir / "events.jsonl",
            run_dir / "evidence" / "manifest.json",
        ]
        index = build_provenance_index(
            context.evidence,
            root=run_dir,
            source_files=source_files,
            generated_outputs=[
                (markdown_path, "text/markdown", "writeup-markdown"),
                (html_path, "text/html", "writeup-html"),
            ],
            flag_reference=flag_reference,
            metadata={
                "generator": "ctf_agent.writeup.WriteupGenerator",
                "redact_flags": redact_flags,
            },
        )
        index["path"] = provenance_path.relative_to(run_dir).as_posix()
        index = self._redact_provenance(index, flag, flag_reference, redact_flags)
        save_provenance_index(index, provenance_path)
        return WriteupOutputs(markdown_path, html_path, provenance_path)

    def render(self, context: WriteupContext, *, redact_flags: bool = False) -> str:
        try:
            env = self._jinja_environment()
        except ModuleNotFoundError:
            return self._render_without_jinja(context, redact_flags=redact_flags)

        template = env.get_template("writeup.md.j2")
        return str(template.render(**self._template_vars(context, redact_flags=redact_flags)))

    def render_html(self, context: WriteupContext, *, redact_flags: bool = True) -> str:
        try:
            env = self._jinja_environment()
            template = env.get_template("writeup.html.j2")
            return str(template.render(**self._template_vars(context, redact_flags=redact_flags)))
        except ModuleNotFoundError:
            markdown = self._render_without_jinja(context, redact_flags=redact_flags)
            return self._html_shell(markdown)

    def _jinja_environment(self) -> Any:
        from jinja2 import Environment, FileSystemLoader, select_autoescape

        env = Environment(
            loader=FileSystemLoader(str(self._template_dir)),
            autoescape=select_autoescape(
                enabled_extensions=("html.j2",),
                disabled_extensions=("md.j2",),
                default_for_string=True,
                default=True,
            ),
            trim_blocks=True,
            lstrip_blocks=True,
        )
        env.filters["json"] = lambda value: json.dumps(value, indent=2, sort_keys=True)
        return env

    def _render_without_jinja(self, context: WriteupContext, *, redact_flags: bool) -> str:
        variables = self._template_vars(context, redact_flags=redact_flags)
        evidence_lines = "\n".join(
            f"- `{entry.path}` ({entry.label}, sha256 `{entry.sha256}`)"
            for entry in context.evidence.entries
        ) or "- No evidence files recorded."
        failure_lines = "\n".join(
            f"- {failure.label}: {failure.reason}" for failure in context.evidence.failures
        ) or "- No capture failures recorded."
        event_lines = "\n".join(
            f"- {event.get('stage', event.get('state', 'UNKNOWN'))}: "
            f"{event.get('message', event.get('type', event))}"
            for event in context.events
        ) or "- No events recorded."
        hypotheses = json.dumps(context.hypotheses, indent=2, sort_keys=True)
        triage = json.dumps(context.triage, indent=2, sort_keys=True)
        return f"""# {variables["title"]}

Generated: {context.generated_at}

## Challenge

- Category: {variables["category"]}
- Points: {variables["points"]}
- URL: {variables["url"]}

{variables["summary"]}

## Triage

```json
{triage}
```

## Hypotheses

```json
{hypotheses}
```

## Verified Flag

`{variables["display_flag"]}`

Flag reference: `{variables["flag_reference"]}`

## Evidence

{evidence_lines}

## Capture Failures

{failure_lines}

## Timeline

{event_lines}

## Reproduction

Run the final solver from the run directory:

```bash
python3 solve.py
```

```python
{variables["display_solve_py"]}
```
"""

    def _template_vars(self, context: WriteupContext, *, redact_flags: bool) -> dict[str, Any]:
        challenge = context.challenge
        flag = self._select_verified_flag(context.events, context.evidence, challenge)
        flag_reference = self._flag_reference(flag, redact_flags=redact_flags)
        display_flag = flag_reference.get("display", flag)
        return {
            "title": challenge.get("title") or challenge.get("name") or context.run_dir.name,
            "category": challenge.get("category", "unknown"),
            "points": challenge.get("points", challenge.get("score", "unknown")),
            "url": challenge.get("url", ""),
            "flag": flag,
            "display_flag": display_flag,
            "flag_hash": flag_reference.get("sha256", ""),
            "flag_reference": flag_reference.get("reference", ""),
            "redact_flags": redact_flags,
            "summary": challenge.get("description", "No challenge description was recorded."),
            "challenge": challenge,
            "triage": context.triage,
            "hypotheses": context.hypotheses,
            "events": context.events,
            "evidence": context.evidence,
            "solve_py": context.solve_py,
            "display_solve_py": self._redact_flag_text(context.solve_py, flag, redact_flags),
            "generated_at": context.generated_at,
        }

    def _html_shell(self, markdown: str) -> str:
        return (
            "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<title>CTF writeup</title></head><body><pre>"
            f"{escape(markdown)}"
            "</pre></body></html>\n"
        )

    def _flag_reference(self, flag: str, *, redact_flags: bool) -> dict[str, str]:
        if flag == "not recorded":
            return {"display": flag, "reference": "not-recorded", "sha256": ""}
        digest = sha256(flag.encode()).hexdigest()
        if not redact_flags:
            return {"display": flag, "reference": f"flag-sha256:{digest}", "sha256": digest}
        return {
            "display": f"[REDACTED flag:{digest[:12]}]",
            "reference": f"flag-sha256:{digest}",
            "sha256": digest,
        }

    def _redact_flag_text(self, value: str, flag: str, redact_flags: bool) -> str:
        if not redact_flags or flag == "not recorded":
            return value
        return value.replace(flag, self._flag_reference(flag, redact_flags=True)["display"])

    def _redact_provenance(
        self,
        value: dict[str, Any],
        flag: str,
        flag_reference: dict[str, str],
        redact_flags: bool,
    ) -> dict[str, Any]:
        if not redact_flags or flag == "not recorded":
            return value
        replacement = flag_reference.get("reference", "[REDACTED flag]")
        return cast(dict[str, Any], self._replace_raw_flag(value, flag, replacement))

    def _replace_raw_flag(self, value: Any, flag: str, replacement: str) -> Any:
        if isinstance(value, str):
            return value.replace(flag, replacement)
        if isinstance(value, list):
            return [self._replace_raw_flag(item, flag, replacement) for item in value]
        if isinstance(value, dict):
            return {
                str(key): self._replace_raw_flag(item, flag, replacement)
                for key, item in value.items()
            }
        return value

    def _select_verified_flag(
        self, events: list[dict[str, Any]], evidence: EvidenceManifest, challenge: dict[str, Any]
    ) -> str:
        for event in reversed(events):
            data = event.get("data", event.get("payload", {}))
            stage = event.get("stage", event.get("state", event.get("type")))
            if (
                stage in {"VERIFY", "SUBMIT", "DONE"}
                and data.get("accepted") is True
                and data.get("flag")
            ):
                return str(data["flag"])
        for evidence_event in reversed(evidence.events):
            if (
                evidence_event.stage in {"VERIFY", "SUBMIT", "DONE"}
                and evidence_event.data.get("flag")
            ):
                return str(evidence_event.data["flag"])
        return str(challenge.get("verified_flag", "not recorded"))
