from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass

import httpx


def _looks_like_media_url(value: str) -> bool:
    return bool(re.search(r"\.(m3u8|mp4|rmvb|avi|wmv|flv|mkv|webm|mov|m3u)(?!\w)", value.strip(), re.IGNORECASE))


def _normalize_headers(raw_headers) -> dict[str, str]:
    if not raw_headers:
        return {}
    if isinstance(raw_headers, Mapping):
        return {str(key): str(value) for key, value in raw_headers.items()}
    if isinstance(raw_headers, str):
        try:
            parsed = json.loads(raw_headers)
        except json.JSONDecodeError:
            return {}
        if isinstance(parsed, Mapping):
            return {str(key): str(value) for key, value in parsed.items()}
    return {}


@dataclass(frozen=True, slots=True)
class BuiltInPlaybackParser:
    key: str
    label: str
    api: str
    headers: dict[str, str]


@dataclass(frozen=True, slots=True)
class BuiltInPlaybackParserResult:
    parser_key: str
    parser_label: str
    url: str
    headers: dict[str, str]


class BuiltInPlaybackParserService:
    def __init__(self, get: Callable[..., httpx.Response] = httpx.get) -> None:
        self._get = get
        self._parsers = [
            BuiltInPlaybackParser(
                key="fish",
                label="fish",
                api="https://kalbim.xatut.top/kalbim2025/781718/play/video_player.php",
                headers={
                    "user-agent": "Mozilla/5.0 (Linux; Android 10; MI 8 Build/QKQ1.190828.002; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/83.0.4103.101 Mobile Safari/537.36 bsl/1.0;webank/h5face;webank/2.0"
                },
            ),
            BuiltInPlaybackParser(
                key="jx1",
                label="jx1",
                api="http://sspa8.top:8100/api/?key=1060089351&",
                headers={
                    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/110.0.0.0 Safari/537.36 Edg/110.0.1587.57"
                },
            ),
            BuiltInPlaybackParser(
                key="jx2",
                label="jx2",
                api="http://sspa8.top:8100/api/?cat_ext=eyJmbGFnIjpbInFxIiwi6IW+6K6vIiwicWl5aSIsIueIseWlh+iJuiIsIuWlh+iJuiIsInlvdWt1Iiwi5LyY6YW3Iiwic29odSIsIuaQnOeLkCIsImxldHYiLCLkuZDop4YiLCJtZ3R2Iiwi6IqS5p6cIiwidG5tYiIsInNldmVuIiwiYmlsaWJpbGkiLCIxOTA1Il0sImhlYWRlciI6eyJVc2VyLUFnZW50Ijoib2todHRwLzQuOS4xIn19&key=星睿4k&",
                headers={
                    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/110.0.0.0 Safari/537.36 Edg/110.0.1587.57"
                },
            ),
            BuiltInPlaybackParser(
                key="mg1",
                label="mg1",
                api="http://shybot.top/v2/video/jx/?shykey=4595a71a4e7712568edcfa43949236b42fcfcb04997788ebe7984d6da2c6a51c&qn=max&",
                headers={
                    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/110.0.0.0 Safari/537.36 Edg/110.0.1587.57"
                },
            ),
            BuiltInPlaybackParser(
                key="tx1",
                label="tx1",
                api="http://shybot.top/v2/video/jx/?shykey=4595a71a4e7712568edcfa43949236b42fcfcb04997788ebe7984d6da2c6a51c&",
                headers={
                    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/110.0.0.0 Safari/537.36 Edg/110.0.1587.57"
                },
            ),
        ]

    def parsers(self) -> list[BuiltInPlaybackParser]:
        return list(self._parsers)

    def resolve(self, flag: str, url: str, preferred_key: str = "") -> BuiltInPlaybackParserResult:
        if not url.strip():
            raise ValueError("解析失败: 缺少待解析地址")
        errors: list[str] = []
        for parser in self._ordered_parsers(preferred_key):
            try:
                response = self._get(
                    parser.api,
                    params={"flag": flag, "url": url},
                    headers=dict(parser.headers),
                    timeout=15.0,
                    follow_redirects=True,
                )
                payload = response.json()
                media_url = str(payload.get("url") or "").strip()
                if payload.get("parse") == 0 or payload.get("jx") == 0 or _looks_like_media_url(media_url):
                    if not _looks_like_media_url(media_url):
                        raise ValueError("返回地址不可播放")
                    return BuiltInPlaybackParserResult(
                        parser_key=parser.key,
                        parser_label=parser.label,
                        url=media_url,
                        headers=_normalize_headers(payload.get("header") or payload.get("headers")),
                    )
                raise ValueError("返回结果仍需解析")
            except Exception as exc:
                errors.append(f"{parser.key}: {exc}")
        raise ValueError(f"解析失败: {'; '.join(errors)}")

    def _ordered_parsers(self, preferred_key: str) -> list[BuiltInPlaybackParser]:
        if not preferred_key:
            return self.parsers()
        preferred = [parser for parser in self._parsers if parser.key == preferred_key]
        remaining = [parser for parser in self._parsers if parser.key != preferred_key]
        return [*preferred, *remaining]
