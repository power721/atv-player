from atv_player.app import decide_start_view
from atv_player.models import AppConfig
from atv_player.ui.main_window import MainWindow


class FakeBrowseController:
    pass


class FakeHistoryController:
    pass


class FakePlayerController:
    pass


def test_main_window_starts_on_browse_tab(qtbot) -> None:
    window = MainWindow(
        browse_controller=FakeBrowseController(),
        history_controller=FakeHistoryController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
    )

    qtbot.addWidget(window)
    window.show()

    assert window.nav_tabs.currentIndex() == 0


def test_decide_start_view_prefers_login_without_token() -> None:
    assert decide_start_view(AppConfig(token="")) == "login"


def test_decide_start_view_uses_main_window_with_token() -> None:
    assert decide_start_view(AppConfig(token="token-123")) == "main"
