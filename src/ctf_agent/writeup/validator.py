"""Deterministic checks for evidence-backed write-ups."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ctf_agent.evidence.manifest import EvidenceManifest, sha256_file
from ctf_agent.evidence.sanitizer import SecretSanitizer


@dataclass(frozen=True)
class FactValidationResult:
    ok: bool
    errors: tuple[str, ...] = field(default_factory=tuple)
    warnings: tuple[str, ...] = field(default_factory=tuple)


class WriteupValidator:
    REQUIRED_HEADINGS = ("# ", "## Challenge", "## Verified Flag", "## Evidence", "## Reproduction")

    def __init__(self, sanitizer: SecretSanitizer | None = None) -> None:
        self._sanitizer = sanitizer or SecretSanitizer()

    def validate(self, run_dir: Path, writeup_path: Path | None = None) -> FactValidationResult:
        path = writeup_path or run_dir / "writeup.md"
        errors: list[str] = []
        warnings: list[str] = []

        if not path.exists():
            return FactValidationResult(False, (f"missing writeup: {path}",), ())

        text = path.read_text(encoding="utf-8")
        sanitized = self._sanitizer.sanitize(text)
        if sanitized.redacted:
            errors.append("writeup contains secret-like material that would be redacted")

        for heading in self.REQUIRED_HEADINGS:
            if heading not in text:
                errors.append(f"missing required heading: {heading.strip()}")

        manifest_path = run_dir / "evidence" / "manifest.json"
        if not manifest_path.exists():
            errors.append("missing evidence manifest")
            manifest = None
        else:
            manifest = EvidenceManifest.load(manifest_path)
            errors.extend(manifest.verify_files(run_dir))
            for entry in manifest.entries:
                if entry.path not in text:
                    warnings.append(f"evidence file not referenced in writeup: {entry.path}")

        ledger = self._read_jsonl(run_dir / "events.jsonl")
        flags = self._raw_flags(text)
        if flags and not self._flags_are_supported(flags, manifest, ledger):
            unsupported = ", ".join(sorted(flags))
            errors.append(
                "writeup contains flag-looking values not supported by "
                f"VERIFY/SUBMIT/DONE evidence events: {unsupported}"
            )

        return FactValidationResult(ok=not errors, errors=tuple(errors), warnings=tuple(warnings))

    def validate_all(self, run_dir: Path) -> FactValidationResult:
        markdown = self.validate(run_dir, run_dir / "writeup.md")
        errors = list(markdown.errors)
        warnings = list(markdown.warnings)
        html_path = run_dir / "writeup.html"
        provenance_path = run_dir / "provenance.json"
        provenance: dict[str, Any] = {}
        if provenance_path.exists():
            try:
                loaded = json.loads(provenance_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    provenance = loaded
            except json.JSONDecodeError:
                pass
        redaction_required = bool(
            provenance.get("metadata", {}).get("redact_flags")
            if isinstance(provenance.get("metadata"), dict)
            else False
        )
        markdown_path = run_dir / "writeup.md"
        markdown_text = (
            markdown_path.read_text(encoding="utf-8") if markdown_path.exists() else ""
        )
        if redaction_required and self._raw_flags(markdown_text):
            errors.append("redacted Markdown contains raw flag-looking values")
        if not html_path.exists():
            errors.append("missing standalone HTML writeup")
        else:
            html = html_path.read_text(encoding="utf-8")
            if "<html" not in html.lower():
                errors.append("standalone HTML writeup is not an HTML document")
            html_flags = self._raw_flags(html)
            if redaction_required and html_flags:
                errors.append("redacted HTML contains raw flag-looking values")
            manifest = self._load_manifest(run_dir)
            ledger = self._read_jsonl(run_dir / "events.jsonl")
            if html_flags and not self._flags_are_supported(html_flags, manifest, ledger):
                errors.append("HTML contains unsupported raw flag claims")
        if not provenance_path.exists():
            errors.append("missing provenance JSON")
        else:
            self._validate_provenance(run_dir, provenance_path, errors, warnings)
        return FactValidationResult(ok=not errors, errors=tuple(errors), warnings=tuple(warnings))

    def _flags_are_supported(
        self,
        flags: set[str],
        manifest: EvidenceManifest | None,
        ledger: list[dict[str, Any]],
    ) -> bool:
        supported: set[str] = set()
        if manifest is not None:
            for event in manifest.events:
                flag = event.data.get("flag")
                if event.stage in {"VERIFY", "SUBMIT", "DONE"} and isinstance(flag, str):
                    supported.add(flag)
        for ledger_event in ledger:
            data = ledger_event.get("data", ledger_event.get("payload", {}))
            stage = ledger_event.get("stage", ledger_event.get("state", ledger_event.get("type")))
            flag = data.get("flag") if isinstance(data, dict) else None
            if stage in {"VERIFY", "SUBMIT", "DONE"} and isinstance(flag, str):
                supported.add(flag)
        return flags.issubset(supported)

    def _validate_provenance(
        self,
        run_dir: Path,
        provenance_path: Path,
        errors: list[str],
        warnings: list[str],
    ) -> None:
        try:
            provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            errors.append(f"invalid provenance JSON: {exc}")
            return
        outputs = provenance.get("generated_outputs", [])
        output_paths = {item.get("path") for item in outputs if isinstance(item, dict)}
        for required in {"writeup.md", "writeup.html"}:
            if required not in output_paths:
                errors.append(f"provenance missing generated output: {required}")
        if provenance.get("path") != "provenance.json":
            errors.append("provenance missing self path")
        flag_reference = provenance.get("flag_reference", {})
        if flag_reference and not flag_reference.get("sha256"):
            warnings.append("provenance flag reference has no sha256")
        for item in outputs:
            if not isinstance(item, dict) or "path" not in item or "sha256" not in item:
                errors.append("malformed generated output provenance entry")
                continue
            path = run_dir / str(item["path"])
            if not path.exists():
                errors.append(f"provenance output missing on disk: {item['path']}")
                continue
            if sha256_file(path) != item["sha256"]:
                errors.append(f"provenance sha256 mismatch: {item['path']}")

    def _load_manifest(self, run_dir: Path) -> EvidenceManifest | None:
        manifest_path = run_dir / "evidence" / "manifest.json"
        if not manifest_path.exists():
            return None
        return EvidenceManifest.load(manifest_path)

    def _read_jsonl(self, path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        events: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                events.append(json.loads(line))
        return events

    def _raw_flags(self, text: str) -> set[str]:
        return set(re.findall(r"[A-Za-z0-9_.-]+\{[^{}\n]{1,200}\}", text))
