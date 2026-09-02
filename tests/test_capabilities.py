from __future__ import annotations

import importlib
import json
import shutil
from pathlib import Path
from types import ModuleType

import anyio
import pytest

from ctf_agent.capability_manifest import DEFAULT_CAPABILITY_MANIFEST
from ctf_agent.config import Settings
from ctf_agent.doctor import run_doctor
from ctf_agent.schemas import Hypothesis
from ctf_agent.specialists.toolchain import (
    ToolchainProfile,
    ToolchainSpecialist,
    ToolRequirement,
)
from ctf_agent.workers import CommandPolicy


def _capabilities() -> ModuleType:
    try:
        return importlib.import_module("ctf_agent.capabilities")
    except ModuleNotFoundError:
        pytest.fail("runtime capability snapshot provider is not implemented")


def _snapshot(*, installed: bool, allowed: bool = True, required: bool = True):
    capabilities = _capabilities()
    manifest = capabilities.CapabilityManifest(
        capabilities=(
            capabilities.CapabilityDefinition(
                name="file",
                command="file",
                required=required,
                allowed_by_default=allowed,
                version_args=("--version",),
            ),
        )
    )
    probe = capabilities.StaticCapabilityProbe(
        capabilities.ContainerProbeResult(
            reachable=True,
            image_digest="sha256:test-image",
            tools=(
                capabilities.ToolProbeResult(
                    name="file",
                    installed=installed,
                    reachable=installed,
                    authenticated=True,
                    version="file-5.44" if installed else None,
                    reason=None if installed else "command not found in image",
                ),
            ),
        )
    )
    return capabilities.CapabilityProvider(manifest, probe).snapshot("test-tools:1")


def test_command_policy_uses_manifest_allowlist() -> None:
    expected = {
        item.command
        for item in DEFAULT_CAPABILITY_MANIFEST.capabilities
        if item.command is not None and item.allowed_by_default
    }
    assert CommandPolicy().effective_allowed_argv0 == expected


def test_known_non_tool_capabilities_are_typed_and_digest_bound() -> None:
    capabilities = _capabilities()
    snapshot = capabilities.CapabilityProvider(
        DEFAULT_CAPABILITY_MANIFEST,
        capabilities.StaticCapabilityProbe(
            capabilities.ContainerProbeResult(
                reachable=True,
                image_digest="sha256:test-image",
                tools=(),
            )
        ),
    ).snapshot("test-tools:1")

    expected = {
        "auth:http-session": ("auth", "unauthenticated"),
        "network:scoped-http": ("http", "available"),
        "browser:interaction": ("browser", "missing"),
        "skill:trusted-runtime": ("skill", "available"),
    }
    observed = {
        item.name: (item.category.value, item.status.value)
        for item in snapshot.capabilities
        if item.name in expected
    }

    assert observed == expected
    assert len(snapshot.digest) == 64
    assert snapshot.prompt_context()["digest"] == snapshot.digest
    assert snapshot.require("skill:trusted-runtime").digest is not None
    assert "secret" not in json.dumps(snapshot.model_dump(mode="json")).lower()


def test_snapshot_digest_changes_with_observed_capability_truth() -> None:
    available = _snapshot(installed=True)
    missing = _snapshot(installed=False)

    assert available.digest != missing.digest


def test_container_installed_tool_is_available_when_host_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shutil, "which", lambda _name: None)

    snapshot = _snapshot(installed=True)

    capability = snapshot.require("file")
    assert capability.status.value == "available"
    assert capability.installed is True
    assert capability.source == "container:test-tools:1"


def test_container_missing_tool_is_missing_when_host_is_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: f"/host/{name}")

    snapshot = _snapshot(installed=False)

    capability = snapshot.require("file")
    assert capability.status.value == "missing"
    assert capability.installed is False


def test_installed_but_disallowed_is_disallowed_in_prompt_and_worker_policy() -> None:
    snapshot = _snapshot(installed=True, allowed=False)

    prompt_entry = snapshot.prompt_context()["capabilities"][0]
    assert prompt_entry["installed"] is True
    assert prompt_entry["allowed"] is False
    assert prompt_entry["status"] == "disallowed"
    with pytest.raises(RuntimeError, match="disallowed"):
        CommandPolicy(runtime_capabilities=snapshot).validate_argv(["file", "sample.bin"])


def test_doctor_toolchain_and_artifact_consume_the_same_snapshot(tmp_path: Path) -> None:
    snapshot = _snapshot(installed=True)
    artifact = tmp_path / "artifacts" / "runtime-capabilities.json"

    report = run_doctor(
        Settings(backend="static", runs_dir=tmp_path / "runs"),
        capability_snapshot=snapshot,
    )
    profile = ToolchainProfile(
        category="rev",
        requirements=(ToolRequirement("file", "container image", required=True),),
        fallback="continue",
    )
    runtime_hypothesis = Hypothesis(
        id="runtime",
        claim="inspect binary",
        expected_signal="headers",
        cost="low",
        confidence=0.5,
        kill_condition="required capability unavailable",
        success_condition="typed observation",
    )
    result = anyio.run(
        ToolchainSpecialist(profile, snapshot).solve,
        runtime_hypothesis,
        {"triage": {}},
    )
    snapshot.write(artifact)

    persisted = json.loads(artifact.read_text(encoding="utf-8"))
    assert report.runtime_capabilities == snapshot
    assert persisted == snapshot.model_dump(mode="json")
    assert any("tool available: file" in fact for fact in result.facts)


def test_manifest_advertised_required_tool_missing_from_image_is_doctor_error(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot(installed=False, required=True)

    report = run_doctor(
        Settings(backend="static", runs_dir=tmp_path / "runs"),
        capability_snapshot=snapshot,
    )

    check = next(item for item in report.checks if item.name == "capability:file")
    assert check.status == "error"
    assert report.ok is False


def test_unreachable_unauthenticated_misconfigured_and_reference_only_are_distinct() -> None:
    capabilities = _capabilities()
    definitions = (
        capabilities.CapabilityDefinition(name="offline", command="offline"),
        capabilities.CapabilityDefinition(name="login", command="login", requires_auth=True),
        capabilities.CapabilityDefinition(name="broken", command="broken"),
        capabilities.CapabilityDefinition(
            name="ghidra-guide", command=None, reference_only=True
        ),
    )
    probe = capabilities.StaticCapabilityProbe(
        capabilities.ContainerProbeResult(
            reachable=True,
            image_digest="sha256:test-image",
            tools=(
                capabilities.ToolProbeResult(
                    name="offline", installed=True, reachable=False, reason="endpoint refused"
                ),
                capabilities.ToolProbeResult(
                    name="login",
                    installed=True,
                    reachable=True,
                    authenticated=False,
                    reason="credentials absent",
                ),
                capabilities.ToolProbeResult(
                    name="broken",
                    installed=True,
                    reachable=True,
                    authenticated=True,
                    misconfigured=True,
                    reason="invalid runtime configuration",
                ),
            ),
        )
    )

    snapshot = capabilities.CapabilityProvider(
        capabilities.CapabilityManifest(capabilities=definitions), probe
    ).snapshot("test-tools:1")

    assert {item.name: item.status.value for item in snapshot.capabilities} == {
        "offline": "unreachable",
        "login": "unauthenticated",
        "broken": "misconfigured",
        "ghidra-guide": "reference_only",
    }
