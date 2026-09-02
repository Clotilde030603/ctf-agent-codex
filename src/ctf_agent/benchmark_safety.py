"""Expected-output matching and hardcoded-solver rejection."""

from __future__ import annotations

import ast
import base64
import binascii
import hashlib
import re
from pathlib import Path

from ctf_agent.benchmark_helpers import _relative_label, _under
from ctf_agent.benchmark_manifest import BenchmarkChallenge


def _matching_candidate(text: str, challenge: BenchmarkChallenge) -> str | None:
    if challenge.expected_flag is not None:
        return challenge.expected_flag if challenge.expected_flag in text else None
    if challenge.expected_flag_sha256 is None:
        return None
    for line in text.splitlines():
        candidate = line.strip()
        if hashlib.sha256(candidate.encode()).hexdigest() == challenge.expected_flag_sha256:
            return candidate
    return None


def _matches_expected(text: str, challenge: BenchmarkChallenge) -> bool:
    if challenge.expected_flag is not None:
        return challenge.expected_flag in text
    if challenge.expected_flag_sha256 is None:
        return False
    for line in text.splitlines():
        if hashlib.sha256(line.strip().encode()).hexdigest() == challenge.expected_flag_sha256:
            return True
    return False


def _hardcoded_solver_reason(run_dir: Path, challenge: BenchmarkChallenge) -> str | None:
    if challenge.expected_flag is None:
        return _hash_only_hardcoded_reason(run_dir, challenge)
    raw = challenge.expected_flag
    encoded = base64.b64encode(raw.encode()).decode()
    hexed = raw.encode().hex()
    for source in _solver_sources(run_dir, challenge):
        try:
            text = source.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            text = source.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if raw and raw in text:
            return f"solver source contains raw expected flag: {_relative_label(source, run_dir)}"
        if encoded and encoded in text:
            return (
                f"solver source contains base64 expected flag: {_relative_label(source, run_dir)}"
            )
        if hexed and hexed.lower() in text.lower():
            return f"solver source contains hex expected flag: {_relative_label(source, run_dir)}"
        if source.suffix == ".py":
            for constant in _python_constant_strings(text):
                if raw and raw in constant:
                    return (
                        "solver source constructs raw expected flag from constants: "
                        f"{_relative_label(source, run_dir)}"
                    )
                if encoded and encoded in constant:
                    return (
                        "solver source constructs base64 expected flag from constants: "
                        f"{_relative_label(source, run_dir)}"
                    )
                if hexed and hexed.lower() in constant.lower():
                    return (
                        "solver source constructs hex expected flag from constants: "
                        f"{_relative_label(source, run_dir)}"
                    )
    return None


def _hash_only_hardcoded_reason(run_dir: Path, challenge: BenchmarkChallenge) -> str | None:
    expected_hash = challenge.expected_flag_sha256
    if expected_hash is None:
        return None
    for source in _solver_sources(run_dir, challenge):
        try:
            text = source.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        constants = _generic_source_strings(text)
        if source.suffix == ".py":
            constants.update(_python_constant_strings(text))
        for constant in constants:
            for value in _constant_variants(constant):
                if hashlib.sha256(value).hexdigest() == expected_hash:
                    return (
                        "solver source embeds value matching expected flag hash: "
                        f"{_relative_label(source, run_dir)}"
                    )
    return None


def _generic_source_strings(text: str) -> set[str]:
    """Extract plain, encoded, and simple concatenated literals across script types."""
    values = set(re.findall(r"[A-Za-z0-9_]{1,64}\{[^\r\n{}]{1,256}\}", text))
    literal_pattern = re.compile(
        r"(?P<quote>['\"`])(?P<body>(?:\\.|(?!(?P=quote)).)*)(?P=quote)",
        re.DOTALL,
    )
    matches = list(literal_pattern.finditer(text))
    decoded: list[str] = []
    for match in matches:
        body = match.group("body")
        value = body
        if match.group("quote") != "`":
            try:
                parsed = ast.literal_eval(match.group(0))
            except (SyntaxError, ValueError):
                parsed = body
            if isinstance(parsed, str):
                value = parsed
        values.add(value)
        decoded.append(value)

    for index, value in enumerate(decoded[:-1]):
        combined = value
        previous = matches[index]
        for next_index in range(index + 1, len(matches)):
            current = matches[next_index]
            separator = text[previous.end() : current.start()]
            if not re.fullmatch(r"[ \t]*(?:[+.][ \t\r\n]*)?", separator):
                break
            combined += decoded[next_index]
            values.add(combined)
            previous = current
    return values


def _python_constant_strings(source: str) -> set[str]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    values: set[str] = set()
    for node in ast.walk(tree):
        value = _constant_string(node)
        if value is not None:
            values.add(value)
    return values


def _constant_string(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _constant_string(node.left)
        right = _constant_string(node.right)
        if left is not None and right is not None:
            return left + right
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for item in node.values:
            if not isinstance(item, ast.Constant) or not isinstance(item.value, str):
                return None
            parts.append(item.value)
        return "".join(parts)
    return None


def _constant_variants(value: str) -> set[bytes]:
    variants = {value.encode()}
    try:
        variants.add(base64.b64decode(value, validate=True))
    except (ValueError, binascii.Error):
        pass
    try:
        variants.add(bytes.fromhex(value))
    except ValueError:
        pass
    return variants


def _solver_sources(run_dir: Path, challenge: BenchmarkChallenge) -> list[Path]:
    paths: list[Path] = []
    source_suffixes = {".py", ".sh", ".js", ".mjs", ".ts", ".rb", ".pl", ".php"}
    for item in challenge.source_files:
        candidate = (run_dir / item).resolve()
        if _under(candidate, run_dir) and candidate.is_file():
            paths.append(candidate)
    for item in [*challenge.command, *(challenge.replay_command or [])]:
        candidate = (run_dir / item).resolve()
        if (
            _under(candidate, run_dir)
            and candidate.is_file()
            and candidate.suffix in source_suffixes
        ):
            paths.append(candidate)
    return sorted(set(paths))
