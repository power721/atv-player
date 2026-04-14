from atv_player.controllers.login_controller import LoginController
from atv_player.models import AppConfig


class FakeApiClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def login(self, username: str, password: str) -> dict:
        self.calls.append((username, password))
        return {"token": "token-123"}


class FakeSettingsRepository:
    def __init__(self) -> None:
        self.saved: AppConfig | None = None
        self.current = AppConfig()

    def load_config(self) -> AppConfig:
        return self.current

    def save_config(self, config: AppConfig) -> None:
        self.current = config
        self.saved = config


def test_login_controller_persists_base_url_username_and_token() -> None:
    repo = FakeSettingsRepository()
    api = FakeApiClient()
    controller = LoginController(repo, api)

    result = controller.login("http://127.0.0.1:4567", "alice", "secret")

    assert result.token == "token-123"
    assert repo.saved is not None
    assert repo.saved.base_url == "http://127.0.0.1:4567"
    assert repo.saved.username == "alice"
    assert repo.saved.token == "token-123"


def test_login_controller_reads_defaults_from_storage() -> None:
    repo = FakeSettingsRepository()
    repo.current = AppConfig(base_url="http://demo", username="bob", token="", last_path="/")
    controller = LoginController(repo, FakeApiClient())

    config = controller.load_defaults()

    assert config.base_url == "http://demo"
    assert config.username == "bob"
