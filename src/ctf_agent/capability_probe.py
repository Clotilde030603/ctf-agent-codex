"""Docker-backed runtime capability probe."""

from __future__ import annotations

import importlib.metadata
import importlib.util
import os
import shutil
import subprocess
from pathlib import Path

from ctf_agent.capabilities import (
    CapabilityDefinition,
    CapabilityProbe,
    ContainerProbeResult,
    ToolProbeResult,
    UnknownCapabilityError,
)


class RuntimeCapabilityProbe:
    """Combine container observations with controller-local runtime capabilities."""

    def __init__(self, container_probe: CapabilityProbe) -> None:
        self._container_probe = container_probe

    def probe(
        self, image: str, definitions: tuple[CapabilityDefinition, ...]
    ) -> ContainerProbeResult:
        container = self._container_probe.probe(image, definitions)
        browser = next(
            (item for item in definitions if item.name == "browser:interaction"),
            None,
        )
        if browser is None:
            return container
        return container.model_copy(
            update={"tools": (*container.tools, _browser_observation())}
        )


def _browser_observation() -> ToolProbeResult:
    if importlib.util.find_spec("playwright") is None:
        return ToolProbeResult(
            name="browser:interaction",
            installed=False,
            reachable=False,
            source="local-runtime",
            reason="playwright package is not installed",
        )
    version = importlib.metadata.version("playwright")
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            executable = Path(playwright.chromium.executable_path)
    except (ImportError, OSError, PlaywrightError) as exc:
        return ToolProbeResult(
            name="browser:interaction",
            installed=True,
            reachable=False,
            misconfigured=True,
            version=version,
            source="local-runtime",
            reason=f"Playwright inspection failed: {exc}",
        )
    ready = executable.is_file()
    return ToolProbeResult(
        name="browser:interaction",
        installed=True,
        reachable=ready,
        version=version,
        source="local-runtime",
        reason=("Playwright Chromium is available" if ready else "Chromium is not installed"),
    )


class DockerCapabilityProbe:
    """Probe executable presence and versions inside the configured Docker image."""

    def __init__(self, docker_binary: str = "docker", timeout_seconds: float = 60) -> None:
        self._docker_binary = docker_binary
        self._timeout_seconds = timeout_seconds

    def probe(
        self, image: str, definitions: tuple[CapabilityDefinition, ...]
    ) -> ContainerProbeResult:
        docker = shutil.which(self._docker_binary)
        if docker is None:
            return _unreachable(definitions, "docker executable not found")
        inspect = _run(
            [docker, "image", "inspect", "--format", "{{.Id}}", image],
            self._timeout_seconds,
        )
        if inspect.returncode != 0:
            return _unreachable(
                definitions,
                (inspect.stderr or inspect.stdout or f"image unavailable: {image}").strip(),
            )
        image_digest = inspect.stdout.strip() or None
        executable = tuple(
            item for item in definitions if item.command is not None and not item.reference_only
        )
        script = "test \"$(id -u)\" != 0 || exit 77\n" + "\n".join(
            _probe_script_line(item) for item in executable
        )
        result = _run(
            [
                docker,
                "run",
                "--rm",
                "--network=none",
                "--read-only",
                "--pids-limit=64",
                "--memory=256m",
                "--cpus=1",
                "--tmpfs=/tmp:rw,noexec,nosuid,size=16m",
                image,
                "sh",
                "-c",
                script,
            ],
            self._timeout_seconds,
        )
        if result.returncode != 0:
            reason = (result.stderr or result.stdout or "container probe failed").strip()
            return _unreachable(definitions, reason, image_digest=image_digest)
        rows = {row.name: row for row in _parse_probe_output(result.stdout)}
        tools = tuple(
            rows.get(
                definition.name,
                ToolProbeResult(
                    name=definition.name,
                    installed=False,
                    reachable=True,
                    reason="command not found in image",
                ),
            )
            for definition in executable
        )
        return ContainerProbeResult(
            reachable=True,
            image_digest=image_digest,
            tools=tools,
        )


def _probe_script_line(definition: CapabilityDefinition) -> str:
    command = definition.command
    if command is None:
        raise UnknownCapabilityError(name=definition.name)
    args = " ".join(definition.version_args)
    return (
        f"if command -v {command} >/dev/null 2>&1; then "
        f"v=$({command} {args} 2>&1 | head -n 1); "
        f"printf '%s\\t1\\t%s\\n' '{definition.name}' \"$v\"; "
        f"else printf '%s\\t0\\t\\n' '{definition.name}'; fi"
    )


def _parse_probe_output(output: str) -> tuple[ToolProbeResult, ...]:
    rows: list[ToolProbeResult] = []
    for line in output.splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        name, installed_text, version = parts
        installed = installed_text == "1"
        rows.append(
            ToolProbeResult(
                name=name,
                installed=installed,
                reachable=True,
                authenticated=True if installed else None,
                version=version or None,
                reason=None if installed else "command not found in image",
            )
        )
    return tuple(rows)


def _unreachable(
    definitions: tuple[CapabilityDefinition, ...],
    reason: str,
    *,
    image_digest: str | None = None,
) -> ContainerProbeResult:
    return ContainerProbeResult(
        reachable=False,
        image_digest=image_digest,
        reason=reason,
        tools=tuple(
            ToolProbeResult(
                name=item.name,
                installed=False,
                reachable=False,
                reason=reason,
            )
            for item in definitions
            if item.command is not None and not item.reference_only
        ),
    )


def _run(command: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout,
            env={"PATH": os.environ.get("PATH", "")},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(command, 124, "", str(exc))
