"""Deterministic checks for evidence-backed write-ups."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from ctf_agent.evidence.manifest import EvidenceManifest
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

        flags = set(re.findall(r"[A-Za-z0-9_.-]+\{[^{}\n]{1,200}\}", text))
        if flags and not self._flags_are_supported(flags, manifest):
            unsupported = ", ".join(sorted(flags))
            errors.append(
                "writeup contains flag-looking values not supported by "
                f"VERIFY/SUBMIT/DONE evidence events: {unsupported}"
            )

        return FactValidationResult(ok=not errors, errors=tuple(errors), warnings=tuple(warnings))

    def _flags_are_supported(self, flags: set[str], manifest: EvidenceManifest | None) -> bool:
        if manifest is None:
            return False
        supported: set[str] = set()
        for event in manifest.events:
            flag = event.data.get("flag")
            if event.stage in {"VERIFY", "SUBMIT", "DONE"} and isinstance(flag, str):
                supported.add(flag)
        return flags.issubset(supported)
