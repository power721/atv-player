import threading

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QPushButton

from atv_player.following_models import FollowingCardItem, FollowingRecord
from atv_player.ui.following_page import FollowingPage


class FakeFollowingController:
    def __init__(self) -> None:
        self.check_all_calls = 0
        self.only_updates_seen: list[bool] = []

    def load_page(self, *, page: int, size: int, keyword: str, only_updates: bool):
        del page, size, keyword
        self.only_updates_seen.append(only_updates)
        record = FollowingRecord(
            id=1,
            title="凡人修仙传",
            provider="bangumi",
            provider_id="subject:1",
            current_episode=127,
            latest_episode=128,
            total_episodes=156,
            has_update=True,
        )
        return [
            FollowingCardItem(
                record=record,
                display_title=record.title,
                subtitle="Bangumi",
                progress_text="看到 127 · 最新 128 / 总 156",
                update_text="有 1 集更新",
                updated_hint=True,
            )
        ], 1

    def check_all_due(self) -> None:
        self.check_all_calls += 1


def test_following_page_renders_update_card_and_emits_detail(qtbot) -> None:
    controller = FakeFollowingController()
    page = FollowingPage(controller)
    qtbot.addWidget(page)
    opened: list[int] = []
    page.open_detail_requested.connect(opened.append)

    page.ensure_loaded()
    qtbot.mouseClick(page.card_widgets[0], Qt.MouseButton.LeftButton)

    assert page.records[0].display_title == "凡人修仙传"
    assert page.records[0].updated_hint is True
    assert "共 1 条" in page.status_label.text()
    assert "1 条有更新" in page.status_label.text()
    assert "color:" in page.card_widgets[0].title_label.styleSheet()
    assert "color:" in page.card_widgets[0].progress_label.styleSheet()
    assert "color:" in page.card_widgets[0].update_label.styleSheet()
    assert page.card_widgets[0].poster_label.height() >= 220
    assert page.card_widgets[0].height() >= 320
    assert page.card_widgets[0].layout().stretch(page.card_widgets[0].layout().count() - 1) == 0
    assert opened == [1]


def test_following_card_keeps_long_progress_text_below_poster(qtbot) -> None:
    item = FollowingCardItem(
        record=FollowingRecord(
            id=1,
            title="航海王",
            provider="tmdb",
            provider_id="tv:37854",
        ),
        display_title="航海王",
        subtitle="动漫",
        progress_text="看到 S15E581 · 最新 S23E1163 / 总 1181",
        update_text="有 582 集更新",
        updated_hint=True,
    )
    page = FollowingPage(FakeFollowingController())
    qtbot.addWidget(page)

    card = page.card_widgets[0] if page.card_widgets else None
    page._render_cards([item])
    card = page.card_widgets[0]
    card.show()
    qtbot.wait(0)

    poster_bottom = card.poster_label.geometry().bottom()
    title_top = card.title_label.geometry().top()
    progress_top = card.progress_label.geometry().top()
    update_bottom = card.update_label.geometry().bottom()

    assert card.height() >= 340
    assert title_top > poster_bottom
    assert progress_top > card.title_label.geometry().bottom()
    assert update_bottom <= card.contentsRect().bottom()


def test_following_page_centers_content_like_favorites_page(qtbot) -> None:
    page = FollowingPage(FakeFollowingController())
    qtbot.addWidget(page)

    layout = page.layout()
    content_widget = layout.itemAt(1).widget()

    assert layout.count() == 3
    assert layout.itemAt(0).spacerItem() is not None
    assert content_widget is not None
    assert content_widget.maximumWidth() == 1800
    assert layout.itemAt(2).spacerItem() is not None


def test_following_page_filters_updates_and_runs_manual_check(qtbot) -> None:
    controller = FakeFollowingController()
    page = FollowingPage(controller)
    qtbot.addWidget(page)

    page.only_updates_checkbox.setChecked(True)
    page.load_page()
    page.check_updates_button.click()

    assert controller.only_updates_seen[-1] is True
    assert controller.check_all_calls == 1
    assert page.status_label.text().startswith("已检查更新")


def test_following_page_shows_empty_status(qtbot) -> None:
    class EmptyController(FakeFollowingController):
        def load_page(self, *, page: int, size: int, keyword: str, only_updates: bool):
            del page, size, keyword, only_updates
            return [], 0

    page = FollowingPage(EmptyController())
    qtbot.addWidget(page)

    page.ensure_loaded()

    assert page.card_widgets == []
    assert page.status_label.text() == "没有追更记录"


def test_following_page_loads_card_poster(qtbot, tmp_path) -> None:
    poster_path = tmp_path / "poster.png"
    image = QImage(24, 24, QImage.Format.Format_RGB32)
    image.fill(QColor("#ff0000"))
    assert image.save(str(poster_path))

    class PosterController(FakeFollowingController):
        def load_page(self, *, page: int, size: int, keyword: str, only_updates: bool):
            items, total = super().load_page(
                page=page,
                size=size,
                keyword=keyword,
                only_updates=only_updates,
            )
            items[0].record.poster = str(poster_path)
            return items, total

    page = FollowingPage(PosterController())
    qtbot.addWidget(page)

    page.ensure_loaded()

    qtbot.waitUntil(
        lambda: page.card_widgets[0].poster_label.pixmap() is not None
        and not page.card_widgets[0].poster_label.pixmap().isNull(),
        timeout=1000,
    )


def test_following_page_import_backend_runs_off_ui_thread(qtbot, monkeypatch) -> None:
    class ImportResult:
        summary_text = "共 1 条订阅，导入 1 条"

    class ImportController(FakeFollowingController):
        def __init__(self) -> None:
            super().__init__()
            self.import_calls = 0
            self.worker_thread_names: list[str] = []
            self.progress_seen: list[tuple[int, int, str]] = []

        def backend_sync_available(self) -> bool:
            return True

        def import_backend_subscriptions(self, *, progress_callback=None, cancel_callback=None):
            self.import_calls += 1
            self.worker_thread_names.append(threading.current_thread().name)
            if progress_callback is not None:
                progress_callback(0, 1, "正在导入 测试剧...")
            return ImportResult()

    shown: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "atv_player.ui.following_page.QMessageBox.information",
        staticmethod(lambda parent, title, text: shown.append((title, text))),
    )

    controller = ImportController()
    page = FollowingPage(controller)
    qtbot.addWidget(page)
    page.ensure_loaded()
    loads_before = len(controller.only_updates_seen)
    received: list[tuple[int, int, str]] = []
    page.import_progress.connect(lambda *args: received.append(args))

    page.import_backend_button.click()
    dialog = page._import_progress_dialog
    assert dialog is not None
    assert not page.import_backend_button.isEnabled()

    # 取消走事件:点击取消按钮 → canceled 信号 → 置位,worker 侧通过回调在下一条目边界感知。
    # (QProgressDialog.cancel() 编程调用不发 canceled,须点真实按钮。)
    cancel_button = next(b for b in dialog.findChildren(QPushButton) if b.text() == "取消")
    cancel_button.click()
    assert page._import_cancel_event is not None and page._import_cancel_event.is_set()

    qtbot.waitUntil(lambda: bool(shown), timeout=5000)

    assert controller.import_calls == 1
    assert controller.worker_thread_names[0] != threading.main_thread().name
    assert received == [(0, 1, "正在导入 测试剧...")]
    assert shown[0] == ("从服务端导入", "共 1 条订阅，导入 1 条")
    assert page._import_progress_dialog is None
    assert page.import_backend_button.isEnabled()
    assert len(controller.only_updates_seen) == loads_before + 1
