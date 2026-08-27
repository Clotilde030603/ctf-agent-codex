"""Generate evidence-backed CTF writeups."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ctf_agent.evidence.manifest import EvidenceManifest
from ctf_agent.evidence.sanitizer import SecretSanitizer


@dataclass(frozen=True)
class WriteupContext:
    run_dir: Path
    challenge: dict[str, Any]
    triage: dict[str, Any]
    hypotheses: dict[str, Any] | list[Any]
    events: list[dict[str, Any]]
    evidence: EvidenceManifest
    solve_py: str
    generated_at: str


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
        manifest_path = run_dir / "evidence" / "manifest.json"
        evidence = (
            EvidenceManifest.load(manifest_path)
            if manifest_path.exists()
            else EvidenceManifest(run_id=run_dir.name)
        )
        return WriteupContext(
            run_dir=run_dir,
            challenge=self._read_json(run_dir / "challenge.json", {}),
            triage=self._read_json(run_dir / "triage.json", {}),
            hypotheses=self._read_json(run_dir / "hypotheses.json", []),
            events=self._read_jsonl(run_dir / "events.jsonl"),
            evidence=evidence,
            solve_py=self._read_text(run_dir / "solve.py"),
            generated_at=datetime.now(UTC).replace(microsecond=0).isoformat(),
        )

    def generate(self, run_dir: Path, output: Path | None = None) -> Path:
        context = self.build_context(run_dir)
        markdown = self.render(context)
        sanitized = self._sanitizer.sanitize(markdown).text
        output_path = output or run_dir / "writeup.md"
        output_path.write_text(sanitized, encoding="utf-8")
        return output_path

    def render(self, context: WriteupContext) -> str:
        try:
            from jinja2 import Environment, FileSystemLoader, select_autoescape
        except ModuleNotFoundError:
            return self._render_without_jinja(context)

        env = Environment(
            loader=FileSystemLoader(str(self._template_dir)),
            autoescape=select_autoescape(disabled_extensions=("md", "j2")),
            trim_blocks=True,
            lstrip_blocks=True,
        )
        env.filters["json"] = lambda value: json.dumps(value, indent=2, sort_keys=True)
        template = env.get_template("writeup.md.j2")
        return str(template.render(**self._template_vars(context)))

    def _render_without_jinja(self, context: WriteupContext) -> str:
        variables = self._template_vars(context)
        evidence_lines = "\n".join(
            f"- `{entry.path}` ({entry.label}, sha256 `{entry.sha256}`)"
            for entry in context.evidence.entries
        ) or "- No evidence files recorded."
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

`{variables["flag"]}`

## Evidence

{evidence_lines}

## Timeline

{event_lines}

## Reproduction

Run the final solver from the run directory:

```bash
python3 solve.py
```

```python
{context.solve_py.strip()}
```
"""

    def _template_vars(self, context: WriteupContext) -> dict[str, Any]:
        challenge = context.challenge
        flag = self._select_verified_flag(context.events, context.evidence, challenge)
        return {
            "title": challenge.get("title") or challenge.get("name") or context.run_dir.name,
            "category": challenge.get("category", "unknown"),
            "points": challenge.get("points", challenge.get("score", "unknown")),
            "url": challenge.get("url", ""),
            "flag": flag,
            "summary": challenge.get("description", "No challenge description was recorded."),
            "challenge": challenge,
            "triage": context.triage,
            "hypotheses": context.hypotheses,
            "events": context.events,
            "evidence": context.evidence,
            "solve_py": context.solve_py,
            "generated_at": context.generated_at,
        }

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

    def _read_json(self, path: Path, default: Any) -> Any:
        if not path.exists():
            return default
        return json.loads(path.read_text(encoding="utf-8"))

    def _read_jsonl(self, path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        events: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                events.append(json.loads(line))
        return events

    def _read_text(self, path: Path) -> str:
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8")
