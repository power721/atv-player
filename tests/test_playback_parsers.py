import httpx
import pytest

from atv_player.playback_parsers import BuiltInPlaybackParserService


def test_parser_service_tries_saved_parser_first_and_falls_back() -> None:
    calls: list[tuple[str, dict[str, str]]] = []

    def fake_get(url: str, params: dict[str, str], headers: dict[str, str], timeout: float, follow_redirects: bool):
        calls.append((url, headers))
        if "sspa8.top:8100/api/?key=1060089351&" in url:
            return httpx.Response(200, json={"parse": 1, "jx": 1, "url": "https://page.example/watch"})
        if "kalbim.xatut.top/kalbim2025/781718/play/video_player.php" in url:
            return httpx.Response(200, json={"parse": 1, "jx": 1, "url": "https://page.example/watch"})
        return httpx.Response(200, json={"parse": 0, "jx": 0, "url": "https://media.example/real.m3u8"})

    service = BuiltInPlaybackParserService(get=fake_get)

    result = service.resolve("qq", "https://site.example/play?id=1", preferred_key="jx1")

    assert result.parser_key == "jx2"
    assert result.url == "https://media.example/real.m3u8"
    assert [url for url, _headers in calls][:3] == [
        "http://sspa8.top:8100/api/?key=1060089351&",
        "https://kalbim.xatut.top/kalbim2025/781718/play/video_player.php",
        "http://sspa8.top:8100/api/?cat_ext=eyJmbGFnIjpbInFxIiwi6IW+6K6vIiwicWl5aSIsIueIseWlh+iJuiIsIuWlh+iJuiIsInlvdWt1Iiwi5LyY6YW3Iiwic29odSIsIuaQnOeLkCIsImxldHYiLCLkuZDop4YiLCJtZ3R2Iiwi6IqS5p6cIiwidG5tYiIsInNldmVuIiwiYmlsaWJpbGkiLCIxOTA1Il0sImhlYWRlciI6eyJVc2VyLUFnZW50Ijoib2todHRwLzQuOS4xIn19&key=星睿4k&",
    ]


def test_parser_service_uses_response_headers_payload() -> None:
    def fake_get(url: str, params: dict[str, str], headers: dict[str, str], timeout: float, follow_redirects: bool):
        return httpx.Response(
            200,
            json={
                "parse": 0,
                "jx": 0,
                "url": "https://media.example/real.m3u8",
                "header": {"Referer": "https://site.example"},
            },
        )

    service = BuiltInPlaybackParserService(get=fake_get)

    result = service.resolve("qq", "https://site.example/play?id=2", preferred_key="fish")

    assert result.parser_key == "fish"
    assert result.headers == {"Referer": "https://site.example"}


def test_parser_service_uses_response_headers_alias_payload() -> None:
    def fake_get(url: str, params: dict[str, str], headers: dict[str, str], timeout: float, follow_redirects: bool):
        return httpx.Response(
            200,
            json={
                "parse": 0,
                "jx": 0,
                "url": "https://media.example/real.m3u8",
                "headers": '{"User-Agent":"UA","Referer":"https://site.example"}',
            },
        )

    service = BuiltInPlaybackParserService(get=fake_get)

    result = service.resolve("qq", "https://site.example/play?id=2", preferred_key="fish")

    assert result.headers == {
        "User-Agent": "UA",
        "Referer": "https://site.example",
    }


def test_parser_service_raises_when_all_parsers_fail() -> None:
    def fake_get(url: str, params: dict[str, str], headers: dict[str, str], timeout: float, follow_redirects: bool):
        return httpx.Response(200, json={"parse": 1, "jx": 1, "url": "https://page.example/watch"})

    service = BuiltInPlaybackParserService(get=fake_get)

    with pytest.raises(ValueError, match="解析失败"):
        service.resolve("qq", "https://site.example/play?id=3")
