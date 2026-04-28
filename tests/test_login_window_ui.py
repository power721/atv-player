import threading

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


class AsyncLoginController:
    def __init__(self) -> None:
        self._main_thread_id = threading.get_ident()
        self.load_defaults_calls = 0
        self.login_calls: list[tuple[str, str, str]] = []
        self._defaults_events: list[threading.Event] = []
        self._defaults_results: list[AppConfig] = []
        self._login_events: list[threading.Event] = []
        self._login_results: list[AppConfig] = []
        self._login_errors: list[Exception] = []

    def load_defaults(self) -> AppConfig:
        self.load_defaults_calls += 1
        assert threading.get_ident() != self._main_thread_id
        event = threading.Event()
        self._defaults_events.append(event)
        assert event.wait(timeout=5), "load_defaults was never released"
        return self._defaults_results.pop(0)

    def finish_defaults(self, config: AppConfig) -> None:
        self._defaults_results.append(config)
        self._defaults_events.pop(0).set()

    def login(self, base_url: str, username: str, password: str) -> AppConfig:
        self.login_calls.append((base_url, username, password))
        assert threading.get_ident() != self._main_thread_id
        event = threading.Event()
        self._login_events.append(event)
        assert event.wait(timeout=5), "login was never released"
        if self._login_errors:
            raise self._login_errors.pop(0)
        return self._login_results.pop(0)

    def finish_login(self, config: AppConfig | None = None, exc: Exception | None = None) -> None:
        if config is not None:
            self._login_results.append(config)
        if exc is not None:
            self._login_errors.append(exc)
        self._login_events.pop(0).set()


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


def test_login_window_loads_defaults_outside_main_thread(qtbot) -> None:
    controller = AsyncLoginController()
    window = LoginWindow(controller)
    qtbot.addWidget(window)
    window.show()

    qtbot.waitUntil(lambda: controller.load_defaults_calls == 1, timeout=1000)
    controller.finish_defaults(AppConfig(base_url="http://async-demo", username="carol"))

    qtbot.waitUntil(
        lambda: window.base_url_edit.text() == "http://async-demo" and window.username_edit.text() == "carol",
        timeout=1000,
    )


def test_login_window_click_login_runs_off_main_thread_and_disables_button(qtbot) -> None:
    controller = AsyncLoginController()
    window = LoginWindow(controller)
    qtbot.addWidget(window)
    window.show()
    qtbot.waitUntil(lambda: controller.load_defaults_calls == 1, timeout=1000)
    controller.finish_defaults(AppConfig(base_url="http://demo", username="alice"))
    qtbot.waitUntil(lambda: window.base_url_edit.text() == "http://demo", timeout=1000)

    window.base_url_edit.setText("http://server")
    window.username_edit.setText("bob")
    window.password_edit.setText("secret")

    with qtbot.waitSignal(window.login_succeeded, timeout=1000):
        window.login_button.click()
        qtbot.waitUntil(lambda: controller.login_calls == [("http://server", "bob", "secret")], timeout=1000)
        assert window.login_button.isEnabled() is False
        controller.finish_login(AppConfig(base_url="http://server", username="bob", token="token-123"))

    assert window.login_button.isEnabled() is True


def test_login_window_shows_async_login_error_and_restores_button(qtbot, monkeypatch) -> None:
    controller = AsyncLoginController()
    window = LoginWindow(controller)
    qtbot.addWidget(window)
    window.show()
    qtbot.waitUntil(lambda: controller.load_defaults_calls == 1, timeout=1000)
    controller.finish_defaults(AppConfig(base_url="http://demo", username="alice"))
    qtbot.waitUntil(lambda: window.base_url_edit.text() == "http://demo", timeout=1000)

    errors: list[tuple[str, str]] = []
    monkeypatch.setattr("atv_player.ui.login_window.QMessageBox.critical", lambda parent, title, message: errors.append((title, message)))

    window.password_edit.setText("secret")
    window.login_button.click()
    qtbot.waitUntil(lambda: controller.login_calls == [("http://demo", "alice", "secret")], timeout=1000)
    controller.finish_login(exc=RuntimeError("bad credentials"))

    qtbot.waitUntil(lambda: errors == [("登录失败", "bad credentials")], timeout=1000)
    assert window.login_button.isEnabled() is True


def test_login_window_reads_inputs_on_main_thread_before_worker_start(qtbot, monkeypatch) -> None:
    controller = AsyncLoginController()
    window = LoginWindow(controller)
    qtbot.addWidget(window)
    window.show()
    qtbot.waitUntil(lambda: controller.load_defaults_calls == 1, timeout=1000)
    controller.finish_defaults(AppConfig(base_url="http://demo", username="alice"))
    qtbot.waitUntil(lambda: window.base_url_edit.text() == "http://demo", timeout=1000)

    main_thread_id = threading.get_ident()
    text_call_threads: list[int] = []

    base_url_text = window.base_url_edit.text
    username_text = window.username_edit.text
    password_text = window.password_edit.text

    monkeypatch.setattr(
        window.base_url_edit,
        "text",
        lambda: text_call_threads.append(threading.get_ident()) or base_url_text(),
    )
    monkeypatch.setattr(
        window.username_edit,
        "text",
        lambda: text_call_threads.append(threading.get_ident()) or username_text(),
    )
    monkeypatch.setattr(
        window.password_edit,
        "text",
        lambda: text_call_threads.append(threading.get_ident()) or password_text(),
    )

    window.base_url_edit.setText("http://server")
    window.username_edit.setText("bob")
    window.password_edit.setText("secret")

    with qtbot.waitSignal(window.login_succeeded, timeout=1000):
        window.login_button.click()
        qtbot.waitUntil(lambda: controller.login_calls == [("http://server", "bob", "secret")], timeout=1000)
        controller.finish_login(AppConfig(base_url="http://server", username="bob", token="token-123"))

    assert text_call_threads == [main_thread_id, main_thread_id, main_thread_id]
