from __future__ import annotations

import gzip
import re
import time
from dataclasses import dataclass
from datetime import datetime
from xml.etree import ElementTree


@dataclass(slots=True)
class EpgSchedule:
    current: str = ""
    upcoming: list[str] | None = None


class LiveEpgService:
    _RESOLUTION_SUFFIX_PATTERN = re.compile(r"(hd|uhd|fhd|高清|超清|标清)+$", re.IGNORECASE)

    def __init__(self, repository, http_client) -> None:
        self._repository = repository
        self._http_client = http_client

    def load_config(self):
        return self._repository.load()

    def save_url(self, epg_url: str) -> None:
        self._repository.save_url(epg_url)

    def refresh(self) -> None:
        config = self._repository.load()
        urls = self._parse_epg_urls(config.epg_url)
        if not urls:
            return
        errors: list[str] = []
        merged_channel_names: dict[str, list[str]] = {}
        merged_programmes: list[dict[str, object]] = []
        try:
            for url in urls:
                try:
                    text = self._load_xmltv_text(url)
                    channel_names_by_id, programmes = self._parse_xmltv(text)
                except Exception as exc:
                    errors.append(f"{url}: {exc}")
                    continue
                merged_channel_names, merged_programmes = self._merge_xmltv(
                    merged_channel_names,
                    merged_programmes,
                    channel_names_by_id,
                    programmes,
                )
        except Exception as exc:
            self._repository.save_refresh_result(
                cache_text=config.cache_text,
                last_refreshed_at=config.last_refreshed_at,
                last_error=str(exc),
            )
            raise
        if not merged_channel_names and not merged_programmes:
            message = "\n".join(errors) or "没有可用的 EPG URL"
            self._repository.save_refresh_result(
                cache_text=config.cache_text,
                last_refreshed_at=config.last_refreshed_at,
                last_error=message,
            )
            raise RuntimeError(message)
        text = self._serialize_xmltv(merged_channel_names, merged_programmes)
        refreshed_at = int(time.time())
        self._repository.save_refresh_result(
            cache_text=text,
            last_refreshed_at=refreshed_at,
            last_error="\n".join(errors),
        )

    def get_schedule(self, channel_name: str, *, now_text: str | None = None) -> EpgSchedule | None:
        config = self._repository.load()
        if not config.cache_text.strip():
            return None
        now = datetime.fromisoformat(now_text) if now_text else datetime.now().astimezone()
        channel_names_by_id, programmes = self._parse_xmltv(config.cache_text)
        channel_id = self._match_channel_id(channel_name, channel_names_by_id)
        if not channel_id:
            return None
        current = None
        upcoming: list[str] = []
        for item in programmes:
            if item["channel"] != channel_id:
                continue
            if item["start"] <= now < item["stop"]:
                current = item
                continue
            if current is not None and item["start"] >= current["stop"]:
                start = item["start"]
                assert isinstance(start, datetime)
                if start.date() != now.date():
                    break
                upcoming.append(self._format_programme(item))
        if current is None:
            return None
        return EpgSchedule(
            current=self._format_programme(current),
            upcoming=upcoming,
        )

    def _load_xmltv_text(self, url: str) -> str:
        payload = self._http_client.get_bytes(url)
        if payload[:2] == b"\x1f\x8b":
            payload = gzip.decompress(payload)
        return payload.decode("utf-8")

    def _parse_epg_urls(self, value: str) -> list[str]:
        urls: list[str] = []
        for line in value.splitlines():
            url = line.strip()
            if not url or url in urls:
                continue
            urls.append(url)
        return urls

    def _parse_xmltv(self, text: str) -> tuple[dict[str, list[str]], list[dict[str, object]]]:
        root = ElementTree.fromstring(text)
        channel_names_by_id: dict[str, list[str]] = {}
        for channel in root.findall("channel"):
            channel_id = (channel.get("id") or "").strip()
            if not channel_id:
                continue
            names = [
                (display_name.text or "").strip()
                for display_name in channel.findall("display-name")
                if (display_name.text or "").strip()
            ]
            channel_names_by_id[channel_id] = names

        programmes: list[dict[str, object]] = []
        for programme in root.findall("programme"):
            channel_id = (programme.get("channel") or "").strip()
            start_text = (programme.get("start") or "").strip()
            stop_text = (programme.get("stop") or "").strip()
            title_node = programme.find("title")
            title = (title_node.text or "").strip() if title_node is not None else ""
            if not channel_id or not start_text or not stop_text or not title:
                continue
            programmes.append(
                {
                    "channel": channel_id,
                    "start": datetime.strptime(start_text, "%Y%m%d%H%M%S %z"),
                    "stop": datetime.strptime(stop_text, "%Y%m%d%H%M%S %z"),
                    "title": title,
                }
            )
        programmes.sort(key=lambda item: (item["channel"], item["start"]))
        return channel_names_by_id, programmes

    def _merge_xmltv(
        self,
        merged_channel_names: dict[str, list[str]],
        merged_programmes: list[dict[str, object]],
        channel_names_by_id: dict[str, list[str]],
        programmes: list[dict[str, object]],
    ) -> tuple[dict[str, list[str]], list[dict[str, object]]]:
        for channel_id, names in channel_names_by_id.items():
            existing_names = merged_channel_names.setdefault(channel_id, [])
            for name in names:
                if name not in existing_names:
                    existing_names.append(name)

        seen_programmes = {
            (item["channel"], item["start"], item["stop"])
            for item in merged_programmes
        }
        for item in programmes:
            key = (item["channel"], item["start"], item["stop"])
            if key in seen_programmes:
                continue
            merged_programmes.append(item)
            seen_programmes.add(key)

        merged_programmes.sort(key=lambda item: (item["channel"], item["start"]))
        return merged_channel_names, merged_programmes

    def _serialize_xmltv(
        self,
        channel_names_by_id: dict[str, list[str]],
        programmes: list[dict[str, object]],
    ) -> str:
        root = ElementTree.Element("tv")
        for channel_id in sorted(channel_names_by_id):
            channel = ElementTree.SubElement(root, "channel", {"id": channel_id})
            for name in channel_names_by_id[channel_id]:
                display_name = ElementTree.SubElement(channel, "display-name")
                display_name.text = name
        for programme in programmes:
            start = programme["start"]
            stop = programme["stop"]
            title = programme["title"]
            assert isinstance(start, datetime)
            assert isinstance(stop, datetime)
            assert isinstance(title, str)
            node = ElementTree.SubElement(
                root,
                "programme",
                {
                    "channel": str(programme["channel"]),
                    "start": start.strftime("%Y%m%d%H%M%S %z"),
                    "stop": stop.strftime("%Y%m%d%H%M%S %z"),
                },
            )
            title_node = ElementTree.SubElement(node, "title")
            title_node.text = title
        return ElementTree.tostring(root, encoding="unicode")

    def _match_channel_id(self, channel_name: str, channel_names_by_id: dict[str, list[str]]) -> str:
        target = channel_name.strip()
        if not target:
            return ""
        for channel_id, names in channel_names_by_id.items():
            if target in names:
                return channel_id
        normalized_target = self._normalize_name(target)
        for channel_id, names in channel_names_by_id.items():
            if any(self._normalize_name(name) == normalized_target for name in names):
                return channel_id
        return ""

    def _normalize_name(self, value: str) -> str:
        normalized = (
            value.strip()
            .lower()
            .replace(" ", "")
            .replace("-", "")
            .replace("_", "")
            .replace("（", "(")
            .replace("）", ")")
        )
        normalized = self._RESOLUTION_SUFFIX_PATTERN.sub("", normalized)
        return re.sub(r"(?<=[a-z])(?=\d)|(?<=\d)(?=[a-z])", "", normalized)

    def _format_programme(self, programme: dict[str, object]) -> str:
        start = programme["start"]
        stop = programme["stop"]
        title = programme["title"]
        assert isinstance(start, datetime)
        assert isinstance(stop, datetime)
        assert isinstance(title, str)
        return f"{start.strftime('%H:%M')}-{stop.strftime('%H:%M')} {title}"
