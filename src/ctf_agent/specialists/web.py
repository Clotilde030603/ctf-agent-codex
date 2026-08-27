"""Static web specialist for downloaded source and assets.

This specialist never performs network requests. It extracts facts with exact
file-and-line provenance and only emits flag candidates when the preserved
source/data directly contains or constructs them.
"""

from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass
from pathlib import Path

from ctf_agent.schemas import FlagCandidate, Hypothesis, SpecialistResult

WEB_EXTENSIONS = {
    ".py",
    ".js",
    ".ts",
    ".jsx",
    ".tsx",
    ".php",
    ".rb",
    ".go",
    ".java",
    ".html",
    ".htm",
    ".vue",
    ".svelte",
}
TEXT_EXTENSIONS = WEB_EXTENSIONS | {
    ".css",
    ".json",
    ".yaml",
    ".yml",
    ".txt",
    ".env",
    ".ini",
    ".toml",
    ".md",
}

FLAG_RE = re.compile(r"[A-Za-z0-9_.-]+\{[^{}\r\n]{1,256}\}")
HTTP_METHOD_RE = re.compile(r"\b(GET|POST|PUT|PATCH|DELETE|OPTIONS|HEAD)\b", re.I)
PY_DECORATOR_RE = re.compile(
    r"@(?P<object>(?:app|bp|blueprint|router|api)\.)?"
    r"(?P<decorator>route|get|post|put|patch|delete)\s*\((?P<args>.*)"
)
EXPRESS_ROUTE_RE = re.compile(
    r"\b(?:app|router)\.(?P<method>get|post|put|patch|delete|use)\s*"
    r"\(\s*['\"](?P<path>/[^'\"]*)['\"]",
    re.I,
)
FASTAPI_DECORATOR_RE = re.compile(
    r"@(?P<object>router|app)\.(?P<method>get|post|put|patch|delete)\("
)
PHP_ROUTE_RE = re.compile(
    r"\bRoute::(?P<method>get|post|put|patch|delete|any)\s*"
    r"\(\s*['\"](?P<path>/[^'\"]*)['\"]",
    re.I,
)
GO_ROUTE_RE = re.compile(
    r"\b(?:HandleFunc|Handle)\s*\(\s*['\"](?P<path>/[^'\"]*)['\"]",
)
PARAM_RE = re.compile(
    r"(?:request\.(?:args|form|json|cookies|headers)|req\.(?:query|body|params|cookies|headers)"
    r"|\$_(?:GET|POST|REQUEST|COOKIE|SERVER))"
    r"(?:\.get)?\s*(?:\(\s*|\[\s*)['\"](?P<name>[A-Za-z0-9_.:-]+)['\"]",
)
AUTH_RE = re.compile(
    r"\b(session|cookie|jwt|bearer|authorization|login_required|authenticated|"
    r"require_auth|passport|csrf|xsrf|same_site|samesite)\b",
    re.I,
)
CSRF_RE = re.compile(r"\b(csrf|xsrf|csrf_token|x-csrf-token|x-xsrf-token)\b", re.I)


@dataclass(frozen=True, slots=True)
class SourceFile:
    path: Path
    relative: str
    text: str
    lines: list[str]


class StaticWebSpecialist:
    name = "static-web"

    def supports(self, category: str) -> bool:
        lowered = category.lower()
        return any(token in lowered for token in ("web", "http", "flask", "express", "api"))

    async def solve(
        self, hypothesis: Hypothesis, context: dict[str, object]
    ) -> SpecialistResult:
        run_dir = Path(str(context["run_dir"]))
        files = _collect_source_files(run_dir)
        if not files:
            return SpecialistResult(
                hypothesis_id=hypothesis.id,
                status="inconclusive",
                facts=["missing capability: no downloaded web source/assets were available"],
                next_action="download source/assets or use a dynamic web specialist",
                confidence=0.05,
            )

        facts: list[str] = []
        for source in files:
            facts.extend(_route_facts(source))
            facts.extend(_parameter_facts(source))
            facts.extend(_trust_boundary_facts(source))

        candidates = _flag_candidates(files)
        artifacts: list[str] = []
        commands: list[str] = []
        reproduction_command = ""
        status = "inconclusive"
        next_action = "use dynamic testing/model-backed web specialist for data-dependent behavior"
        confidence = 0.25 if facts else 0.1

        if candidates:
            solver = _write_solver(run_dir, files)
            artifacts.append(str(solver))
            commands.append("python3 solve.py")
            reproduction_command = "python3 solve.py"
            status = "confirmed"
            next_action = "independent verification"
            confidence = max(candidate.confidence for candidate in candidates)
        elif facts:
            facts.append(
                "missing capability: static analysis found web surfaces "
                "but no directly derivable flag"
            )

        return SpecialistResult(
            hypothesis_id=hypothesis.id,
            status=status,
            facts=facts,
            artifacts=artifacts,
            commands=commands,
            reproduction_command=reproduction_command,
            flag_candidates=candidates,
            next_action=next_action,
            confidence=confidence,
        )


def _collect_source_files(run_dir: Path) -> list[SourceFile]:
    roots = [run_dir / "files", run_dir / "artifacts"]
    collected: list[SourceFile] = []
    for root in roots:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in TEXT_EXTENSIONS:
                continue
            try:
                data = path.read_bytes()
            except OSError:
                continue
            if b"\x00" in data[:4096]:
                continue
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                text = data.decode("latin-1")
            relative = path.relative_to(run_dir).as_posix()
            collected.append(
                SourceFile(path=path, relative=relative, text=text, lines=text.splitlines())
            )
    return collected


def _route_facts(source: SourceFile) -> list[str]:
    facts: list[str] = []
    pending_py_decorators: list[tuple[int, str, str]] = []
    for line_number, line in enumerate(source.lines, start=1):
        stripped = line.strip()
        py_match = PY_DECORATOR_RE.search(stripped)
        if py_match:
            route_path = _first_string_literal(py_match.group("args"))
            if route_path:
                method = _decorator_method(py_match.group("decorator"), py_match.group("args"))
                pending_py_decorators.append((line_number, method, route_path))
            continue

        if pending_py_decorators and stripped.startswith("def "):
            function_name = stripped.split("(", 1)[0].removeprefix("def ").strip()
            for decorator_line, method, route_path in pending_py_decorators:
                facts.append(
                    f"{source.relative}:{decorator_line} route {method} "
                    f"{route_path} -> {function_name}"
                )
            pending_py_decorators.clear()

        for regex in (EXPRESS_ROUTE_RE, PHP_ROUTE_RE):
            match = regex.search(line)
            if match:
                facts.append(
                    f"{source.relative}:{line_number} route "
                    f"{match.group('method').upper()} {match.group('path')}"
                )
        go_match = GO_ROUTE_RE.search(line)
        if go_match:
            facts.append(f"{source.relative}:{line_number} route ANY {go_match.group('path')}")
        if FASTAPI_DECORATOR_RE.search(line) and "(" in line:
            route_path = _first_string_literal(line)
            fastapi_match = FASTAPI_DECORATOR_RE.search(line)
            if fastapi_match and route_path:
                facts.append(
                    f"{source.relative}:{line_number} route "
                    f"{fastapi_match.group('method').upper()} {route_path}"
                )
    return facts


def _parameter_facts(source: SourceFile) -> list[str]:
    facts: list[str] = []
    seen: set[tuple[int, str]] = set()
    for line_number, line in enumerate(source.lines, start=1):
        for match in PARAM_RE.finditer(line):
            key = (line_number, match.group("name"))
            if key in seen:
                continue
            seen.add(key)
            facts.append(f"{source.relative}:{line_number} parameter {match.group('name')}")
        if "<form" in line.lower() or "<input" in line.lower():
            name_match = re.search(r"\bname=['\"]([^'\"]+)['\"]", line, re.I)
            if name_match:
                facts.append(
                    f"{source.relative}:{line_number} form parameter {name_match.group(1)}"
                )
    return facts


def _trust_boundary_facts(source: SourceFile) -> list[str]:
    facts: list[str] = []
    for line_number, line in enumerate(source.lines, start=1):
        auth_match = AUTH_RE.search(line)
        if auth_match:
            kind = "CSRF boundary" if CSRF_RE.search(line) else "auth/session boundary"
            facts.append(f"{source.relative}:{line_number} {kind}: {line.strip()[:140]}")
        method_match = HTTP_METHOD_RE.search(line)
        if method_match and any(token in line.lower() for token in ("fetch(", "axios", "http.")):
            facts.append(
                f"{source.relative}:{line_number} client endpoint uses "
                f"{method_match.group(1).upper()}"
            )
    return facts


def _flag_candidates(files: list[SourceFile]) -> list[FlagCandidate]:
    candidates: list[FlagCandidate] = []
    seen: set[str] = set()
    for source in files:
        for line_number, line in enumerate(source.lines, start=1):
            for match in FLAG_RE.finditer(line):
                if _looks_like_source_expression(line, match.start(), match.end()):
                    continue
                value = match.group(0)
                if any(char in value for char in ("'", '"', "+")):
                    continue
                if value not in seen:
                    seen.add(value)
                    candidates.append(
                        _candidate(
                            value,
                            source,
                            line_number,
                            "literal flag-like value present in downloaded source/asset",
                            0.88,
                        )
                    )
        if source.path.suffix.lower() == ".py":
            for value, line_number, derivation in _python_computed_flags(source):
                if value not in seen:
                    seen.add(value)
                    candidates.append(_candidate(value, source, line_number, derivation, 0.82))
    return candidates


def _python_computed_flags(source: SourceFile) -> list[tuple[str, int, str]]:
    try:
        tree = ast.parse(source.text)
    except SyntaxError:
        return []
    found: list[tuple[str, int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign | ast.AnnAssign | ast.Return):
            value = _eval_string_expr(node.value)
            if value and FLAG_RE.fullmatch(value):
                found.append(
                    (
                        value,
                        getattr(node, "lineno", 1),
                        "Python string expression computes this flag-like value",
                    )
                )
    return found


def _eval_string_expr(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for value in node.values:
            if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
                return None
            parts.append(value.value)
        return "".join(parts)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _eval_string_expr(node.left)
        right = _eval_string_expr(node.right)
        if left is not None and right is not None:
            return left + right
    return None


def _candidate(
    value: str,
    source: SourceFile,
    line_number: int,
    derivation: str,
    confidence: float,
) -> FlagCandidate:
    return FlagCandidate(
        value=value,
        source_artifact=source.relative,
        source_location=f"{source.relative}:{line_number}",
        derivation=["static web source analysis", derivation],
        solver_command="python3 solve.py",
        format_match=True,
        confidence=confidence,
    )


def _looks_like_source_expression(line: str, start: int, end: int) -> bool:
    before = line[:start]
    after = line[end:]
    quote_count = line.count("'") + line.count('"')
    if quote_count >= 2 and ("+" in before or "+" in after):
        return True
    return False


def _write_solver(run_dir: Path, files: list[SourceFile]) -> Path:
    paths = [source.relative for source in files]
    source = f'''#!/usr/bin/env python3
"""Reproduce static web specialist candidates from preserved files."""
from pathlib import Path
import ast
import re

PATTERN = re.compile({json.dumps(FLAG_RE.pattern)})
FILES = {json.dumps(paths, indent=2)}

def eval_string_expr(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts = []
        for value in node.values:
            if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
                return None
            parts.append(value.value)
        return "".join(parts)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = eval_string_expr(node.left)
        right = eval_string_expr(node.right)
        if left is not None and right is not None:
            return left + right
    return None

seen = set()
for relative in FILES:
    path = Path(relative)
    if not path.is_file():
        continue
    text = path.read_text(encoding="utf-8", errors="replace")
    for match in PATTERN.finditer(text):
        value = match.group(0)
        if any(char in value for char in ("'", '"', "+")):
            continue
        if value not in seen:
            seen.add(value)
            print(value)
    if path.suffix == ".py":
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.Assign, ast.AnnAssign, ast.Return)):
                value = eval_string_expr(node.value)
                if value and PATTERN.fullmatch(value) and value not in seen:
                    seen.add(value)
                    print(value)
'''
    artifacts_dir = run_dir / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    specialist_solver = artifacts_dir / "web_solve.py"
    specialist_solver.write_text(source, encoding="utf-8")
    specialist_solver.chmod(0o755)
    solve_path = run_dir / "solve.py"
    if not solve_path.exists():
        solve_path.write_text(source, encoding="utf-8")
        solve_path.chmod(0o755)
    return specialist_solver


def _decorator_method(decorator: str, args: str) -> str:
    lowered = decorator.lower()
    if lowered != "route":
        return lowered.upper()
    methods_match = re.search(r"methods\s*=\s*\[([^\]]+)\]", args)
    if not methods_match:
        return "GET"
    methods = re.findall(r"['\"]([A-Za-z]+)['\"]", methods_match.group(1))
    return ",".join(method.upper() for method in methods) or "GET"


def _first_string_literal(text: str) -> str | None:
    match = re.search(r"['\"]([^'\"]+)['\"]", text)
    return match.group(1) if match else None
