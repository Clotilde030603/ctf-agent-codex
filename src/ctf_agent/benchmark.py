"""Offline benchmark runner for retired or local CTF challenge manifests."""

from __future__ import annotations

import ast
import asyncio
import base64
import binascii
import hashlib
import json
import os
import re
import shutil
import signal
import statistics
import tempfile
import time
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from ctf_agent.config import DEFAULT_CTF_TOOL_IMAGE


class BenchmarkMetrics(BaseModel):
    wrong_submissions: int = Field(default=0, ge=0)
    model_calls: int = Field(default=0, ge=0)
    tool_calls: int = Field(default=0, ge=0)
    hallucinated_candidates: int = Field(default=0, ge=0)
    candidate_count: int = Field(default=0, ge=0)
    time_to_candidate_seconds: float | None = Field(default=None, ge=0)
    time_to_accepted_seconds: float | None = Field(default=None, ge=0)
    replay_verified: bool | None = None
    independent_verified: bool | None = None
    writeup_validated: bool | None = None
    resume_verified: bool | None = None


class BenchmarkChallenge(BaseModel):
    id: str
    command: list[str] = Field(default_factory=list)
    expected_flag: str | None = None
    expected_flag_sha256: str | None = None
    category: str = "misc"
    difficulty: str = "unknown"
    repeat_runs: int | None = Field(default=None, ge=1)
    timeout_seconds: float | None = Field(default=None, gt=0)
    total_budget_seconds: float | None = Field(default=None, gt=0)
    workdir: str = "."
    source_files: list[str] = Field(default_factory=list)
    metrics_file: str = "benchmark-metrics.json"
    events_file: str = "events.jsonl"
    metrics_source: Literal["none", "self_reported"] = "none"
    clean_replay: bool = True
    clean_mode: Literal["local", "docker"] = "local"
    replay_command: list[str] | None = None
    docker_image: str = DEFAULT_CTF_TOOL_IMAGE

    @field_validator("command", "replay_command")
    @classmethod
    def command_items_must_not_be_empty(cls, value: list[str] | None) -> list[str] | None:
        if value is not None and any(not item for item in value):
            raise ValueError("command entries must not be empty")
        return value

    @model_validator(mode="after")
    def expected_flag_source_required(self) -> BenchmarkChallenge:
        if self.expected_flag is None and self.expected_flag_sha256 is None:
            raise ValueError("expected_flag or expected_flag_sha256 is required")
        return self


class BenchmarkManifest(BaseModel):
    challenges: list[BenchmarkChallenge] = Field(default_factory=list)
    repeat_runs: int = Field(default=3, ge=1)
    timeout_seconds: float = Field(default=60, gt=0)
    total_budget_seconds: float = Field(default=3600, gt=0)


class CommandRecord(BaseModel):
    command: list[str]
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    seconds: float = 0
    timed_out: bool = False
    skipped: bool = False
    skip_reason: str | None = None


class BenchmarkRunRecord(BaseModel):
    challenge_id: str
    category: str
    difficulty: str
    repeat_index: int
    fixture_command_success: bool
    clean_replay_success: bool | None = None
    clean_replay_skipped: bool = False
    clean_replay_reason: str | None = None
    solved: bool
    expected_flag_seen: bool
    seconds_to_result: float
    timed_out: bool = False
    hardcoded_rejected: bool = False
    error: str | None = None
    command: CommandRecord | None = None
    clean_replay: CommandRecord | None = None
    metrics: BenchmarkMetrics = Field(default_factory=BenchmarkMetrics)
    self_reported_metrics: BenchmarkMetrics | None = None


class BenchmarkChallengeRecord(BaseModel):
    id: str
    category: str
    difficulty: str
    repeat_runs: int
    solved: bool
    fixture_command_success_rate: float | None
    clean_replay_success_rate: float | None
    wrong_submissions: int
    model_calls: int
    tool_calls: int
    hallucinated_candidate_rate: float | None
    time_to_candidate_seconds: float | None
    time_to_accepted_seconds: float | None
    replay_verified_rate: float | None
    independent_verified_rate: float | None
    writeup_validated_rate: float | None
    resume_verified_rate: float | None
    runs: list[BenchmarkRunRecord]


class BenchmarkReport(BaseModel):
    manifest: str
    challenge_count: int
    run_count: int
    solved_count: int
    solved_run_count: int
    solve_rate: float | None
    fixture_command_success_rate: float | None
    clean_reproduction_rate: float | None
    replay_verified_rate: float | None
    independent_verified_rate: float | None
    writeup_validated_rate: float | None
    resume_verified_rate: float | None
    wrong_submissions: int
    model_calls: int
    tool_calls: int
    hallucinated_candidate_rate: float | None
    results: list[dict[str, Any]]
    challenges: list[BenchmarkChallengeRecord]


def _load_manifest(path: Path) -> BenchmarkManifest:
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".json":
        return BenchmarkManifest.model_validate(json.loads(text))
    try:
        import yaml
    except ImportError as exc:
        raise RuntimeError("YAML manifests require the optional PyYAML package") from exc
    return BenchmarkManifest.model_validate(yaml.safe_load(text) or {})


async def run_benchmark(manifest: Path) -> dict[str, Any]:
    manifest = manifest.resolve()
    config = _load_manifest(manifest)
    started = time.monotonic()
    challenge_records: list[BenchmarkChallengeRecord] = []

    for challenge in config.challenges:
        if time.monotonic() - started >= config.total_budget_seconds:
            break
        challenge_records.append(await _run_challenge(manifest, config, challenge, started))

    runs = [run for challenge in challenge_records for run in challenge.runs]
    solved_count = sum(1 for item in challenge_records if item.solved)
    solved_run_count = sum(1 for item in runs if item.solved)
    fixture_successes = [run.fixture_command_success for run in runs]
    replay_successes = [
        run.clean_replay_success
        for run in runs
        if run.clean_replay_success is not None and not run.clean_replay_skipped
    ]

    report = BenchmarkReport(
        manifest=str(manifest),
        challenge_count=len(challenge_records),
        run_count=len(runs),
        solved_count=solved_count,
        solved_run_count=solved_run_count,
        solve_rate=_rate([item.solved for item in challenge_records]),
        fixture_command_success_rate=_rate(fixture_successes),
        clean_reproduction_rate=_rate(replay_successes),
        replay_verified_rate=_metric_rate(runs, "replay_verified"),
        independent_verified_rate=_metric_rate(runs, "independent_verified"),
        writeup_validated_rate=_metric_rate(runs, "writeup_validated"),
        resume_verified_rate=_metric_rate(runs, "resume_verified"),
        wrong_submissions=sum(item.wrong_submissions for item in challenge_records),
        model_calls=sum(item.model_calls for item in challenge_records),
        tool_calls=sum(item.tool_calls for item in challenge_records),
        hallucinated_candidate_rate=_candidate_hallucination_rate(runs),
        results=[_legacy_result(item) for item in challenge_records],
        challenges=challenge_records,
    )
    return report.model_dump(mode="json")


async def _run_challenge(
    manifest: Path,
    config: BenchmarkManifest,
    challenge: BenchmarkChallenge,
    benchmark_started: float,
) -> BenchmarkChallengeRecord:
    repeat_runs = challenge.repeat_runs or config.repeat_runs
    timeout_seconds = challenge.timeout_seconds or config.timeout_seconds
    challenge_budget = challenge.total_budget_seconds or config.total_budget_seconds
    runs: list[BenchmarkRunRecord] = []
    challenge_started = time.monotonic()
    for repeat_index in range(1, repeat_runs + 1):
        elapsed_total = time.monotonic() - benchmark_started
        elapsed_challenge = time.monotonic() - challenge_started
        remaining_total = config.total_budget_seconds - elapsed_total
        remaining_challenge = challenge_budget - elapsed_challenge
        if remaining_total <= 0 or remaining_challenge <= 0:
            break
        run_timeout = min(timeout_seconds, remaining_total, remaining_challenge)
        runs.append(
            await _run_once(
                manifest,
                challenge,
                repeat_index,
                timeout_seconds=run_timeout,
            )
        )

    replay_successes = [
        run.clean_replay_success
        for run in runs
        if run.clean_replay_success is not None and not run.clean_replay_skipped
    ]
    return BenchmarkChallengeRecord(
        id=challenge.id,
        category=challenge.category,
        difficulty=challenge.difficulty,
        repeat_runs=len(runs),
        solved=bool(runs) and all(run.solved for run in runs),
        fixture_command_success_rate=_rate([run.fixture_command_success for run in runs]),
        clean_replay_success_rate=_rate(replay_successes),
        wrong_submissions=sum(run.metrics.wrong_submissions for run in runs),
        model_calls=sum(run.metrics.model_calls for run in runs),
        tool_calls=sum(run.metrics.tool_calls for run in runs),
        hallucinated_candidate_rate=_candidate_hallucination_rate(runs),
        time_to_candidate_seconds=_median(
            run.metrics.time_to_candidate_seconds for run in runs
        ),
        time_to_accepted_seconds=_median(run.metrics.time_to_accepted_seconds for run in runs),
        replay_verified_rate=_metric_rate(runs, "replay_verified"),
        independent_verified_rate=_metric_rate(runs, "independent_verified"),
        writeup_validated_rate=_metric_rate(runs, "writeup_validated"),
        resume_verified_rate=_metric_rate(runs, "resume_verified"),
        runs=runs,
    )


async def _run_once(
    manifest: Path,
    challenge: BenchmarkChallenge,
    repeat_index: int,
    *,
    timeout_seconds: float,
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

            command = await _run_command(
                challenge.command,
                run_dir,
                timeout_seconds=timeout_seconds,
            )
            output = f"{command.stdout}\n{command.stderr}"
            expected_seen = _matches_expected(output, challenge)
            fixture_success = command.exit_code == 0 and expected_seen and not command.timed_out
            self_reported_metrics = (
                _load_metrics(run_dir, challenge)
                if challenge.metrics_source == "self_reported"
                else None
            )
            metrics = BenchmarkMetrics(
                tool_calls=1,
                time_to_candidate_seconds=(command.seconds if expected_seen else None),
            )
            clean_replay: CommandRecord | None = None
            clean_replay_success: bool | None = None
            clean_replay_skipped = False
            clean_replay_reason: str | None = None
            if challenge.clean_replay and fixture_success:
                clean_replay, clean_replay_success, clean_replay_skipped, clean_replay_reason = (
                    await _run_clean_replay(
                        manifest,
                        challenge,
                        timeout_seconds=timeout_seconds,
                    )
                )

            solved = fixture_success and (
                clean_replay_success is not False
                and not (challenge.clean_replay and clean_replay_success is None)
            )
            metrics = metrics.model_copy(
                update={
                    "tool_calls": 1 + (1 if clean_replay is not None else 0),
                    "replay_verified": clean_replay_success,
                }
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


async def _run_clean_replay(
    manifest: Path,
    challenge: BenchmarkChallenge,
    *,
    timeout_seconds: float,
) -> tuple[CommandRecord | None, bool | None, bool, str | None]:
    replay_command = challenge.replay_command or challenge.command
    fixture_root = _fixture_root(manifest, challenge)
    if challenge.clean_mode == "docker":
        if shutil.which("docker") is None:
            return None, None, True, "docker unavailable"
        with _fresh_workdir(fixture_root) as run_dir:
            command_error = _command_path_error(replay_command, run_dir)
            if command_error is not None:
                raise ValueError(command_error)
            docker_command = [
                "docker",
                "run",
                "--rm",
                "--network=none",
                "--cpus=1",
                "--memory=512m",
                "--pids-limit=128",
                "--read-only",
                "--tmpfs=/tmp:rw,noexec,nosuid,size=64m",
                f"--mount=type=bind,src={run_dir},dst=/work,readonly",
                "-w",
                "/work",
                challenge.docker_image,
                *replay_command,
            ]
            record = await _run_command(docker_command, run_dir, timeout_seconds=timeout_seconds)
    else:
        with _fresh_workdir(fixture_root) as run_dir:
            command_error = _command_path_error(replay_command, run_dir)
            if command_error is not None:
                raise ValueError(command_error)
            record = await _run_command(replay_command, run_dir, timeout_seconds=timeout_seconds)
    output = f"{record.stdout}\n{record.stderr}"
    success = (
        record.exit_code == 0
        and _matches_expected(output, challenge)
        and not record.timed_out
    )
    return record, success, False, None


async def _run_command(command: list[str], cwd: Path, *, timeout_seconds: float) -> CommandRecord:
    started = time.monotonic()
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        start_new_session=os.name == "posix",
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout_seconds)
    except TimeoutError:
        if os.name == "posix":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        else:
            process.kill()
        stdout, stderr = await process.communicate()
        return CommandRecord(
            command=command,
            exit_code=124,
            stdout=stdout.decode(errors="replace"),
            stderr=(stderr.decode(errors="replace") + "\ntimed out").strip(),
            seconds=time.monotonic() - started,
            timed_out=True,
        )
    returncode = process.returncode
    if returncode is None:
        raise RuntimeError("process finished without a return code")
    return CommandRecord(
        command=command,
        exit_code=returncode,
        stdout=stdout.decode(errors="replace"),
        stderr=stderr.decode(errors="replace")[-1000:],
        seconds=time.monotonic() - started,
    )


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
                "solver source contains base64 expected flag: "
                f"{_relative_label(source, run_dir)}"
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


def _hash_only_hardcoded_reason(
    run_dir: Path, challenge: BenchmarkChallenge
) -> str | None:
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
    values = set(
        re.findall(r"[A-Za-z0-9_]{1,64}\{[^\r\n{}]{1,256}\}", text)
    )
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


def _load_metrics(run_dir: Path, challenge: BenchmarkChallenge) -> BenchmarkMetrics:
    metrics = BenchmarkMetrics()
    metrics_path = run_dir / challenge.metrics_file
    if metrics_path.is_file():
        payload = json.loads(metrics_path.read_text(encoding="utf-8"))
        if isinstance(payload, Mapping):
            metrics = _merge_metrics(metrics, payload)
            events = payload.get("events")
            if isinstance(events, list):
                metrics = _merge_metrics(metrics, _derive_event_metrics(events))
    events_path = run_dir / challenge.events_file
    if events_path.is_file():
        metrics = _merge_metrics(metrics, _derive_jsonl_metrics(events_path))
    return metrics


def _merge_metrics(
    metrics: BenchmarkMetrics,
    payload: Mapping[str, Any],
) -> BenchmarkMetrics:
    data = metrics.model_dump()
    for key in data:
        if key in payload and payload[key] is not None:
            data[key] = payload[key]
    return BenchmarkMetrics.model_validate(data)


def _derive_jsonl_metrics(path: Path) -> Mapping[str, Any]:
    events: list[Mapping[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            value = json.loads(line)
            if isinstance(value, Mapping):
                events.append(value)
    return _derive_event_metrics(events)


def _derive_event_metrics(events: Iterable[Mapping[str, Any]]) -> Mapping[str, Any]:
    wrong = 0
    model_calls = 0
    tool_calls = 0
    hallucinated_candidates = 0
    candidate_count = 0
    first_candidate: float | None = None
    first_accepted: float | None = None
    replay_verified: bool | None = None
    independent_verified: bool | None = None
    writeup_validated: bool | None = None
    resume_verified: bool | None = None

    for event in events:
        event_type = str(event.get("type") or event.get("event_type") or event.get("stage") or "")
        payload = event.get("payload") or event.get("data") or {}
        payload = payload if isinstance(payload, Mapping) else {}
        seconds = _event_seconds(event)
        verdict = str(payload.get("verdict", "")).lower()
        if event_type in {"flag.submitted", "submission"} and verdict == "wrong":
            wrong += 1
        if event_type in {"model.call", "model.called", "model.request"}:
            model_calls += 1
        if event_type in {
            "tool.run",
            "tool.called",
            "worker.command",
            "worker.tool",
            "worker.tool_call",
        }:
            tool_calls += 1
        if event_type in {"flag.candidate", "candidate.found"}:
            candidate_count += 1
            if bool(payload.get("hallucinated")):
                hallucinated_candidates += 1
            if first_candidate is None:
                first_candidate = seconds
        if event_type in {"flag.submitted", "submission"} and verdict in {
            "accepted",
            "already_solved",
        } and first_accepted is None:
            first_accepted = seconds
        if event_type in {"solver.replayed", "flag.verified"}:
            replay_verified = bool(payload.get("accepted", True))
        if event_type in {"flag.verified", "independent.verified"}:
            independent_verified = bool(payload.get("accepted", True))
        if event_type in {"writeup.validated", "writeup"}:
            writeup_validated = bool(payload.get("accepted", payload.get("ok", True)))
        if event_type in {"run.resumed", "resume.verified"}:
            resume_verified = True

    return {
        "wrong_submissions": wrong,
        "model_calls": model_calls,
        "tool_calls": tool_calls,
        "hallucinated_candidates": hallucinated_candidates,
        "candidate_count": candidate_count,
        "time_to_candidate_seconds": first_candidate,
        "time_to_accepted_seconds": first_accepted,
        "replay_verified": replay_verified,
        "independent_verified": independent_verified,
        "writeup_validated": writeup_validated,
        "resume_verified": resume_verified,
    }


def _event_seconds(event: Mapping[str, Any]) -> float | None:
    for key in ("seconds", "elapsed_seconds", "time_seconds"):
        value = event.get(key)
        if isinstance(value, int | float):
            return float(value)
    payload = event.get("payload") or event.get("data") or {}
    if isinstance(payload, Mapping):
        value = payload.get("seconds") or payload.get("elapsed_seconds")
        if isinstance(value, int | float):
            return float(value)
    return None


def _legacy_result(challenge: BenchmarkChallengeRecord) -> dict[str, Any]:
    first_run = challenge.runs[0] if challenge.runs else None
    return {
        "id": challenge.id,
        "category": challenge.category,
        "difficulty": challenge.difficulty,
        "solved": challenge.solved,
        "seconds_to_result": first_run.seconds_to_result if first_run else 0,
        "solved_at_15m": challenge.solved
        and first_run is not None
        and first_run.seconds_to_result <= 900,
        "solved_at_30m": challenge.solved
        and first_run is not None
        and first_run.seconds_to_result <= 1800,
        "solved_at_60m": challenge.solved
        and first_run is not None
        and first_run.seconds_to_result <= 3600,
        "wrong_submissions": challenge.wrong_submissions,
        "hallucinated_candidate_rate": challenge.hallucinated_candidate_rate,
        "clean_reproduction": challenge.clean_replay_success_rate == 1,
        "clean_reproduction_rate": challenge.clean_replay_success_rate,
        "fixture_command_success_rate": challenge.fixture_command_success_rate,
        "exit_code": first_run.command.exit_code if first_run and first_run.command else 127,
        "stderr": first_run.command.stderr if first_run and first_run.command else "",
        "error": first_run.error if first_run else "not run",
    }


def _rate(values: Iterable[bool | None]) -> float | None:
    filtered = [value for value in values if value is not None]
    if not filtered:
        return None
    return sum(1 for value in filtered if value) / len(filtered)


def _metric_rate(runs: Iterable[BenchmarkRunRecord], field: str) -> float | None:
    return _rate(getattr(run.metrics, field) for run in runs)


def _candidate_hallucination_rate(runs: Iterable[BenchmarkRunRecord]) -> float | None:
    run_list = list(runs)
    candidate_count = sum(run.metrics.candidate_count for run in run_list)
    if candidate_count == 0:
        return None
    return sum(run.metrics.hallucinated_candidates for run in run_list) / candidate_count


def _median(values: Iterable[float | None]) -> float | None:
    filtered = [value for value in values if value is not None]
    if not filtered:
        return None
    return statistics.median(filtered)


def _fixture_root(manifest: Path, challenge: BenchmarkChallenge) -> Path:
    root = manifest.parent.resolve()
    fixture = (root / challenge.workdir).resolve()
    if not _under(fixture, root) or not fixture.is_dir():
        raise ValueError(
            "benchmark workdir must be an existing directory under manifest root"
        )
    return fixture


def _command_path_error(command: list[str], run_dir: Path) -> str | None:
    if not command:
        return None
    interpreter_names = {
        "python",
        "python3",
        "node",
        "ruby",
        "perl",
        "sh",
        "bash",
    }
    source_suffixes = {".py", ".sh", ".js", ".mjs", ".rb", ".pl"}
    executable = Path(command[0])
    executable_name = executable.name
    if executable_name in interpreter_names:
        if _has_inline_interpreter_flag(executable_name, command[1:]):
            return "inline interpreter execution is not allowed in benchmark commands"
        script_paths = [
            (run_dir / item).resolve()
            for item in command[1:]
            if not item.startswith("-") and Path(item).suffix in source_suffixes
        ]
        if not script_paths:
            return "benchmark interpreter command must reference a fixture script"
        if any(not _under(path, run_dir) or not path.is_file() for path in script_paths):
            return "benchmark interpreter script must exist inside fresh workdir"
    else:
        resolved_executable = (
            executable.resolve()
            if executable.is_absolute()
            else (run_dir / executable).resolve()
        )
        if (
            not _under(resolved_executable, run_dir)
            or not resolved_executable.is_file()
            or resolved_executable.suffix not in source_suffixes
        ):
            return "benchmark executable must be an allowlisted interpreter or fixture script"
    for index, item in enumerate(command):
        path = Path(item)
        if index == 0 and path.is_absolute():
            if path.name in interpreter_names:
                continue
            return f"absolute benchmark executable is not allowlisted: {item}"
        if path.is_absolute() and not _under(path.resolve(), run_dir):
            return f"benchmark command path escapes fresh workdir: {item}"
        if not path.is_absolute() and ".." in path.parts:
            return f"benchmark command path contains parent traversal: {item}"
    return None


def _has_inline_interpreter_flag(executable: str, arguments: list[str]) -> bool:
    for argument in arguments:
        if argument == "-":
            return True
        if executable in {"sh", "bash"} and argument.startswith("-"):
            if "c" in argument[1:]:
                return True
        elif executable in {"python", "python3"}:
            if argument == "-m" or (argument.startswith("-") and "c" in argument[1:]):
                return True
        elif executable == "node" and argument in {"-e", "--eval", "-p", "--print"}:
            return True
        elif executable in {"ruby", "perl"} and argument.startswith("-"):
            if "e" in argument[1:]:
                return True
    return False


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


@contextmanager
def _fresh_workdir(source: Path) -> Iterator[Path]:
    source = source.resolve()
    with tempfile.TemporaryDirectory(prefix="ctf-benchmark-") as temp_dir:
        target = Path(temp_dir)
        shutil.copytree(
            source,
            target,
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        yield target


def _under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _relative_label(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root.resolve()))
    except ValueError:
        return str(path.name)


def benchmark(manifest: Path) -> dict[str, Any]:
    return asyncio.run(run_benchmark(manifest))
