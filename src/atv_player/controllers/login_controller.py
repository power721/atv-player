from atv_player.models import AppConfig


class LoginController:
    def __init__(self, repo, api_client) -> None:
        self._repo = repo
        self._api_client = api_client

    def load_defaults(self) -> AppConfig:
        return self._repo.load_config()

    def login(self, base_url: str, username: str, password: str) -> AppConfig:
        api_client = self._api_client(base_url) if callable(self._api_client) else self._api_client
        payload = api_client.login(username, password)
        config = self._repo.load_config()
        config.base_url = base_url.rstrip("/")
        config.username = username
        config.token = payload.get("token", "")
        config.vod_token = ""
        self._repo.save_config(config)
        return config
