import os
import time

import atv_player.danmaku.cache as danmaku_cache_module
from atv_player.danmaku.models import DanmakuSourceGroup, DanmakuSourceOption, DanmakuSourceSearchResult


def test_load_or_create_danmaku_ass_cache_reuses_existing_file(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(danmaku_cache_module, "app_cache_dir", lambda: tmp_path / "app-cache")
    xml_text = '<?xml version="1.0" encoding="UTF-8"?><i><d p="0.0,1,25,16777215">第一条</d></i>'

    first_path = danmaku_cache_module.load_or_create_danmaku_ass_cache(xml_text, 1)
    second_path = danmaku_cache_module.load_or_create_danmaku_ass_cache(xml_text, 1)

    assert first_path is not None
    assert second_path == first_path
    assert first_path.exists()
    assert first_path.read_text(encoding="utf-8").startswith("[Script Info]")


def test_purge_stale_danmaku_cache_deletes_files_older_than_three_days(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(danmaku_cache_module, "app_cache_dir", lambda: tmp_path / "app-cache")
    cache_dir = danmaku_cache_module.danmaku_cache_dir()
    old_file = cache_dir / "old.ass"
    new_file = cache_dir / "new.ass"
    old_file.write_text("old", encoding="utf-8")
    new_file.write_text("new", encoding="utf-8")
    now = time.time()
    stale_age = now - (4 * 24 * 60 * 60)
    fresh_age = now - (1 * 24 * 60 * 60)
    os.utime(old_file, (stale_age, stale_age))
    os.utime(new_file, (fresh_age, fresh_age))

    danmaku_cache_module.purge_stale_danmaku_cache(now=now)

    assert old_file.exists() is False
    assert new_file.exists() is True


def test_save_and_load_cached_danmaku_xml(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(danmaku_cache_module, "app_cache_dir", lambda: tmp_path / "app-cache")
    xml_text = '<?xml version="1.0" encoding="UTF-8"?><i><d p="0.0,1,25,16777215">第一条</d></i>'

    cache_path = danmaku_cache_module.save_cached_danmaku_xml("剑来 10集", "/play/10", xml_text)

    assert cache_path is not None
    assert danmaku_cache_module.load_cached_danmaku_xml("剑来 10集", "/play/10") == xml_text


def test_save_and_load_cached_danmaku_source_search_result(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(danmaku_cache_module, "app_cache_dir", lambda: tmp_path / "app-cache")
    result = DanmakuSourceSearchResult(
        groups=[
            DanmakuSourceGroup(
                provider="tencent",
                provider_label="腾讯",
                preferred_by_history=True,
                options=[
                    DanmakuSourceOption(
                        provider="tencent",
                        name="剑来 第10集",
                        url="https://v.qq.com/demo",
                        ratio=0.98,
                        simi=0.97,
                        duration_seconds=1320,
                        episode_match=True,
                        preferred_by_history=True,
                        resolve_ready=True,
                    )
                ],
            )
        ],
        default_option_url="https://v.qq.com/demo",
        default_provider="tencent",
    )

    cache_path = danmaku_cache_module.save_cached_danmaku_source_search_result("剑来 10集", "/play/10", result)
    loaded = danmaku_cache_module.load_cached_danmaku_source_search_result("剑来 10集", "/play/10")

    assert cache_path is not None
    assert loaded == result
