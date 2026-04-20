from __future__ import annotations

from dataclasses import dataclass, field
import secrets
import time


@dataclass(slots=True)
class PlaylistSegment:
    index: int
    url: str
    duration: float | None = None


@dataclass(slots=True)
class ProxySession:
    token: str
    playlist_url: str
    headers: dict[str, str]
    segments: list[PlaylistSegment] = field(default_factory=list)
    cached_playlist_text: str | None = None
    created_at: float = field(default_factory=time.time)
    last_accessed_at: float = field(default_factory=time.time)


class ProxySessionRegistry:
    def __init__(self, ttl_seconds: float = 900.0) -> None:
        self._sessions: dict[str, ProxySession] = {}
        self._ttl_seconds = ttl_seconds

    def create_session(self, playlist_url: str, headers: dict[str, str]) -> str:
        self.expire_stale()
        token = secrets.token_urlsafe(9)
        self._sessions[token] = ProxySession(
            token=token,
            playlist_url=playlist_url,
            headers=dict(headers),
        )
        return token

    def get(self, token: str) -> ProxySession:
        self.expire_stale()
        session = self._sessions[token]
        session.last_accessed_at = time.time()
        return session

    def contains(self, token: str) -> bool:
        self.expire_stale()
        return token in self._sessions

    def delete(self, token: str) -> None:
        self._sessions.pop(token, None)

    def expire_stale(self, now: float | None = None) -> None:
        cutoff = (now if now is not None else time.time()) - self._ttl_seconds
        expired_tokens = [
            token for token, session in self._sessions.items() if session.last_accessed_at < cutoff
        ]
        for token in expired_tokens:
            self._sessions.pop(token, None)
