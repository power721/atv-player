from PySide6.QtCore import QSize

from atv_player.models import AppConfig
from atv_player.ui.login_window import LoginWindow


class FakeLoginController:
    def __init__(self) -> None:
        self.login_calls: list[tuple[str, str, str]] = []

    def load_defaults(self) -> AppConfig:
        return AppConfig(base_url="http://demo", username="alice")

    def login(self, base_url: str, username: str, password: str) -> AppConfig:
        self.login_calls.append((base_url, username, password))
        return AppConfig(base_url=base_url, username=username, token="token-123")


def test_login_window_uses_larger_default_size(qtbot) -> None:
    window = LoginWindow(FakeLoginController())
    qtbot.addWidget(window)
    window.show()
    qtbot.wait(50)

    assert window.size() == QSize(720, 520)


def test_login_window_centers_content_container_both_axes(qtbot) -> None:
    window = LoginWindow(FakeLoginController())
    qtbot.addWidget(window)
    window.resize(1400, 900)
    window.show()
    qtbot.wait(50)

    container_center = window.content_container.geometry().center()
    window_center = window.rect().center()

    assert abs(container_center.x() - window_center.x()) <= 5
    assert abs(container_center.y() - window_center.y()) <= 5


def test_login_window_click_login_uses_existing_submission_flow(qtbot) -> None:
    controller = FakeLoginController()
    window = LoginWindow(controller)
    qtbot.addWidget(window)
    window.show()

    window.base_url_edit.setText("http://server")
    window.username_edit.setText("bob")
    window.password_edit.setText("secret")

    with qtbot.waitSignal(window.login_succeeded):
        window.login_button.click()

    assert controller.login_calls == [("http://server", "bob", "secret")]
