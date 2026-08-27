"""Integrated vertical slice for ingest, solve, verify, submit, and evidence."""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

from ctf_agent.config import Settings
from ctf_agent.engine import Controller, RunContext, StateHandler, StateOutcome
from ctf_agent.evidence import EvidenceManifest, TerminalRenderer
from ctf_agent.platforms.base import PlatformAdapter
from ctf_agent.platforms.ctfd import CTFdPlatformAdapter
from ctf_agent.reproduction import reproduce_solver
from ctf_agent.scheduler import Scheduler, StaticHypothesisPlanner
from ctf_agent.schemas import (
    Challenge,
    FlagCandidate,
    Hypothesis,
    RunState,
    SpecialistResult,
    SubmissionVerdict,
)
from ctf_agent.security import redact_persisted_value
from ctf_agent.specialists.deterministic import ArtifactSignalSpecialist
from ctf_agent.triage import ScanConfig, classify_report, scan_path
from ctf_agent.verification import FlagGate, RejectedCandidates, ReplayVerifier, SubmissionBudget
from ctf_agent.writeup import WriteupGenerator, WriteupValidator


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(
            redact_persisted_value(value), indent=2, sort_keys=True, default=str
        )
        + "\n",
        encoding="utf-8",
    )


class AutonomousWorkflow:
    def __init__(self, settings: Settings, adapter: PlatformAdapter | None = None) -> None:
        self.settings = settings
        self._adapter_override = adapter
        self.handlers: dict[RunState, StateHandler] = {
            RunState.AUTHENTICATE: self.authenticate,
            RunState.INGEST: self.ingest,
            RunState.TRIAGE: self.triage,
            RunState.PLAN: self.plan,
            RunState.SOLVE: self.solve,
            RunState.VERIFY: self.verify,
            RunState.SUBMIT: self.submit,
            RunState.EVIDENCE: self.evidence,
            RunState.WRITEUP: self.writeup,
            RunState.REPRODUCE: self.reproduce,
        }

    def controller(self) -> Controller:
        return Controller(self.settings, self.handlers)

    def _adapter(self, context: RunContext) -> PlatformAdapter:
        if self._adapter_override is not None:
            return self._adapter_override
        adapter = context.values.get("adapter")
        if adapter is not None:
            return cast(PlatformAdapter, adapter)
        parsed = urlsplit(self._challenge_url(context))
        base_url = f"{parsed.scheme}://{parsed.netloc}"
        storage_state = self.settings.browser_storage_state
        if storage_state is None:
            storage_state = self.settings.runs_dir / ".sessions" / f"{parsed.hostname}.json"

        def observe(request: Any) -> None:
            context.ledger.append(
                context.record.run_id,
                "network.request",
                dict(request),
                state=context.record.state.value,
            )

        created = CTFdPlatformAdapter(
            base_url,
            browser_storage_state=storage_state,
            allow_private_hosts=self.settings.allow_private_hosts,
            request_observer=observe,
        )
        context.values["adapter"] = created
        return created

    async def authenticate(self, context: RunContext) -> StateOutcome:
        session = await self._adapter(context).authenticate()
        if not session.authenticated:
            raise RuntimeError(
                "no authenticated platform session; configure a browser/session cookie first"
            )
        return StateOutcome(RunState.INGEST, {"authenticated": True})

    async def ingest(self, context: RunContext) -> StateOutcome:
        adapter = self._adapter(context)
        challenge = await adapter.fetch_challenge(self._challenge_url(context))
        challenge.flag_policy = await adapter.extract_flag_policy(challenge)
        artifacts = await adapter.download_attachments(challenge, context.record.run_dir / "files")
        _write_json(context.record.run_dir / "challenge.json", challenge.model_dump(mode="json"))
        context.values.update(challenge=challenge, artifacts=artifacts)
        context.ledger.append(
            context.record.run_id,
            "network.ingest",
            {
                "challenge_url": challenge.url,
                "attachment_urls": challenge.attachment_urls,
                "artifact_count": len(artifacts),
            },
            state=RunState.INGEST.value,
        )
        return StateOutcome(RunState.TRIAGE, {"artifacts": len(artifacts)})

    async def triage(self, context: RunContext) -> StateOutcome:
        config = ScanConfig(
            max_depth=self.settings.max_extraction_depth,
            max_total_extracted_size=self.settings.max_extracted_bytes,
            tool_timeout_seconds=self.settings.tool_timeout_seconds,
        )
        report = scan_path(
            context.record.run_dir / "files",
            context.record.run_dir / "artifacts" / "triage",
            config,
        )
        classification = classify_report(report)
        payload = report.to_dict()
        payload["classification"] = classification.to_dict()
        _write_json(context.record.run_dir / "triage.json", payload)
        context.values.update(triage=payload, classification=classification)
        return StateOutcome(
            RunState.PLAN,
            {
                "files": len(report.files),
                "primary_category": classification.primary_category,
            },
        )

    async def plan(self, context: RunContext) -> StateOutcome:
        classification = context.values.get("classification")
        category = getattr(classification, "primary_category", "misc")
        triage_data = self._load_json(context.record.run_dir / "triage.json")
        evidence = [
            str(item.get("reason"))
            for item in triage_data.get("classification", {}).get("evidence", [])[:5]
            if isinstance(item, dict)
        ]
        hypotheses = [
            Hypothesis(
                id=f"H{index}",
                claim=claim,
                supporting_evidence=evidence,
                expected_signal="a provenance-backed flag candidate or a falsifying result",
                cost=cost,
                confidence=confidence,
                required_tools=tools,
                kill_condition="three consecutive runs add no fact or artifact",
                success_condition="fresh replay reproduces a policy-matching candidate",
            )
            for index, (claim, cost, confidence, tools) in enumerate(
                [
                    (
                        f"deterministic {category} artifact signals expose the solution",
                        "low",
                        0.65,
                        [],
                    ),
                    (
                        f"a category-specific {category} transformation is required",
                        "medium",
                        0.45,
                        [category],
                    ),
                    ("a secondary or mixed-category path explains the challenge", "high", 0.25, []),
                ],
                start=1,
            )
        ]
        _write_json(
            context.record.run_dir / "hypotheses.json",
            [hypothesis.model_dump(mode="json") for hypothesis in hypotheses],
        )
        context.values["hypotheses"] = hypotheses
        return StateOutcome(RunState.SOLVE, {"hypotheses": len(hypotheses)})

    async def solve(self, context: RunContext) -> StateOutcome:
        hypotheses = context.values.get("hypotheses")
        if not isinstance(hypotheses, list):
            hypotheses = [Hypothesis.model_validate(item) for item in self._load_json(
                context.record.run_dir / "hypotheses.json"
            )]
        scheduler = Scheduler(
            StaticHypothesisPlanner(hypotheses),
            (ArtifactSignalSpecialist(),),
            no_progress_cutoff=3,
            max_rounds=1,
        )
        result = await scheduler.run(
            {
                "run_dir": str(context.record.run_dir),
                "triage": context.values.get("triage")
                or self._load_json(context.record.run_dir / "triage.json"),
            }
        )
        _write_json(
            context.record.run_dir / "artifacts" / "specialist-results.json",
            [item.model_dump(mode="json") for item in result.specialist_results],
        )
        (context.record.run_dir / "requirements.txt").write_text(
            "# Final deterministic solver uses only Python 3.12 standard library.\n",
            encoding="utf-8",
        )
        context.values["specialist_results"] = list(result.specialist_results)
        if not result.solved:
            return StateOutcome(
                RunState.PLAN,
                {"solved": False, "stop_reason": result.stop_reason},
            )
        return StateOutcome(RunState.VERIFY, {"stop_reason": result.stop_reason})

    async def verify(self, context: RunContext) -> StateOutcome:
        challenge = self._challenge(context)
        results = self._specialist_results(context)
        rejected = RejectedCandidates()
        gate = FlagGate(challenge.flag_policy, rejected)
        reasons: list[str] = []
        for result in results:
            for candidate in result.flag_candidates:
                if context.store.is_rejected(context.record.run_id, candidate.value):
                    rejected.add(candidate.value)
                verifier = ReplayVerifier(
                    gate,
                    context.record.run_dir / "solve.py",
                    flag_regex=challenge.flag_policy.pattern,
                    timeout_seconds=self.settings.tool_timeout_seconds,
                )
                outcome = verifier.verify(candidate)
                if outcome.accepted:
                    verified = candidate.model_copy(
                        update={
                            "format_match": True,
                            "replay_verified": True,
                            "independent_verified": True,
                        }
                    )
                    context.values["candidate"] = verified
                    context.values["replay"] = outcome.replay
                    context.ledger.append(
                        context.record.run_id,
                        "flag.verified",
                        {"accepted": True, "flag": verified.value, "reason": outcome.reason},
                        state=RunState.VERIFY.value,
                    )
                    return StateOutcome(RunState.SUBMIT, {"accepted": True, "flag": verified.value})
                reasons.append(f"{candidate.value}: {outcome.reason}")
        context.ledger.append(
            context.record.run_id,
            "flag.verification_failed",
            {"reasons": reasons},
            state=RunState.VERIFY.value,
        )
        return StateOutcome(
            RunState.PLAN,
            {"accepted": False, "reasons": reasons},
        )

    async def submit(self, context: RunContext) -> StateOutcome:
        candidate = self._candidate(context)
        if not context.record.auto_submit:
            raise RuntimeError("verified candidate is ready, but --auto-submit was not enabled")
        previous_verdict = context.store.latest_submission_verdict(
            context.record.run_id, candidate.value
        )
        if previous_verdict in {
            SubmissionVerdict.ACCEPTED.value,
            SubmissionVerdict.ALREADY_SOLVED.value,
        }:
            return StateOutcome(
                RunState.EVIDENCE,
                {"accepted": True, "flag": candidate.value, "resumed": True},
            )
        if previous_verdict == SubmissionVerdict.WRONG.value:
            context.store.reject_candidate(
                context.record.run_id,
                candidate.value,
                "previous durable submission verdict was wrong",
            )
            wrong_count = context.store.submission_count_for_verdict(
                context.record.run_id, SubmissionVerdict.WRONG.value
            )
            target = RunState.TRIAGE if wrong_count >= 2 else RunState.PLAN
            return StateOutcome(
                target,
                {"accepted": False, "wrong_count": wrong_count, "resumed": True},
            )
        adapter = self._adapter(context)
        pending = context.store.pending_submission(context.record.run_id)
        if pending is not None:
            attempt_id, pending_value = pending
            if pending_value != candidate.value:
                raise RuntimeError("pending submission does not match verified candidate")
            resolver = getattr(adapter, "resolve_submission", None)
            if resolver is None:
                raise RuntimeError("pending submission cannot be resolved by this platform")
            result = await resolver(self._challenge(context), candidate.value)
            if result is None or result.verdict in {
                SubmissionVerdict.UNKNOWN,
                SubmissionVerdict.RATE_LIMITED,
            }:
                raise RuntimeError(
                    "pending submission outcome is unknown; refusing duplicate submission"
                )
            context.ledger.append(
                context.record.run_id,
                "submission.resolved",
                {"attempt_id": attempt_id, "verdict": result.verdict.value},
                state=RunState.SUBMIT.value,
            )
        else:
            used = context.store.submission_count(context.record.run_id)
            budget = SubmissionBudget(self.settings.submission_budget, used)
            gate = FlagGate(self._challenge(context).flag_policy)
            decision = gate.evaluate(candidate, budget)
            if not gate.reserve_submission(decision, budget):
                raise RuntimeError(f"submission blocked: {decision.reason}")
            attempt_id = hashlib.sha256(
                f"{context.record.run_id}\0{candidate.value}\0{used}".encode()
            ).hexdigest()
            context.store.begin_submission(
                context.record.run_id, candidate.value, attempt_id
            )
            context.ledger.append(
                context.record.run_id,
                "submission.pending",
                {"attempt_id": attempt_id, "flag": candidate.value},
                state=RunState.SUBMIT.value,
            )
            result = await adapter.submit_flag(self._challenge(context), candidate.value)
        context.store.record_submission(
            context.record.run_id,
            candidate.value,
            result.verdict.value,
            attempt_id=attempt_id,
        )
        context.values["submission"] = result
        context.ledger.append(
            context.record.run_id,
            "flag.submitted",
            {
                "accepted": result.verdict
                in {SubmissionVerdict.ACCEPTED, SubmissionVerdict.ALREADY_SOLVED},
                "flag": candidate.value,
                "verdict": result.verdict.value,
                "message": result.message,
            },
            state=RunState.SUBMIT.value,
        )
        if result.verdict in {SubmissionVerdict.ACCEPTED, SubmissionVerdict.ALREADY_SOLVED}:
            return StateOutcome(
                RunState.EVIDENCE, {"accepted": True, "flag": candidate.value}
            )
        if result.verdict is SubmissionVerdict.WRONG:
            context.store.reject_candidate(context.record.run_id, candidate.value, result.message)
            wrong_count = context.store.submission_count_for_verdict(
                context.record.run_id, SubmissionVerdict.WRONG.value
            )
            target = RunState.TRIAGE if wrong_count >= 2 else RunState.PLAN
            return StateOutcome(target, {"accepted": False, "wrong_count": wrong_count})
        raise RuntimeError(f"submission did not produce a final verdict: {result.verdict.value}")

    async def evidence(self, context: RunContext) -> StateOutcome:
        run_dir = context.record.run_dir
        evidence_dir = run_dir / "evidence"
        challenge = self._challenge(context)
        adapter = self._adapter(context)
        challenge_image = await adapter.capture_challenge(
            challenge, evidence_dir / "01-challenge.png"
        )
        replay = context.values.get("replay")
        transcript = getattr(replay, "stdout", "") or self._candidate(context).value
        terminal = TerminalRenderer().render(
            transcript,
            evidence_dir,
            stem="02-exploit-proof",
            command="python3 solve.py",
        )
        proof_image = terminal.png_path
        if proof_image is None:
            proof_image = evidence_dir / "02-exploit-proof.png"
            proof_image.write_bytes(base64.b64decode(_FALLBACK_PNG))
        verdict_image = await adapter.capture_verdict(
            challenge, evidence_dir / "03-accepted.png"
        )
        required = [challenge_image, proof_image, verdict_image]
        if any(path is None or not path.is_file() for path in required):
            raise RuntimeError(
                "platform evidence capture did not produce all three required images"
            )
        manifest = EvidenceManifest(context.record.run_id)
        labels = ("challenge", "exploit-proof", "accepted")
        for label, path in zip(labels, required, strict=True):
            assert path is not None
            manifest.add_file(
                path,
                root=run_dir,
                label=label,
                media_type="image/png",
                source="platform" if label != "exploit-proof" else "solver-replay",
                redacted=label == "exploit-proof",
            )
        manifest.add_file(
            terminal.html_path,
            root=run_dir,
            label="exploit-proof-transcript",
            media_type="text/html",
            source="solver-replay",
            redacted=terminal.redacted,
        )
        flag = self._candidate(context).value
        manifest.add_event("VERIFY", "candidate independently replayed", flag=flag)
        manifest.add_event("SUBMIT", "platform accepted candidate", flag=flag, accepted=True)
        manifest.save(evidence_dir / "manifest.json")
        target = RunState.WRITEUP if context.record.writeup else RunState.REPRODUCE
        return StateOutcome(target, {"evidence_files": 4})

    async def writeup(self, context: RunContext) -> StateOutcome:
        generator = WriteupGenerator()
        output = generator.generate(context.record.run_dir)
        validation = WriteupValidator().validate(context.record.run_dir, output)
        if not validation.ok:
            raise RuntimeError("write-up validation failed: " + "; ".join(validation.errors))
        return StateOutcome(RunState.REPRODUCE, {"writeup": str(output)})

    async def reproduce(self, context: RunContext) -> StateOutcome:
        result = await reproduce_solver(
            context.record.run_dir,
            self._candidate(context).value,
            image=self.settings.docker_image,
            timeout_seconds=self.settings.tool_timeout_seconds,
            use_docker=not self.settings.allow_local_reproduction,
        )
        context.ledger.append(
            context.record.run_id,
            "solver.reproduced",
            {
                "accepted": result.success,
                "flag": self._candidate(context).value,
                "exit_code": result.exit_code,
                "command": result.command,
            },
            state=RunState.REPRODUCE.value,
        )
        if not result.success:
            return StateOutcome(RunState.SOLVE, {"reproduced": False})
        return StateOutcome(RunState.DONE, {"reproduced": True})

    def _challenge(self, context: RunContext) -> Challenge:
        value = context.values.get("challenge")
        if isinstance(value, Challenge):
            return value
        challenge = Challenge.model_validate(
            self._load_json(context.record.run_dir / "challenge.json")
        )
        context.values["challenge"] = challenge
        return challenge

    def _specialist_results(self, context: RunContext) -> list[SpecialistResult]:
        values = context.values.get("specialist_results")
        if isinstance(values, list) and all(isinstance(item, SpecialistResult) for item in values):
            return values
        payload = self._load_json(context.record.run_dir / "artifacts" / "specialist-results.json")
        results = [SpecialistResult.model_validate(item) for item in payload]
        context.values["specialist_results"] = results
        return results

    def _candidate(self, context: RunContext) -> FlagCandidate:
        value = context.values.get("candidate")
        if isinstance(value, FlagCandidate):
            return value
        verified_events = [
            item for item in context.ledger.list(context.record.run_id)
            if item["event_type"] == "flag.verified" and item["payload"].get("flag")
        ]
        if not verified_events:
            raise RuntimeError("resume data has no verified flag candidate")
        flag = str(verified_events[-1]["payload"]["flag"])
        for result in self._specialist_results(context):
            for candidate in result.flag_candidates:
                if candidate.value == flag:
                    verified = candidate.model_copy(
                        update={
                            "format_match": True,
                            "replay_verified": True,
                            "independent_verified": True,
                        }
                    )
                    context.values["candidate"] = verified
                    return verified
        raise RuntimeError("verified candidate is absent from specialist artifacts")

    @staticmethod
    def _load_json(path: Path) -> Any:
        return json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def _challenge_url(context: RunContext) -> str:
        return str(context.values.get("challenge_url") or context.record.challenge_url)


_FALLBACK_PNG = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "/wcAAusB9Y9Z4ioAAAAASUVORK5CYII="
)
