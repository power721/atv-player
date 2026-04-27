from atv_player.danmaku.errors import ProviderNotSupportedError
from atv_player.danmaku.models import DanmakuRecord, DanmakuSearchItem
from atv_player.danmaku.service import DanmakuService


class FakeProvider:
    def __init__(self, key: str, items: list[DanmakuSearchItem], records: list[DanmakuRecord]) -> None:
        self.key = key
        self.items = items
        self.records = records
        self.search_calls: list[str] = []
        self.resolve_calls: list[str] = []

    def search(self, name: str) -> list[DanmakuSearchItem]:
        self.search_calls.append(name)
        return list(self.items)

    def resolve(self, page_url: str) -> list[DanmakuRecord]:
        self.resolve_calls.append(page_url)
        return list(self.records)

    def supports(self, page_url: str) -> bool:
        return self.key in page_url


def test_search_danmu_prefers_provider_from_reg_src() -> None:
    tencent = FakeProvider(
        "tencent",
        [DanmakuSearchItem(provider="tencent", name="剑来 第1集", url="https://tencent/item", ratio=0.9, simi=0.8)],
        [],
    )
    youku = FakeProvider(
        "youku",
        [DanmakuSearchItem(provider="youku", name="剑来 第1集", url="https://youku/item", ratio=0.8, simi=0.8)],
        [],
    )
    service = DanmakuService({"tencent": tencent, "youku": youku}, provider_order=["tencent", "youku"])

    results = service.search_danmu("剑来 第1集", "https://v.qq.com/x/cover/demo.html")

    assert [item.provider for item in results] == ["tencent"]
    assert tencent.search_calls == ["剑来 第1集"]
    assert youku.search_calls == []


def test_search_danmu_aggregates_and_sorts_results_without_reg_src() -> None:
    tencent = FakeProvider(
        "tencent",
        [DanmakuSearchItem(provider="tencent", name="剑来 第1集", url="https://tencent/item", ratio=0.82, simi=0.82)],
        [],
    )
    youku = FakeProvider(
        "youku",
        [DanmakuSearchItem(provider="youku", name="剑来 第1集", url="https://youku/item", ratio=0.91, simi=0.91)],
        [],
    )
    service = DanmakuService({"tencent": tencent, "youku": youku}, provider_order=["tencent", "youku"])

    results = service.search_danmu("剑来 第1集")

    assert [item.provider for item in results] == ["youku", "tencent"]


def test_search_danmu_falls_back_to_default_order_for_unknown_reg_src() -> None:
    tencent = FakeProvider(
        "tencent",
        [DanmakuSearchItem(provider="tencent", name="剑来 第1集", url="https://tencent/item", ratio=0.82, simi=0.82)],
        [],
    )
    youku = FakeProvider(
        "youku",
        [DanmakuSearchItem(provider="youku", name="剑来 第1集", url="https://youku/item", ratio=0.91, simi=0.91)],
        [],
    )
    service = DanmakuService({"tencent": tencent, "youku": youku}, provider_order=["tencent", "youku"])

    results = service.search_danmu("剑来 第1集", "https://unknown.example/video/1")

    assert [item.provider for item in results] == ["youku", "tencent"]


def test_resolve_danmu_dispatches_by_url_and_builds_xml() -> None:
    tencent = FakeProvider(
        "tencent",
        [],
        [DanmakuRecord(time_offset=1.0, pos=1, color="16777215", content="hello")],
    )
    service = DanmakuService({"tencent": tencent}, provider_order=["tencent"])

    xml = service.resolve_danmu("https://video.tencent/item")

    assert '<d p="1.0,1,25,16777215">hello</d>' in xml
    assert tencent.resolve_calls == ["https://video.tencent/item"]


def test_resolve_danmu_raises_for_unknown_provider_url() -> None:
    service = DanmakuService({}, provider_order=[])

    try:
        service.resolve_danmu("https://unknown.example/video/1")
    except ProviderNotSupportedError:
        pass
    else:
        raise AssertionError("Expected ProviderNotSupportedError")
