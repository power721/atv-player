from PySide6.QtCore import QSize
from PySide6.QtGui import QImage

from atv_player.ui.poster_loader import (
    build_poster_request_headers,
    load_remote_poster_image,
    normalize_poster_url,
)


class FakeResponse:
    def __init__(self, content: bytes) -> None:
        self.content = content

    def raise_for_status(self) -> None:
        return None


def test_normalize_poster_url_upgrades_douban_ratio_path() -> None:
    result = normalize_poster_url("https://img3.doubanio.com/view/photo/s_ratio_poster/public/p123.jpg")
    assert result == "https://img3.doubanio.com/view/photo/m/public/p123.jpg"


def test_build_poster_request_headers_uses_site_specific_referers() -> None:
    assert build_poster_request_headers("https://img3.doubanio.com/view/photo/m/public/p123.jpg")["Referer"] == "https://movie.douban.com/"
    assert build_poster_request_headers("https://i.ytimg.com/vi/123/maxresdefault.jpg")["Referer"] == "https://www.youtube.com/"
    assert build_poster_request_headers("https://cc.163.com/cover.png")["Referer"] == "https://cc.163.com/"


def test_load_remote_poster_image_scales_downloaded_image() -> None:
    def fake_get(url: str, headers: dict[str, str], timeout: float) -> FakeResponse:
        png = QImage(20, 40, QImage.Format.Format_RGB32)
        png.fill(0x00FF00)

        from PySide6.QtCore import QBuffer, QByteArray, QIODeviceBase

        data = QByteArray()
        qbuffer = QBuffer(data)
        qbuffer.open(QIODeviceBase.OpenModeFlag.WriteOnly)
        png.save(qbuffer, "PNG")
        return FakeResponse(bytes(data))

    loaded = load_remote_poster_image(
        "https://img3.doubanio.com/view/photo/m/public/p123.jpg",
        QSize(90, 130),
        get=fake_get,
    )

    assert loaded is not None
    assert loaded.isNull() is False
    assert loaded.width() <= 90
    assert loaded.height() <= 130
