from atv_player.danmaku.models import DanmakuRecord, DanmakuSearchItem


class MgtvDanmakuProvider:
    key = "mgtv"

    def search(self, name: str) -> list[DanmakuSearchItem]:
        raise NotImplementedError("MGTV danmaku search requires signed request support")

    def resolve(self, page_url: str) -> list[DanmakuRecord]:
        raise NotImplementedError("MGTV danmaku resolution requires signed request support")

    def supports(self, page_url: str) -> bool:
        return "mgtv.com" in page_url
