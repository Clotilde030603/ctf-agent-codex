"""Extracted workflow behavior."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ctf_agent.workflow import AutonomousWorkflow

import html
import json

from ctf_agent.engine import RunContext, StateOutcome
from ctf_agent.evidence import EvidenceManifest, SecretSanitizer
from ctf_agent.schemas import (
    RunState,
)


async def evidence(workflow: AutonomousWorkflow, context: RunContext) -> StateOutcome:
    run_dir = context.record.run_dir
    evidence_dir = run_dir / "evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    challenge = workflow._challenge(context)
    manifest = EvidenceManifest(context.record.run_id)
    failures: dict[str, str] = {}
    try:
        adapter = await workflow._adapter(context)
    except Exception as exc:
        adapter = None
        failures["platform-session"] = f"{type(exc).__name__}: {exc}"

    challenge_image = evidence_dir / "01-challenge.png"
    if not challenge_image.is_file() and adapter is not None:
        try:
            captured = await adapter.capture_challenge(challenge, challenge_image)
            if captured is not None:
                challenge_image = captured
        except Exception as exc:
            failures["challenge-screenshot"] = f"{type(exc).__name__}: {exc}"
    if not challenge_image.is_file():
        failures.setdefault("challenge-screenshot", "screenshot was not created")

    replay = context.values.get("replay")
    transcript = (
        getattr(replay, "stdout", "")
        or workflow._candidate(context, allow_legacy_accepted=True).value
    )
    terminal_html = evidence_dir / "02-exploit-proof.html"
    proof_image = evidence_dir / "02-exploit-proof.png"
    terminal = None
    if not terminal_html.is_file() or not proof_image.is_file():
        try:
            terminal = workflow._terminal_renderer.render(
                transcript,
                evidence_dir,
                stem="02-exploit-proof",
                command="python3 solve.py",
            )
            terminal_html = terminal.html_path
            if terminal.png_path is not None:
                proof_image = terminal.png_path
        except Exception as exc:
            failures["terminal-render"] = f"{type(exc).__name__}: {exc}"
    if terminal_html.is_file():
        manifest.add_file(
            terminal_html,
            root=run_dir,
            label="exploit-proof-transcript",
            media_type="text/html",
            source="solver-replay",
            redacted=bool(terminal and terminal.redacted),
            metadata={
                "screenshot_status": (terminal.screenshot_status if terminal else "preserved")
            },
            producer="ctf_agent.evidence.TerminalRenderer",
            command="python3 solve.py",
            exit_code=getattr(replay, "returncode", None),
            model=workflow.settings.solver_model,
            tool="python",
        )
    if not proof_image.is_file():
        failures.setdefault("terminal-screenshot", "screenshot was not created")

    verdict_image = evidence_dir / "03-accepted.png"
    if not verdict_image.is_file() and adapter is not None:
        try:
            captured = await adapter.capture_verdict(challenge, verdict_image)
            if captured is not None:
                verdict_image = captured
        except Exception as exc:
            failures["verdict-screenshot"] = f"{type(exc).__name__}: {exc}"
    if not verdict_image.is_file():
        failures.setdefault("verdict-screenshot", "screenshot was not created")

    for label, path in (
        ("challenge", challenge_image),
        ("exploit-proof", proof_image),
        ("accepted", verdict_image),
    ):
        if not path.is_file():
            continue
        manifest.add_file(
            path,
            root=run_dir,
            label=label,
            media_type="image/png",
            source="platform" if label != "exploit-proof" else "solver-replay",
            redacted=label == "exploit-proof",
            producer=(
                type(adapter).__name__
                if label != "exploit-proof"
                else "ctf_agent.evidence.TerminalRenderer"
            ),
            command="python3 solve.py" if label == "exploit-proof" else None,
            exit_code=(getattr(replay, "returncode", None) if label == "exploit-proof" else None),
            model=workflow.settings.solver_model if label == "exploit-proof" else None,
            tool="python" if label == "exploit-proof" else "playwright",
        )

    sanitizer = SecretSanitizer()
    if not challenge_image.is_file():
        fallback = evidence_dir / "01-challenge-fallback.html"
        sanitized = sanitizer.sanitize(
            json.dumps(challenge.model_dump(mode="json"), indent=2, default=str)
        )
        fallback.write_text(
            "<html><body><pre>" + html.escape(sanitized.text) + "</pre></body></html>",
            encoding="utf-8",
        )
        manifest.add_file(
            fallback,
            root=run_dir,
            label="challenge-fallback",
            media_type="text/html",
            source="sanitized-challenge-record",
            redacted=sanitized.redacted,
            producer="ctf_agent.workflow.AutonomousWorkflow",
        )
    if not verdict_image.is_file():
        fallback = evidence_dir / "03-verdict-fallback.json"
        verdict = context.store.latest_submission_verdict(
            context.record.run_id,
            workflow._candidate(context, allow_legacy_accepted=True).value,
        )
        sanitized = sanitizer.sanitize(
            json.dumps(
                {"challenge_id": challenge.id, "verdict": verdict},
                indent=2,
            )
        )
        fallback.write_text(sanitized.text + "\n", encoding="utf-8")
        manifest.add_file(
            fallback,
            root=run_dir,
            label="accepted-verdict-fallback",
            media_type="application/json",
            source="durable-submission-record",
            redacted=sanitized.redacted,
            producer="ctf_agent.workflow.AutonomousWorkflow",
        )

    for label, reason in failures.items():
        manifest.add_event("EVIDENCE_FAILURE", reason, accepted=False, label=label)
        manifest.add_capture_failure(
            label,
            stage="EVIDENCE",
            reason=reason,
            producer="ctf_agent.workflow.AutonomousWorkflow",
        )
        context.ledger.append(
            context.record.run_id,
            "evidence.failed",
            {"label": label, "reason": reason},
            state=context.record.state.value,
        )
    flag = workflow._candidate(context, allow_legacy_accepted=True).value
    manifest.add_event("VERIFY", "candidate independently replayed", flag=flag)
    manifest.add_event("SUBMIT", "platform accepted candidate", flag=flag, accepted=True)
    manifest.save(evidence_dir / "manifest.json")
    context.ledger.append(
        context.record.run_id,
        "evidence.captured",
        {
            "accepted": not failures,
            "entry_count": len(manifest.entries),
            "failure_count": len(manifest.failures),
        },
        state=context.record.state.value,
    )
    if context.record.writeup:
        target = RunState.WRITEUP_PENDING
    else:
        target = RunState.DONE_WITH_WARNINGS if failures else RunState.DONE
    return StateOutcome(
        target,
        {
            "evidence_files": len(manifest.entries),
            "warnings": len(failures),
        },
    )
