"""Controller-owned authenticated session handles."""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from enum import StrEnum

from ctf_agent.ingestion.session import ScopedAsyncSession
from ctf_agent.scope import HostScope


@dataclass(frozen=True, slots=True)
class AuthSessionHandle:
    _value: str

    def __repr__(self) -> str:
        return "AuthSessionHandle(<opaque>)"


class AuthSessionStatus(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class AuthSessionMetadata:
    status: AuthSessionStatus
    origin: str | None


class AuthSessionBroker:
    """Own authenticated sessions and issue credential-free lane handles."""

    def __init__(self) -> None:
        self._sessions: dict[str, ScopedAsyncSession] = {}

    @property
    def available(self) -> bool:
        return bool(self._sessions)

    def register(self, session: ScopedAsyncSession) -> AuthSessionHandle:
        if not session.authenticated:
            raise UnauthenticatedSessionError
        value = secrets.token_urlsafe(32)
        self._sessions[value] = session
        return AuthSessionHandle(value)

    def clone_lane(
        self,
        handle: AuthSessionHandle,
        scope: HostScope,
    ) -> ScopedAsyncSession:
        try:
            source = self._sessions[handle._value]
        except KeyError as exc:
            raise UnknownAuthSessionError from exc
        return source.clone(scope)

    def metadata(self, handle: AuthSessionHandle | None) -> AuthSessionMetadata:
        if handle is None:
            return AuthSessionMetadata(AuthSessionStatus.UNAVAILABLE, None)
        session = self._sessions.get(handle._value)
        if session is None:
            return AuthSessionMetadata(AuthSessionStatus.UNAVAILABLE, None)
        return AuthSessionMetadata(AuthSessionStatus.AVAILABLE, session.authenticated_origin)

    def revoke(self, handle: AuthSessionHandle) -> None:
        self._sessions.pop(handle._value, None)


class UnauthenticatedSessionError(RuntimeError):
    def __str__(self) -> str:
        return "only authenticated sessions may be registered"


class ResumeAuthSessionUnavailableError(RuntimeError):
    def __str__(self) -> str:
        return "authenticated session is unavailable after resume; authenticate again"


class UnknownAuthSessionError(LookupError):
    def __str__(self) -> str:
        return "authentication session handle is unknown or expired"
