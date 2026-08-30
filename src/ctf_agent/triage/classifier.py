from __future__ import annotations

from collections import defaultdict

from .types import ChallengeCategory, ClassificationEvidence, ClassificationResult, TriageReport

RECOMMENDED_TOOLS = {
    "web": ["httpx", "playwright", "curl", "sqlmap"],
    "pwn": ["file", "checksec", "gdb", "pwntools"],
    "rev": ["strings", "ghidra", "radare2", "objdump"],
    "crypto-math": ["sage", "sympy", "python"],
    "crypto-binary": ["python", "z3", "hashcat"],
    "forensics": ["exiftool", "binwalk", "foremost", "tshark"],
    "misc": ["python", "strings"],
    "mixed": ["python", "file", "strings"],
}


def classify_report(report: TriageReport) -> ClassificationResult:
    scores: dict[ChallengeCategory, float] = defaultdict(float)
    evidence: list[ClassificationEvidence] = []
    missing: set[str] = set()

    def add(
        category: ChallengeCategory,
        reason: str,
        artifact_path: str | None = None,
        weight: float = 1.0,
    ) -> None:
        scores[category] += weight
        evidence.append(
            ClassificationEvidence(
                category=category, reason=reason, artifact_path=artifact_path, weight=weight
            )
        )

    for scanned in report.files:
        path_lower = scanned.relative_path.lower()
        mime = scanned.mime.lower()
        magic = scanned.magic.lower()
        language = (scanned.language or "").lower()
        text_blob = " ".join(item.value.lower() for item in scanned.strings[:100])
        indicator_kinds = {indicator.kind for indicator in scanned.indicators}

        if language in {"html", "javascript", "typescript", "php"} or any(
            token in path_lower for token in ("www", "route", "server", "app.py", "index.php")
        ):
            add("web", "web source, route, or frontend asset detected", scanned.path, 2.0)
        if (
            "url" in indicator_kinds
            or "http" in text_blob
            or "cookie" in text_blob
            or "csrf" in text_blob
        ):
            add("web", "HTTP/URL/session indicators detected", scanned.path, 1.0)

        if "elf" in magic or "pe executable" in magic:
            add("pwn", "native executable detected", scanned.path, 2.5)
            add("rev", "binary suitable for reverse engineering detected", scanned.path, 1.8)
        if any(result.tool == "checksec" and not result.missing for result in scanned.tool_results):
            add("pwn", "checksec output available", scanned.path, 1.5)
        if any(token in path_lower for token in ("libc", "ld-linux", ".so")):
            add("pwn", "native runtime dependency detected", scanned.path, 1.5)

        if language in {"c/c++", "rust", "go", "java", "kotlin"} or any(
            token in text_blob
            for token in ("license check", "serial", "password", "correct", "wrong")
        ):
            add("rev", "compiled-language or validation-path clues detected", scanned.path, 1.0)

        if "crypto-constant" in indicator_kinds:
            add("crypto-binary", "crypto constants or encodings detected", scanned.path, 1.2)
        if any(
            token in text_blob
            for token in ("modulus", "prime", "factor", "discrete log", "elliptic", "crt", "gcd")
        ):
            add("crypto-math", "mathematical crypto vocabulary detected", scanned.path, 1.8)
        if any(
            token in text_blob for token in ("xor", "aes", "rsa", "nonce", "iv", "salt", "base64")
        ):
            add("crypto-binary", "implementation crypto vocabulary detected", scanned.path, 1.2)

        if (
            mime.startswith("image/")
            or "pdf" in mime
            or any(token in magic for token in ("image", "pdf", "pcap", "archive"))
        ):
            add(
                "forensics",
                "media, document, capture, or archive artifact detected",
                scanned.path,
                1.2,
            )
        if scanned.parent_archive:
            add("forensics", "nested extraction provenance exists", scanned.path, 0.6)
        if scanned.entropy >= 7.5 and scanned.size > 256:
            add("forensics", "high-entropy artifact detected", scanned.path, 0.8)

    for tool in ("file", "strings", "exiftool", "binwalk", "checksec", "tshark"):
        if any(
            result.tool == tool and result.missing
            for scanned in report.files
            for result in scanned.tool_results
        ):
            missing.add(tool)

    if not scores:
        scores["misc"] = 1.0
        evidence.append(
            ClassificationEvidence(
                category="misc", reason="no stronger deterministic signal found", weight=1.0
            )
        )

    ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    primary = ranked[0][0]
    secondaries = [
        category for category, score in ranked[1:] if score >= max(1.0, ranked[0][1] * 0.45)
    ]
    if (
        len([score for _, score in ranked if score >= 2.0]) >= 2
        and ranked[1][1] >= ranked[0][1] * 0.7
    ):
        if primary != "mixed":
            secondaries = [primary, *[category for category, _ in ranked[1:3]]]
        primary = "mixed"

    top_score = ranked[0][1]
    total = sum(scores.values())
    confidence = 0.35 + min(0.6, top_score / (total + 0.001) * 0.6)
    if primary == "mixed":
        confidence = max(0.55, min(0.9, confidence))

    recommended = []
    for category in [primary, *secondaries]:
        recommended.extend(RECOMMENDED_TOOLS.get(category, []))
    recommended_tools = list(dict.fromkeys(recommended))

    return ClassificationResult(
        primary_category=primary,
        secondary_categories=secondaries,
        confidence=round(confidence, 2),
        evidence=evidence,
        recommended_tools=recommended_tools,
        missing_capabilities=sorted(missing),
    )
