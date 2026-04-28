from atv_player.danmaku.models import DanmakuRecord, DanmakuSearchItem


class BilibiliDanmakuProvider:
    key = "bilibili"

    def search(self, name: str) -> list[DanmakuSearchItem]:
        return []

    def resolve(self, page_url: str) -> list[DanmakuRecord]:
        raise NotImplementedError("Bilibili danmaku resolution is not implemented yet")

    def supports(self, page_url: str) -> bool:
        return "bilibili.com" in page_url or "b23.tv" in page_url
