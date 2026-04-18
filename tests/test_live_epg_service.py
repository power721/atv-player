import gzip
from pathlib import Path

import pytest

from atv_player.live_epg_repository import LiveEpgRepository
from atv_player.live_epg_service import LiveEpgService


class FakeHttpBytesClient:
    def __init__(
        self,
        payload: bytes = b"",
        exc: Exception | None = None,
        responses: dict[str, bytes | Exception] | None = None,
    ) -> None:
        self.payload = payload
        self.exc = exc
        self.responses = responses or {}
        self.calls: list[str] = []

    def get_bytes(self, url: str) -> bytes:
        self.calls.append(url)
        if self.responses:
            result = self.responses[url]
            if isinstance(result, Exception):
                raise result
            return result
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
    assert schedule.upcoming == ["10:00-11:00 新闻30分"]


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
    assert schedule.upcoming == []


def test_live_epg_service_matches_channel_names_via_alias_map(tmp_path: Path) -> None:
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

    schedule = service.get_schedule("CCTV-1综合高清", now_text="2026-04-18T09:30:00+08:00")

    assert schedule is not None
    assert schedule.current == "09:00-10:00 朝闻天下"
    assert schedule.upcoming == []


def test_live_epg_service_matches_channel_names_after_stripping_resolution_suffixes(tmp_path: Path) -> None:
    repo = LiveEpgRepository(tmp_path / "app.db")
    repo.save_refresh_result(
        cache_text=(
            "<tv>"
            '<channel id="cctv10"><display-name>CCTV10</display-name></channel>'
            '<channel id="jsws"><display-name>江苏卫视</display-name></channel>'
            '<channel id="dfws"><display-name>东方卫视</display-name></channel>'
            '<channel id="sdws"><display-name>山东卫视</display-name></channel>'
            '<programme start="20260418090000 +0800" stop="20260418100000 +0800" channel="cctv10"><title>探索发现</title></programme>'
            '<programme start="20260418090000 +0800" stop="20260418100000 +0800" channel="jsws"><title>非诚勿扰</title></programme>'
            '<programme start="20260418090000 +0800" stop="20260418100000 +0800" channel="dfws"><title>看东方</title></programme>'
            '<programme start="20260418090000 +0800" stop="20260418100000 +0800" channel="sdws"><title>新闻联播山东版</title></programme>'
            "</tv>"
        ),
        last_refreshed_at=1,
        last_error="",
    )
    service = LiveEpgService(repo, FakeHttpBytesClient())

    cctv_schedule = service.get_schedule("CCTV-10HD", now_text="2026-04-18T09:30:00+08:00")
    jsws_schedule = service.get_schedule("江苏卫视HD", now_text="2026-04-18T09:30:00+08:00")
    dfws_schedule = service.get_schedule("东方卫视高清", now_text="2026-04-18T09:30:00+08:00")
    sdws_schedule = service.get_schedule("山东卫视超清", now_text="2026-04-18T09:30:00+08:00")

    assert cctv_schedule is not None
    assert cctv_schedule.current == "09:00-10:00 探索发现"
    assert jsws_schedule is not None
    assert jsws_schedule.current == "09:00-10:00 非诚勿扰"
    assert dfws_schedule is not None
    assert dfws_schedule.current == "09:00-10:00 看东方"
    assert sdws_schedule is not None
    assert sdws_schedule.current == "09:00-10:00 新闻联播山东版"


def test_live_epg_service_limits_upcoming_programmes_to_rest_of_same_day(tmp_path: Path) -> None:
    repo = LiveEpgRepository(tmp_path / "app.db")
    repo.save_refresh_result(
        cache_text=(
            "<tv>"
            '<channel id="c1"><display-name>CCTV-1</display-name></channel>'
            '<programme start="20260418220000 +0800" stop="20260418230000 +0800" channel="c1"><title>晚间新闻</title></programme>'
            '<programme start="20260418230000 +0800" stop="20260418235900 +0800" channel="c1"><title>午夜剧场</title></programme>'
            '<programme start="20260419000000 +0800" stop="20260419010000 +0800" channel="c1"><title>次日节目</title></programme>'
            "</tv>"
        ),
        last_refreshed_at=1,
        last_error="",
    )
    service = LiveEpgService(repo, FakeHttpBytesClient())

    schedule = service.get_schedule("CCTV-1", now_text="2026-04-18T22:30:00+08:00")

    assert schedule is not None
    assert schedule.current == "22:00-23:00 晚间新闻"
    assert schedule.upcoming == ["23:00-23:59 午夜剧场"]


def test_live_epg_service_refresh_preserves_old_cache_on_failure(tmp_path: Path) -> None:
    repo = LiveEpgRepository(tmp_path / "app.db")
    repo.save_url("https://example.com/epg.xml")
    repo.save_refresh_result(cache_text="<tv>old</tv>", last_refreshed_at=3, last_error="")
    service = LiveEpgService(repo, FakeHttpBytesClient(exc=RuntimeError("boom")))

    try:
        service.refresh()
    except RuntimeError as exc:
        assert str(exc) == "https://example.com/epg.xml: boom"
    else:
        raise AssertionError("expected refresh to fail")

    config = repo.load()
    assert config.cache_text == "<tv>old</tv>"
    assert config.last_refreshed_at == 3
    assert config.last_error == "https://example.com/epg.xml: boom"


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


def test_live_epg_service_refresh_merges_multiple_urls_and_prefers_earlier_programmes(
    tmp_path: Path,
) -> None:
    repo = LiveEpgRepository(tmp_path / "app.db")
    repo.save_url("https://example.com/one.xml\nhttps://example.com/two.xml")
    service = LiveEpgService(
        repo,
        FakeHttpBytesClient(
            responses={
                "https://example.com/one.xml": (
                    "<tv>"
                    '<channel id="c1"><display-name>CCTV-1</display-name></channel>'
                    '<programme start="20260418090000 +0800" stop="20260418100000 +0800" channel="c1"><title>第一源节目</title></programme>'
                    "</tv>"
                ).encode("utf-8"),
                "https://example.com/two.xml": (
                    "<tv>"
                    '<channel id="c1"><display-name>CCTV1综合</display-name></channel>'
                    '<channel id="c2"><display-name>CCTV-2</display-name></channel>'
                    '<programme start="20260418090000 +0800" stop="20260418100000 +0800" channel="c1"><title>第二源冲突节目</title></programme>'
                    '<programme start="20260418100000 +0800" stop="20260418110000 +0800" channel="c1"><title>第二源后续节目</title></programme>'
                    '<programme start="20260418090000 +0800" stop="20260418100000 +0800" channel="c2"><title>经济信息联播</title></programme>'
                    "</tv>"
                ).encode("utf-8"),
            }
        ),
    )

    service.refresh()

    config = repo.load()
    c1_schedule = service.get_schedule("CCTV-1", now_text="2026-04-18T09:30:00+08:00")
    c2_schedule = service.get_schedule("CCTV-2", now_text="2026-04-18T09:30:00+08:00")

    assert service._parse_epg_urls(config.epg_url) == [
        "https://example.com/one.xml",
        "https://example.com/two.xml",
    ]
    assert "第一源节目" in config.cache_text
    assert "第二源后续节目" in config.cache_text
    assert c1_schedule is not None
    assert c1_schedule.current == "09:00-10:00 第一源节目"
    assert c1_schedule.upcoming == ["10:00-11:00 第二源后续节目"]
    assert c2_schedule is not None
    assert c2_schedule.current == "09:00-10:00 经济信息联播"


def test_live_epg_service_refresh_keeps_cache_when_all_urls_fail(tmp_path: Path) -> None:
    repo = LiveEpgRepository(tmp_path / "app.db")
    repo.save_url("https://example.com/one.xml\nhttps://example.com/two.xml")
    repo.save_refresh_result(cache_text="<tv>old</tv>", last_refreshed_at=3, last_error="")
    service = LiveEpgService(
        repo,
        FakeHttpBytesClient(
            responses={
                "https://example.com/one.xml": RuntimeError("boom-one"),
                "https://example.com/two.xml": RuntimeError("boom-two"),
            }
        ),
    )

    with pytest.raises(RuntimeError, match="boom-one"):
        service.refresh()

    config = repo.load()
    assert config.cache_text == "<tv>old</tv>"
    assert config.last_refreshed_at == 3
    assert config.last_error == (
        "https://example.com/one.xml: boom-one\n"
        "https://example.com/two.xml: boom-two"
    )


def test_live_epg_service_refresh_records_partial_failure_without_raising(tmp_path: Path) -> None:
    repo = LiveEpgRepository(tmp_path / "app.db")
    repo.save_url("https://example.com/one.xml\nhttps://example.com/two.xml")
    service = LiveEpgService(
        repo,
        FakeHttpBytesClient(
            responses={
                "https://example.com/one.xml": RuntimeError("boom-one"),
                "https://example.com/two.xml": (
                    "<tv>"
                    '<channel id="c1"><display-name>CCTV-1</display-name></channel>'
                    '<programme start="20260418090000 +0800" stop="20260418100000 +0800" channel="c1"><title>成功节目</title></programme>'
                    "</tv>"
                ).encode("utf-8"),
            }
        ),
    )

    service.refresh()

    config = repo.load()
    schedule = service.get_schedule("CCTV-1", now_text="2026-04-18T09:30:00+08:00")

    assert "成功节目" in config.cache_text
    assert config.last_refreshed_at == 1
    assert config.last_error == "https://example.com/one.xml: boom-one"
    assert schedule is not None
    assert schedule.current == "09:00-10:00 成功节目"
