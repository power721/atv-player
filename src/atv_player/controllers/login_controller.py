from atv_player.models import AppConfig


class LoginController:
    def __init__(self, repo, api_client) -> None:
        self._repo = repo
        self._api_client = api_client

    def load_defaults(self) -> AppConfig:
        return self._repo.load_config()

    def login(self, base_url: str, username: str, password: str) -> AppConfig:
        payload = self._api_client.login(username, password)
        config = self._repo.load_config()
        config.base_url = base_url.rstrip("/")
        config.username = username
        config.token = payload["token"]
        self._repo.save_config(config)
        return config
