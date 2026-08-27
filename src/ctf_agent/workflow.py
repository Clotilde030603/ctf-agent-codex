"""Integrated vertical slice for ingest, solve, verify, submit, and evidence."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

from ctf_agent.config import Settings
from ctf_agent.engine import Controller, RunContext, StateHandler, StateOutcome
from ctf_agent.evidence import EvidenceManifest, TerminalRenderer
from ctf_agent.ingestion.session import ScopedAsyncSession, SessionConfig
from ctf_agent.models.base import ModelBackend, ModelBackendError
from ctf_agent.models.factory import create_codex_backend
from ctf_agent.platforms.base import PlatformAdapter
from ctf_agent.platforms.detect import create_detected_adapter
from ctf_agent.reproduction import reproduce_solver
from ctf_agent.scheduler import ModelHypothesisPlanner, Scheduler, StaticHypothesisPlanner
from ctf_agent.schemas import (
    Challenge,
    FlagCandidate,
    Hypothesis,
    RunState,
    SpecialistResult,
    SubmissionVerdict,
)
from ctf_agent.scope import HostScope
from ctf_agent.security import redact_persisted_value
from ctf_agent.specialists.crypto import CryptoSpecialist
from ctf_agent.specialists.deterministic import ArtifactSignalSpecialist
from ctf_agent.specialists.forensics import ForensicsSpecialist
from ctf_agent.specialists.model import BackendFactory, ModelSolverSpecialist
from ctf_agent.specialists.web import StaticWebSpecialist
from ctf_agent.triage import ScanConfig, classify_report, scan_path
from ctf_agent.verification import (
    BlindVerifier,
    FlagGate,
    RejectedCandidates,
    ReplayVerifier,
    SubmissionBudget,
)
from ctf_agent.workers import SharedModelCallBudget
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
    def __init__(
        self,
        settings: Settings,
        adapter: PlatformAdapter | None = None,
        *,
        planner_backend: ModelBackend | None = None,
        solver_backend_factory: BackendFactory = create_codex_backend,
        worker_local_test_mode: bool = False,
        worker_allowed_argv0: set[str] | None = None,
        terminal_renderer: TerminalRenderer | None = None,
    ) -> None:
        self.settings = settings
        self._adapter_override = adapter
        self._planner_backend_override = planner_backend
        self._solver_backend_factory = solver_backend_factory
        self._worker_local_test_mode = worker_local_test_mode
        self._worker_allowed_argv0 = worker_allowed_argv0
        self._terminal_renderer = terminal_renderer or TerminalRenderer()
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

    async def _adapter(self, context: RunContext) -> PlatformAdapter:
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

        scope = HostScope.from_url(
            base_url,
            allow_private_hosts=self.settings.allow_private_hosts,
        )
        session = ScopedAsyncSession(
            scope,
            config=SessionConfig(
                timeout_seconds=self.settings.request_timeout_seconds,
                retry_budget=self.settings.retry_budget,
                rate_limit_per_second=self.settings.rate_limit_per_second,
            ),
            request_observer=observe,
        )
        created = await create_detected_adapter(
            self._challenge_url(context),
            session=session,
            browser_storage_state=storage_state,
            allow_private_hosts=self.settings.allow_private_hosts,
        )
        context.values["adapter"] = created
        return created

    async def authenticate(self, context: RunContext) -> StateOutcome:
        adapter = await self._adapter(context)
        session = await adapter.authenticate()
        if not session.authenticated:
            raise RuntimeError(
                "no authenticated platform session; configure a browser/session cookie first"
            )
        return StateOutcome(RunState.INGEST, {"authenticated": True})

    async def ingest(self, context: RunContext) -> StateOutcome:
        adapter = await self._adapter(context)
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
        fallback_hypotheses = [
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
        ][: self.settings.max_hypotheses]
        hypotheses = fallback_hypotheses
        planner_source = "static"
        if self.settings.backend == "codex":
            request_count = sum(
                event["event_type"] == "model.request"
                for event in context.ledger.list(context.record.run_id)
            )
            if request_count >= self.settings.model_call_budget:
                if not self.settings.allow_static_fallback:
                    raise RuntimeError("model call budget exhausted before planning")
                context.ledger.append(
                    context.record.run_id,
                    "model.fallback",
                    {"role": "planner", "reason": "model call budget exhausted"},
                    state=RunState.PLAN.value,
                )
            else:
                planner = ModelHypothesisPlanner(
                    self._planner_backend(context),
                    max_hypotheses=self.settings.max_hypotheses,
                )
                context.ledger.append(
                    context.record.run_id,
                    "model.request",
                    {
                        "role": "planner",
                        "model": self.settings.planner_model,
                        "request_index": request_count + 1,
                    },
                    state=RunState.PLAN.value,
                )
                try:
                    hypotheses = list(
                        await planner.plan(self._planning_context(context, triage_data))
                    )
                except ModelBackendError as exc:
                    context.ledger.append(
                        context.record.run_id,
                        "model.failure",
                        {
                            "role": "planner",
                            "error_type": type(exc).__name__,
                            "message": str(exc),
                        },
                        state=RunState.PLAN.value,
                    )
                    if not self.settings.allow_static_fallback:
                        raise
                else:
                    planner_source = "model"
                    context.ledger.append(
                        context.record.run_id,
                        "model.completed",
                        {
                            "role": "planner",
                            "model": self.settings.planner_model,
                            "hypothesis_count": len(hypotheses),
                        },
                        state=RunState.PLAN.value,
                    )
        _write_json(
            context.record.run_dir / "hypotheses.json",
            [hypothesis.model_dump(mode="json") for hypothesis in hypotheses],
        )
        context.values["hypotheses"] = hypotheses
        context.values["planner_source"] = planner_source
        return StateOutcome(
            RunState.SOLVE,
            {"hypotheses": len(hypotheses), "planner_source": planner_source},
        )

    async def solve(self, context: RunContext) -> StateOutcome:
        hypotheses = context.values.get("hypotheses")
        if not isinstance(hypotheses, list):
            hypotheses = [Hypothesis.model_validate(item) for item in self._load_json(
                context.record.run_dir / "hypotheses.json"
            )]
            context.values["hypotheses"] = hypotheses
        triage_data = context.values.get("triage") or self._load_json(
            context.record.run_dir / "triage.json"
        )
        solver_context = self._solver_context(context, triage_data)
        preflight = await ArtifactSignalSpecialist().solve(
            hypotheses[0], solver_context
        )
        specialist_results: tuple[SpecialistResult, ...]
        if preflight.status == "confirmed" and preflight.flag_candidates:
            specialist_results = (preflight,)
            solved = True
            stop_reason = "artifact_signal"
        else:
            preliminary_results = [preflight]
            category_specialist = self._category_specialist(triage_data)
            category_result = None
            if category_specialist is not None:
                category_result = await category_specialist.solve(
                    hypotheses[0], solver_context
                )
                preliminary_results.append(category_result)
            if (
                category_result is not None
                and category_result.status == "confirmed"
                and category_result.flag_candidates
            ):
                specialist_results = tuple(preliminary_results)
                solved = True
                stop_reason = f"category_{category_specialist.name}"
            elif self.settings.backend != "codex":
                specialist_results = tuple(preliminary_results)
                solved = False
                stop_reason = "no_model_backend"
            else:
                existing_model_requests = sum(
                    event["event_type"] == "model.request"
                    for event in context.ledger.list(context.record.run_id)
                )
                remaining_model_calls = max(
                    0, self.settings.model_call_budget - existing_model_requests
                )

                def record_solver_call(index: int) -> None:
                    context.ledger.append(
                        context.record.run_id,
                        "model.request",
                        {
                            "role": "solver",
                            "model": self.settings.solver_model,
                            "request_index": existing_model_requests + index,
                        },
                        state=RunState.SOLVE.value,
                    )

                shared_model_budget = SharedModelCallBudget(
                    remaining_model_calls,
                    on_reserve=record_solver_call,
                )
                model_specialist = ModelSolverSpecialist(
                    self.settings,
                    backend_factory=self._solver_backend_factory,
                    local_test_mode=self._worker_local_test_mode,
                    allowed_argv0=self._worker_allowed_argv0,
                    shared_model_budget=shared_model_budget,
                )
                scheduler = Scheduler(
                    StaticHypothesisPlanner(hypotheses),
                    (model_specialist,),
                    no_progress_cutoff=3,
                    max_rounds=1,
                    max_concurrency=self.settings.max_workers,
                )
                result = await scheduler.run(solver_context)
                specialist_results = tuple(preliminary_results) + result.specialist_results
                solved = result.solved
                stop_reason = result.stop_reason
        _write_json(
            context.record.run_dir / "artifacts" / "specialist-results.json",
            [item.model_dump(mode="json") for item in specialist_results],
        )
        self._promote_solver(context.record.run_dir, specialist_results)
        requirements = context.record.run_dir / "requirements.txt"
        if not requirements.exists():
            requirements.write_text(
                "# Solver dependencies were not declared by the selected lane.\n",
                encoding="utf-8",
            )
        context.values["specialist_results"] = list(specialist_results)
        if not solved:
            return StateOutcome(
                RunState.PLAN,
                {"solved": False, "stop_reason": stop_reason},
            )
        return StateOutcome(RunState.VERIFY, {"stop_reason": stop_reason})

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
                if not outcome.accepted:
                    reasons.append(f"{candidate.value}: replay: {outcome.reason}")
                    continue
                blind = BlindVerifier(
                    context.record.run_dir,
                    challenge.flag_policy,
                    solver_path=context.record.run_dir / "solve.py",
                    timeout_seconds=self.settings.tool_timeout_seconds,
                ).verify(candidate)
                if not blind.accepted:
                    reasons.append(
                        f"{candidate.value}: {blind.failure_stage}: {blind.reason}"
                    )
                    continue
                verified = candidate.model_copy(
                    update={
                        "format_match": True,
                        "provenance_verified": bool(
                            blind.provenance and blind.provenance.accepted
                        ),
                        "replay_verified": True,
                        "independent_verified": True,
                        "submission_allowed": True,
                    }
                )
                context.values["candidate"] = verified
                context.values["replay"] = outcome.replay
                context.ledger.append(
                    context.record.run_id,
                    "flag.verified",
                    {
                        "accepted": True,
                        "flag": verified.value,
                        "reason": blind.reason,
                        "format_match": verified.format_match,
                        "provenance_verified": verified.provenance_verified,
                        "replay_verified": verified.replay_verified,
                        "independent_verified": verified.independent_verified,
                        "submission_allowed": verified.submission_allowed,
                    },
                    state=RunState.VERIFY.value,
                )
                return StateOutcome(
                    RunState.SUBMIT,
                    {"accepted": True, "flag": verified.value},
                )
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
        required_checks = {
            "format_match": candidate.format_match,
            "provenance_verified": candidate.provenance_verified,
            "replay_verified": candidate.replay_verified,
            "independent_verified": candidate.independent_verified,
            "submission_allowed": candidate.submission_allowed,
        }
        failed_checks = [name for name, passed in required_checks.items() if not passed]
        if failed_checks:
            raise RuntimeError(
                "submission blocked by incomplete verification: "
                + ", ".join(failed_checks)
            )
        if not context.record.auto_submit:
            output = context.record.run_dir / "verified-candidate.json"
            _write_json(
                output,
                {
                    "status": "verified_not_submitted",
                    "candidate": candidate.model_dump(mode="json"),
                    "instructions": "review this private run artifact before manual submission",
                },
            )
            context.ledger.append(
                context.record.run_id,
                "flag.ready",
                {
                    "candidate_path": str(output.relative_to(context.record.run_dir)),
                    **required_checks,
                },
                state=RunState.READY.value,
            )
            return StateOutcome(
                RunState.READY,
                {
                    "verified": True,
                    "submitted": False,
                    "candidate_path": str(output),
                },
            )
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
        adapter = await self._adapter(context)
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
        if result.verdict is SubmissionVerdict.AUTH_REQUIRED:
            context.store.abandon_submission(attempt_id, result.verdict.value)
            context.ledger.append(
                context.record.run_id,
                "authentication.expired",
                {"attempt_id": attempt_id, "message": result.message},
                state=RunState.SUBMIT.value,
            )
            return StateOutcome(
                RunState.AUTHENTICATE,
                {"authenticated": False, "submission_consumed": False},
            )
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
        adapter = await self._adapter(context)
        manifest = EvidenceManifest(context.record.run_id)
        failures: list[str] = []
        try:
            challenge_image = await adapter.capture_challenge(
                challenge, evidence_dir / "01-challenge.png"
            )
        except Exception as exc:
            challenge_image = None
            failures.append(f"challenge screenshot failed: {type(exc).__name__}: {exc}")
        replay = context.values.get("replay")
        transcript = getattr(replay, "stdout", "") or self._candidate(context).value
        try:
            terminal = self._terminal_renderer.render(
                transcript,
                evidence_dir,
                stem="02-exploit-proof",
                command="python3 solve.py",
            )
        except Exception as exc:
            terminal = None
            failures.append(f"terminal render failed: {type(exc).__name__}: {exc}")
        proof_image = terminal.png_path if terminal is not None else None
        if terminal is not None and terminal.html_path.is_file():
            manifest.add_file(
                terminal.html_path,
                root=run_dir,
                label="exploit-proof-transcript",
                media_type="text/html",
                source="solver-replay",
                redacted=terminal.redacted,
                metadata={"screenshot_status": terminal.screenshot_status},
                producer="ctf_agent.evidence.TerminalRenderer",
                command="python3 solve.py",
                exit_code=getattr(replay, "returncode", None),
                model=self.settings.solver_model,
                tool="python",
            )
        if proof_image is None:
            status = terminal.screenshot_status if terminal is not None else "render-error"
            failures.append(f"terminal screenshot failed: {status}")
        try:
            verdict_image = await adapter.capture_verdict(
                challenge, evidence_dir / "03-accepted.png"
            )
        except Exception as exc:
            verdict_image = None
            failures.append(f"verdict screenshot failed: {type(exc).__name__}: {exc}")
        if challenge_image is None or not challenge_image.is_file():
            failures.append("challenge screenshot was not created")
        if verdict_image is None or not verdict_image.is_file():
            failures.append("verdict screenshot was not created")
        if failures:
            for failure in dict.fromkeys(failures):
                manifest.add_event("EVIDENCE_FAILURE", failure, accepted=False)
                manifest.add_capture_failure(
                    "required-evidence",
                    stage="EVIDENCE",
                    reason=failure,
                    producer="ctf_agent.workflow.AutonomousWorkflow",
                )
            manifest.save(evidence_dir / "manifest.json")
            raise RuntimeError(
                "required evidence capture failed: " + "; ".join(dict.fromkeys(failures))
            )
        required = (challenge_image, proof_image, verdict_image)
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
                producer=(
                    type(adapter).__name__
                    if label != "exploit-proof"
                    else "ctf_agent.evidence.TerminalRenderer"
                ),
                command="python3 solve.py" if label == "exploit-proof" else None,
                exit_code=(
                    getattr(replay, "returncode", None)
                    if label == "exploit-proof"
                    else None
                ),
                model=self.settings.solver_model if label == "exploit-proof" else None,
                tool="python" if label == "exploit-proof" else "playwright",
            )
        flag = self._candidate(context).value
        manifest.add_event("VERIFY", "candidate independently replayed", flag=flag)
        manifest.add_event("SUBMIT", "platform accepted candidate", flag=flag, accepted=True)
        manifest.save(evidence_dir / "manifest.json")
        target = RunState.WRITEUP if context.record.writeup else RunState.REPRODUCE
        return StateOutcome(target, {"evidence_files": 4})

    async def writeup(self, context: RunContext) -> StateOutcome:
        generator = WriteupGenerator()
        outputs = generator.generate_all(
            context.record.run_dir,
            redact_flags=self.settings.redact_flag,
        )
        validation = WriteupValidator().validate_all(context.record.run_dir)
        if not validation.ok:
            raise RuntimeError("write-up validation failed: " + "; ".join(validation.errors))
        return StateOutcome(
            RunState.REPRODUCE,
            {
                "writeup_markdown": str(outputs.markdown_path),
                "writeup_html": str(outputs.html_path),
                "provenance": str(outputs.provenance_path),
                "redact_flag": self.settings.redact_flag,
            },
        )

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
                            "provenance_verified": True,
                            "replay_verified": True,
                            "independent_verified": True,
                            "submission_allowed": True,
                        }
                    )
                    context.values["candidate"] = verified
                    return verified
        raise RuntimeError("verified candidate is absent from specialist artifacts")

    @staticmethod
    def _load_json(path: Path) -> Any:
        return json.loads(path.read_text(encoding="utf-8"))

    def _planner_backend(self, context: RunContext) -> ModelBackend:
        if self._planner_backend_override is not None:
            return self._planner_backend_override
        return create_codex_backend(self.settings, "planner", context.record.run_dir)

    def _planning_context(
        self, context: RunContext, triage_data: dict[str, Any]
    ) -> dict[str, object]:
        files: list[dict[str, object]] = []
        for item in triage_data.get("files", [])[:100]:
            if not isinstance(item, dict):
                continue
            files.append(
                {
                    key: item.get(key)
                    for key in (
                        "relative_path",
                        "size",
                        "sha256",
                        "mime",
                        "magic",
                        "entropy",
                        "language",
                        "parent_archive",
                        "extraction_depth",
                    )
                }
                | {
                    "indicators": item.get("indicators", [])[:50],
                    "tool_results": item.get("tool_results", [])[:20],
                }
            )
        previous_events = [
            {
                "type": event["event_type"],
                "state": event.get("state"),
                "payload": event["payload"],
            }
            for event in context.ledger.list(context.record.run_id)
            if event["event_type"]
            in {
                "state.error",
                "flag.verification_failed",
                "flag.submitted",
                "model.failure",
            }
        ][-20:]
        challenge = self._challenge(context)
        return {
            "run_id": context.record.run_id,
            "challenge": challenge.model_dump(mode="json"),
            "flag_policy": challenge.flag_policy.model_dump(mode="json"),
            "service_hosts": challenge.service_hosts,
            "classification": triage_data.get("classification", {}),
            "files": files,
            "previous_attempts_and_failures": previous_events,
        }

    def _solver_context(
        self, context: RunContext, triage_data: object
    ) -> dict[str, object]:
        planning = self._planning_context(
            context, triage_data if isinstance(triage_data, dict) else {}
        )
        return {
            **planning,
            "run_dir": str(context.record.run_dir),
            "triage": triage_data,
        }

    @staticmethod
    def _category_specialist(triage_data: object) -> Any:
        if not isinstance(triage_data, dict):
            return None
        classification = triage_data.get("classification", {})
        if not isinstance(classification, dict):
            return None
        primary = str(classification.get("primary_category", "")).lower()
        if primary in {"crypto-math", "crypto-binary"}:
            return CryptoSpecialist()
        if primary in {"forensics", "misc"}:
            return ForensicsSpecialist()
        if primary == "web":
            return StaticWebSpecialist()
        return None

    @staticmethod
    def _promote_solver(
        run_dir: Path, results: tuple[SpecialistResult, ...]
    ) -> None:
        for result in results:
            if not result.flag_candidates:
                continue
            for artifact in result.artifacts:
                candidate = (run_dir / artifact).resolve()
                if (
                    candidate.name == "solve.py"
                    and candidate.is_file()
                    and run_dir in candidate.parents
                ):
                    shutil.copy2(candidate, run_dir / "solve.py")
                    requirements = candidate.parent / "requirements.txt"
                    if requirements.is_file():
                        shutil.copy2(requirements, run_dir / "requirements.txt")
                    return

    @staticmethod
    def _challenge_url(context: RunContext) -> str:
        return str(context.values.get("challenge_url") or context.record.challenge_url)
