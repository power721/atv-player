import gzip
from pathlib import Path

from atv_player.live_epg_repository import LiveEpgRepository
from atv_player.live_epg_service import LiveEpgService


class FakeHttpBytesClient:
    def __init__(self, payload: bytes = b"", exc: Exception | None = None) -> None:
        self.payload = payload
        self.exc = exc
        self.calls: list[str] = []

    def get_bytes(self, url: str) -> bytes:
        self.calls.append(url)
        if self.exc is not None:
            raise self.exc
        return self.payload


def test_live_epg_service_returns_current_and_next_programme_from_cached_xmltv(tmp_path: Path) -> None:
    repo = LiveEpgRepository(tmp_path / "app.db")
    repo.save_refresh_result(
        cache_text=(
            "<tv>"
            '<channel id="c1"><display-name>CCTV-1</display-name></channel>'
            '<programme start="20260418090000 +0800" stop="20260418100000 +0800" channel="c1"><title>朝闻天下</title></programme>'
            '<programme start="20260418100000 +0800" stop="20260418110000 +0800" channel="c1"><title>新闻30分</title></programme>'
            "</tv>"
        ),
        last_refreshed_at=1,
        last_error="",
    )
    service = LiveEpgService(repo, FakeHttpBytesClient())

    schedule = service.get_schedule("CCTV-1", now_text="2026-04-18T09:30:00+08:00")

    assert schedule is not None
    assert schedule.current == "09:00-10:00 朝闻天下"
    assert schedule.next == "10:00-11:00 新闻30分"


def test_live_epg_service_matches_cctv_names_after_normalization(tmp_path: Path) -> None:
    repo = LiveEpgRepository(tmp_path / "app.db")
    repo.save_refresh_result(
        cache_text=(
            "<tv>"
            '<channel id="c1"><display-name>CCTV1综合</display-name></channel>'
            '<programme start="20260418090000 +0800" stop="20260418100000 +0800" channel="c1"><title>朝闻天下</title></programme>'
            "</tv>"
        ),
        last_refreshed_at=1,
        last_error="",
    )
    service = LiveEpgService(repo, FakeHttpBytesClient())

    schedule = service.get_schedule("CCTV-1综合", now_text="2026-04-18T09:30:00+08:00")

    assert schedule is not None
    assert schedule.current == "09:00-10:00 朝闻天下"
    assert schedule.next == ""


def test_live_epg_service_refresh_preserves_old_cache_on_failure(tmp_path: Path) -> None:
    repo = LiveEpgRepository(tmp_path / "app.db")
    repo.save_url("https://example.com/epg.xml")
    repo.save_refresh_result(cache_text="<tv>old</tv>", last_refreshed_at=3, last_error="")
    service = LiveEpgService(repo, FakeHttpBytesClient(exc=RuntimeError("boom")))

    try:
        service.refresh()
    except RuntimeError as exc:
        assert str(exc) == "boom"
    else:
        raise AssertionError("expected refresh to fail")

    config = repo.load()
    assert config.cache_text == "<tv>old</tv>"
    assert config.last_refreshed_at == 3
    assert config.last_error == "boom"


def test_live_epg_service_decompresses_gzip_xmltv_payload(tmp_path: Path) -> None:
    repo = LiveEpgRepository(tmp_path / "app.db")
    repo.save_url("https://example.com/e9.xml.gz")
    payload = gzip.compress(
        (
            "<tv>"
            '<channel id="c1"><display-name>CCTV-1</display-name></channel>'
            '<programme start="20260418090000 +0800" stop="20260418100000 +0800" channel="c1"><title>朝闻天下</title></programme>'
            "</tv>"
        ).encode("utf-8")
    )
    service = LiveEpgService(repo, FakeHttpBytesClient(payload=payload))

    service.refresh()

    assert "CCTV-1" in repo.load().cache_text
