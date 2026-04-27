from atv_player.danmaku.models import DanmakuRecord, DanmakuSearchItem


class IqiyiDanmakuProvider:
    key = "iqiyi"

    def search(self, name: str) -> list[DanmakuSearchItem]:
        raise NotImplementedError("iQIYI danmaku search requires brotli + protobuf support")

    def resolve(self, page_url: str) -> list[DanmakuRecord]:
        raise NotImplementedError("iQIYI danmaku resolution requires brotli + protobuf support")

    def supports(self, page_url: str) -> bool:
        return "iqiyi.com" in page_url
