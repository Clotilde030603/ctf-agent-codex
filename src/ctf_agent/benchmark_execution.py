"""Single-attempt benchmark execution and scorer authority."""

from __future__ import annotations

import time
from pathlib import Path
from typing import assert_never

from ctf_agent.ablation_schema import AblationCondition
from ctf_agent.benchmark_manifest import BenchmarkChallenge
from ctf_agent.benchmark_metrics import _load_metrics
from ctf_agent.benchmark_models import BenchmarkMetrics, BenchmarkRunRecord, CommandRecord
from ctf_agent.benchmark_process import (
    _command_path_error,
    _fixture_root,
    _fresh_workdir,
    _run_clean_replay,
    _run_command,
)
from ctf_agent.benchmark_safety import _hardcoded_solver_reason, _matches_expected
from ctf_agent.benchmark_schema import BenchmarkRunIdentity, BenchmarkRunner


async def _run_once(
    manifest: Path,
    challenge: BenchmarkChallenge,
    repeat_index: int,
    *,
    timeout_seconds: float,
    condition: AblationCondition | None = None,
) -> BenchmarkRunRecord:
    run_started = time.monotonic()
    if not challenge.command:
        return BenchmarkRunRecord(
            challenge_id=challenge.id,
            category=challenge.category,
            difficulty=challenge.difficulty,
            repeat_index=repeat_index,
            fixture_command_success=False,
            solved=False,
            expected_flag_seen=False,
            seconds_to_result=0,
            error="missing command",
        )

    try:
        fixture_root = _fixture_root(manifest, challenge)
        with _fresh_workdir(fixture_root) as run_dir:
            command_error = _command_path_error(challenge.command, run_dir)
            if command_error is not None:
                raise ValueError(command_error)
            replay_error = _command_path_error(
                challenge.replay_command or challenge.command,
                run_dir,
            )
            if replay_error is not None:
                raise ValueError(replay_error)
            rejection = _hardcoded_solver_reason(run_dir, challenge)
            if rejection is not None:
                return BenchmarkRunRecord(
                    challenge_id=challenge.id,
                    category=challenge.category,
                    difficulty=challenge.difficulty,
                    repeat_index=repeat_index,
                    fixture_command_success=False,
                    solved=False,
                    expected_flag_seen=False,
                    seconds_to_result=time.monotonic() - run_started,
                    hardcoded_rejected=True,
                    error=rejection,
                )

            invocation = None
            if challenge.runner is BenchmarkRunner.AUTONOMOUS_WORKFLOW:
                from ctf_agent.benchmark_runner import ScorerInvocation

                invocation = (
                    ScorerInvocation.create(condition)
                    if condition is not None
                    else ScorerInvocation.create_default(
                        "offline-benchmark", "low", challenge.docker_image
                    )
                )
            self_reported_metrics = None
            if challenge.runner is BenchmarkRunner.AUTONOMOUS_WORKFLOW:
                self_reported_metrics = (
                    _load_metrics(run_dir, challenge)
                    if challenge.metrics_source == "self_reported"
                    else None
                )
                from ctf_agent.benchmark_runner import (
                    AutonomousScoreRequest,
                    run_autonomous_workflow,
                )

                if invocation is None:
                    raise RuntimeError("autonomous invocation was not created")
                if not challenge.artifact_paths:
                    raise RuntimeError("autonomous challenge has no source artifact")
                artifacts = await run_autonomous_workflow(
                    run_dir,
                    invocation,
                    AutonomousScoreRequest(
                        challenge=challenge,
                        source_artifact=challenge.artifact_paths[0],
                    ),
                )
                expected_seen = (
                    artifacts.candidate is not None
                    and _matches_expected(artifacts.candidate.value, challenge)
                )
                solved = (
                    expected_seen
                    and artifacts.verified_candidate
                    and artifacts.metrics.replay_verified is True
                )
                return BenchmarkRunRecord(
                    challenge_id=challenge.id,
                    category=challenge.category,
                    difficulty=challenge.difficulty,
                    repeat_index=repeat_index,
                    fixture_command_success=artifacts.final_state == "READY",
                    clean_replay_success=artifacts.metrics.replay_verified,
                    solved=solved,
                    expected_flag_seen=expected_seen,
                    seconds_to_result=(
                        artifacts.metrics.time_to_verified_seconds or 0.0
                    ),
                    metrics=artifacts.metrics,
                    self_reported_metrics=self_reported_metrics,
                    authoritative_metrics_source="scorer_invocation",
                    observed_runtime_identity=artifacts.observed_runtime_identity,
                    run_identity=BenchmarkRunIdentity(
                        run_id=artifacts.run_id,
                        challenge_id=challenge.id,
                        repeat_index=repeat_index,
                        runner=challenge.runner,
                        tool_image_digest=(
                            artifacts.observed_runtime_identity.tool_image_digest
                        ),
                    ),
                    verified_candidate=artifacts.verified_candidate,
                    final_state=artifacts.final_state,
                    promoted_solver_sha256=artifacts.promoted_solver_sha256,
                    error=artifacts.error,
                )
            command = await _run_command(
                challenge.command,
                run_dir,
                timeout_seconds=timeout_seconds,
            )
            self_reported_metrics = (
                _load_metrics(run_dir, challenge)
                if challenge.metrics_source == "self_reported"
                else None
            )
            match challenge.runner:
                case BenchmarkRunner.FIXTURE_COMMAND:
                    output = f"{command.stdout}\n{command.stderr}"
                    expected_seen = _matches_expected(output, challenge)
                    fixture_success = (
                        command.exit_code == 0 and expected_seen and not command.timed_out
                    )
                    clean_replay: CommandRecord | None = None
                    clean_replay_success: bool | None = None
                    clean_replay_skipped = False
                    clean_replay_reason: str | None = None
                    if challenge.clean_replay and fixture_success:
                        (
                            clean_replay,
                            clean_replay_success,
                            clean_replay_skipped,
                            clean_replay_reason,
                        ) = await _run_clean_replay(
                            manifest,
                            challenge,
                            timeout_seconds=timeout_seconds,
                        )
                    solved = fixture_success and (
                        clean_replay_success is not False
                        and not (challenge.clean_replay and clean_replay_success is None)
                    )
                    metrics = BenchmarkMetrics(
                        tool_calls=1 + (1 if clean_replay is not None else 0),
                        time_to_candidate_seconds=(command.seconds if expected_seen else None),
                        replay_verified=clean_replay_success,
                    )
                    return BenchmarkRunRecord(
                        challenge_id=challenge.id,
                        category=challenge.category,
                        difficulty=challenge.difficulty,
                        repeat_index=repeat_index,
                        fixture_command_success=fixture_success,
                        clean_replay_success=clean_replay_success,
                        clean_replay_skipped=clean_replay_skipped,
                        clean_replay_reason=clean_replay_reason,
                        solved=solved,
                        expected_flag_seen=expected_seen,
                        seconds_to_result=time.monotonic() - run_started,
                        timed_out=command.timed_out,
                        command=command,
                        clean_replay=clean_replay,
                        metrics=metrics,
                        self_reported_metrics=self_reported_metrics,
                    )
                case BenchmarkRunner.AUTONOMOUS_WORKFLOW:
                    raise RuntimeError("autonomous workflow bypassed scorer execution")
                case unreachable:
                    assert_never(unreachable)
    except Exception as exc:
        return BenchmarkRunRecord(
            challenge_id=challenge.id,
            category=challenge.category,
            difficulty=challenge.difficulty,
            repeat_index=repeat_index,
            fixture_command_success=False,
            solved=False,
            expected_flag_seen=False,
            seconds_to_result=time.monotonic() - run_started,
            error=f"{type(exc).__name__}: {exc}",
        )
