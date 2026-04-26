from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Protocol

from atv_player.models import AppConfig


class SettingsRepositoryLike(Protocol):
    def load_config(self) -> AppConfig: ...

    def save_config(self, config: AppConfig) -> None: ...


class LoginApiLike(Protocol):
    def login(self, username: str, password: str) -> Mapping[str, str]: ...


class LoginController:
    def __init__(
        self,
        repo: SettingsRepositoryLike,
        api_client: LoginApiLike | Callable[[str], LoginApiLike],
    ) -> None:
        self._repo = repo
        self._api_client = api_client

    def load_defaults(self) -> AppConfig:
        return self._repo.load_config()

    def login(self, base_url: str, username: str, password: str) -> AppConfig:
        created_client = callable(self._api_client)
        api_client = self._api_client(base_url) if created_client else self._api_client
        try:
            payload = api_client.login(username, password)
        finally:
            if created_client:
                close_client = getattr(api_client, "close", None)
                if callable(close_client):
                    close_client()
        config = self._repo.load_config()
        config.base_url = base_url.rstrip("/")
        config.username = username
        config.token = payload.get("token", "")
        config.vod_token = ""
        self._repo.save_config(config)
        return config
