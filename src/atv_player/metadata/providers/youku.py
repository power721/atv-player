from __future__ import annotations

import math
import re
from urllib.parse import parse_qs, urlparse

import httpx

from atv_player.danmaku.utils import strip_episode_suffix
from atv_player.metadata.matching import score_match
from atv_player.metadata.models import MetadataMatch, MetadataQuery, MetadataRecord

# 优酷展示名常带别名注记("加菲猫的幸福生活 第一季(别名:Garfield S01)"),
# 注记里的外文名会作为子串参与标题匹配,把毫不相干的查询("S01")顶过置信线。
_ALIAS_ANNOTATION_RE = re.compile(r"[（(]\s*别名\s*[:：][^（）()]*[）)]")


def _strip_alias_annotation(title: str) -> str:
    return _ALIAS_ANNOTATION_RE.sub("", str(title or "")).strip()


class YoukuMetadataProvider:
    name = "youku"
    _SEARCH_URL = "https://search.youku.com/api/search"
    _CACHE_VERSION = "metadata-v3"
    _SEARCH_USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
    )
    _LIVE_ACTION_CHANNEL_ONLY_GENRES = {"电视剧", "剧集"}

    def __init__(self, get=httpx.get) -> None:
        self._get = get

    def can_enrich(self, _context) -> bool:
        return True

    def search_cache_key(self, candidate: MetadataQuery) -> tuple[str, str]:
        return str(candidate.title or "").strip(), f"{str(candidate.year or '').strip()}#{self._CACHE_VERSION}"

    def detail_cache_key(self, provider_id: str) -> str:
        return f"{str(provider_id or '').strip()}:{self._CACHE_VERSION}"

    def search(self, candidate: MetadataQuery) -> list[MetadataMatch]:
        title = str(candidate.title or "").strip()
        if not title:
            return []
        response = self._get(
            self._SEARCH_URL,
            params={
                "keyword": title,
                "userAgent": self._SEARCH_USER_AGENT,
                "site": 1,
                "categories": 0,
                "ftype": 0,
                "ob": 0,
                "pg": 1,
            },
            headers={
                "user-agent": self._SEARCH_USER_AGENT,
                "accept": "application/json",
                "referer": "https://www.youku.com/",
            },
            follow_redirects=True,
            timeout=10.0,
        )
        payload = response.json()
        matches: list[MetadataMatch] = []
        seen: set[str] = set()
        for item in self._iter_search_items(payload):
            provider_id = str(item.get("provider_id") or "").strip()
            match_title = _strip_alias_annotation(str(item.get("title") or "").strip())
            if not provider_id or not match_title or provider_id in seen:
                continue
            seen.add(provider_id)
            match = MetadataMatch(
                provider=self.name,
                provider_id=provider_id,
                title=match_title,
                year=str(item.get("year") or "").strip(),
                raw=dict(item),
            )
            match.score = score_match(candidate, match)
            matches.append(match)
        return sorted(matches, key=lambda item: item.score, reverse=True)

    def get_detail(self, match: MetadataMatch) -> MetadataRecord:
        payload = dict(match.raw or {})
        page_url = str(match.provider_id or payload.get("provider_id") or "").strip()
        detail_fields: list[dict[str, object]] = []
        update_text = str(payload.get("updateNotice") or payload.get("updateNotification") or "").strip()
        if update_text:
            detail_fields.append({"label": "更新状态", "value": update_text})
        badges = list(payload.get("youku_badges") or [])
        if badges:
            detail_fields.append({"label": "优酷标签", "value": " / ".join(str(item) for item in badges if str(item).strip())})
        if page_url:
            detail_fields.append({"label": "播放链接", "value": page_url})
        detail_fields.extend(self._site_metric_fields(payload))
        return MetadataRecord(
            provider=self.name,
            provider_id=page_url,
            title=_strip_alias_annotation(str(payload.get("title") or match.title or "").strip()),
            year=str(payload.get("year") or match.year or "").strip(),
            poster=str(payload.get("poster") or "").strip(),
            overview=str(payload.get("overview") or "").strip(),
            actors=list(payload.get("actors") or []),
            directors=list(payload.get("directors") or []),
            genres=list(payload.get("genres") or []),
            country=str(payload.get("country") or "").strip(),
            language=str(payload.get("language") or "").strip(),
            detail_fields=detail_fields,
        )

    def _iter_search_items(self, payload: dict) -> list[dict[str, object]]:
        items: list[dict[str, object]] = []
        page_components = payload.get("pageComponentList")
        if isinstance(page_components, list):
            items.extend(self._items_from_page_components(page_components))
        series_list = payload.get("serisesList")
        if isinstance(series_list, list):
            items.extend(self._items_from_series_list(series_list))
        return items

    def _items_from_page_components(self, components: list[object]) -> list[dict[str, object]]:
        results: list[dict[str, object]] = []
        for component in components:
            if not isinstance(component, dict):
                continue
            common = component.get("commonData") if isinstance(component.get("commonData"), dict) else {}
            if not self._is_youku_common_data(common):
                continue
            episodes = self._component_episode_items(component)
            page_url = self._component_primary_url(common) or (str(episodes[0].get("url") or "") if episodes else "")
            title = self._series_title(common, episodes)
            if not title or not page_url:
                continue
            results.append({
                **self._metadata_from_payload(common),
                "title": title,
                "provider_id": page_url,
                "episodes": episodes,
                "category": "优酷",
                "siteScore": common.get("siteScore"),
                "score": common.get("score"),
                "rating": common.get("rating"),
                "heat": common.get("heat"),
                "hot": common.get("hot"),
                "popularity": common.get("popularity"),
                "commentCount": common.get("commentCount"),
                "comments": common.get("comments"),
                "comment": common.get("comment"),
            })
        return results

    def _items_from_series_list(self, series_list: list[object]) -> list[dict[str, object]]:
        results: list[dict[str, object]] = []
        for item in series_list:
            if not isinstance(item, dict):
                continue
            url = self._series_item_url(item)
            title = strip_episode_suffix(str(item.get("title") or item.get("displayName") or "").strip())
            if title and url:
                results.append(
                    {
                        **self._metadata_from_payload(item),
                        "title": title,
                        "provider_id": url,
                        "episodes": [{"title": item.get("title"), "url": url}],
                        "category": "优酷",
                        "siteScore": item.get("siteScore"),
                        "score": item.get("score"),
                        "rating": item.get("rating"),
                        "heat": item.get("heat"),
                        "hot": item.get("hot"),
                        "popularity": item.get("popularity"),
                        "commentCount": item.get("commentCount"),
                        "comments": item.get("comments"),
                        "comment": item.get("comment"),
                    }
                )
        return results

    def _is_youku_common_data(self, common: dict) -> bool:
        if int(common.get("isYouku") or 0) == 1 or int(common.get("hasYouku") or 0) == 1:
            return True
        for candidate in (
            common.get("videoLink"),
            ((common.get("leftButtonDTO") or {}).get("action") or {}).get("value"),
            common.get("action", {}).get("value") if isinstance(common.get("action"), dict) else "",
        ):
            candidate_text = str(candidate or "")
            if "youku.com" in candidate_text or candidate_text.startswith("youku://") or self._normalize_youku_url(candidate_text):
                return True
        return False

    def _component_episode_items(self, component: dict) -> list[dict[str, object]]:
        component_map = component.get("componentMap") if isinstance(component.get("componentMap"), dict) else {}
        episodes = (component_map.get("1035") or {}).get("data") if isinstance(component_map.get("1035"), dict) else []
        if not isinstance(episodes, list):
            return []
        output: list[dict[str, object]] = []
        for episode in episodes:
            if not isinstance(episode, dict):
                continue
            title = str(episode.get("title") or "").strip()
            url = self._component_episode_url(episode)
            if title and url:
                output.append(
                    {
                        "episode_number": self._episode_number(title),
                        "title": title,
                        "url": url,
                        "duration_seconds": self._to_duration_seconds(episode.get("duration")),
                    }
                )
        return output

    def _series_title(self, common: dict, episodes: list[dict[str, object]]) -> str:
        title = _strip_alias_annotation(str((common.get("titleDTO") or {}).get("displayName") or "").strip())
        if title:
            return strip_episode_suffix(title)
        for episode in episodes:
            title = strip_episode_suffix(_strip_alias_annotation(str(episode.get("title") or "").strip()))
            if title:
                return title
        return ""

    def _component_primary_url(self, common: dict) -> str:
        for candidate in (
            common.get("videoLink"),
            ((common.get("leftButtonDTO") or {}).get("action") or {}).get("value"),
            common.get("action", {}).get("value") if isinstance(common.get("action"), dict) else "",
        ):
            url = self._normalize_youku_url(str(candidate or "").strip())
            if url:
                return url
        return ""

    def _component_episode_url(self, episode: dict) -> str:
        video_id = str(episode.get("videoId") or "").strip()
        if video_id:
            return f"https://v.youku.com/v_show/id_{video_id}.html"
        action = episode.get("action") if isinstance(episode.get("action"), dict) else {}
        return self._normalize_youku_url(str(action.get("value") or "").strip())

    def _series_item_url(self, item: dict) -> str:
        video_id = str(item.get("videoId") or "").strip()
        if video_id:
            return f"https://v.youku.com/v_show/id_{video_id}.html"
        action = item.get("action") if isinstance(item.get("action"), dict) else {}
        return self._normalize_youku_url(str(action.get("value") or "").strip())

    def _normalize_youku_url(self, value: str) -> str:
        candidate = str(value or "").strip()
        if not candidate:
            return ""
        if candidate.startswith("//"):
            candidate = f"https:{candidate}"
        if candidate.startswith("https://v.youku.com/v_show/"):
            return candidate
        parsed = urlparse(candidate)
        vid = parse_qs(parsed.query).get("vid", [""])[0].strip()
        if vid:
            return f"https://v.youku.com/v_show/id_{vid}.html"
        path_match = re.search(r"/id_([^/.]+)\.html", parsed.path)
        if path_match is not None and "youku.com" in (parsed.hostname or ""):
            return f"https://v.youku.com/v_show/id_{path_match.group(1)}.html"
        return ""

    def _metadata_from_payload(self, payload: dict[str, object]) -> dict[str, object]:
        genres = self._tokens_from_values(
            payload.get("category"),
            payload.get("type"),
            payload.get("typeDTO"),
            payload.get("typeName"),
            payload.get("cateName"),
            payload.get("tags"),
            payload.get("tag"),
        )
        feature = self._feature_metadata(payload.get("feature"))
        if not genres and feature.get("genre"):
            genres = [str(feature["genre"])]
        genres = self._filter_live_action_channel_only_genres(genres)
        badges = self._tokens_from_values(
            payload.get("cornerMark"),
            payload.get("corner_mark"),
            payload.get("badge"),
            payload.get("badges"),
            payload.get("mark"),
            payload.get("tagText"),
            (payload.get("iconCorner") or {}).get("tagText") if isinstance(payload.get("iconCorner"), dict) else "",
            ((payload.get("posterDTO") or {}).get("iconCorner") or {}).get("tagText")
            if isinstance(payload.get("posterDTO"), dict) and isinstance((payload.get("posterDTO") or {}).get("iconCorner"), dict)
            else "",
        )
        if int(payload.get("isOnly") or 0) == 1 and "独播" not in badges:
            badges.append("独播")
        if int(payload.get("isExclusive") or 0) == 1 and "独家" not in badges:
            badges.append("独家")
        return {
            "year": self._year_value(payload) or str(feature.get("year") or ""),
            "poster": self._image_value(payload),
            "overview": self._first_text(payload, ("summary", "summaryDTO", "desc", "descDTO", "description", "intro", "introduction")),
            "country": self._first_text(payload, ("area", "areaDTO", "region", "regionDTO", "country")) or str(feature.get("country") or ""),
            "language": self._first_text(payload, ("language", "lang")),
            "actors": self._people_values(payload.get("actor") or payload.get("actorDTO") or payload.get("actors") or payload.get("starring") or payload.get("notice")),
            "directors": self._people_values(payload.get("director") or payload.get("directorDTO") or payload.get("directors")),
            "genres": genres,
            "typeName": genres[0] if genres else "",
            "updateNotice": self._first_text(payload, ("updateNotice", "updateNotification", "updateStatus", "stripeBottom")) or str(feature.get("status") or ""),
            "youku_badges": badges,
        }

    def _first_text(self, payload: dict[str, object], keys: tuple[str, ...]) -> str:
        for key in keys:
            value = payload.get(key)
            text = self._text_value(value)
            if text:
                return text
        return ""

    def _text_value(self, value: object) -> str:
        if isinstance(value, dict):
            for key in ("value", "displayName", "title", "name", "text", "label"):
                text = self._text_value(value.get(key))
                if text:
                    return text
            return ""
        if isinstance(value, list):
            return " / ".join(item for item in (self._text_value(item) for item in value) if item)
        return str(value or "").strip()

    def _image_value(self, payload: dict[str, object]) -> str:
        for key in (
            "poster",
            "posterUrl",
            "poster_url",
            "cover",
            "coverUrl",
            "cover_url",
            "img",
            "pic",
            "image",
            "imageUrl",
            "verticalPic",
            "verticalImage",
        ):
            value = str(payload.get(key) or "").strip()
            if value.startswith(("http://", "https://")):
                return value
        for key in ("posterDTO", "imageDTO", "coverDTO", "imgDTO", "picDTO", "screenShotDTO"):
            value = payload.get(key)
            if not isinstance(value, dict):
                continue
            for nested_key in ("url", "src", "value", "vThumbUrl", "hThumbUrl", "thumbUrl"):
                url = str(value.get(nested_key) or "").strip()
                if url.startswith(("http://", "https://")):
                    return url
        return ""

    def _tokens_from_values(self, *values: object) -> list[str]:
        output: list[str] = []
        seen: set[str] = set()
        for value in values:
            for token in re.split(r"[/|、,，]", self._text_value(value)):
                normalized = token.strip()
                if normalized and normalized not in seen:
                    output.append(normalized)
                    seen.add(normalized)
        return output

    def _filter_live_action_channel_only_genres(self, genres: list[str]) -> list[str]:
        if len(genres) == 1 and genres[0] in self._LIVE_ACTION_CHANNEL_ONLY_GENRES:
            return []
        return genres

    def _people_values(self, value: object) -> list[str]:
        text = self._text_value(value)
        text = re.sub(r"^\s*(?:导演|演员|主演)\s*[:：]\s*", "", text)
        if " " in text and not re.search(r"[/|、,，]", text):
            return [item for item in (part.strip() for part in text.split()) if item]
        return self._tokens_from_values(text)

    def _year_value(self, payload: dict[str, object]) -> str:
        for key in ("year", "showYear", "releaseYear", "releaseDate", "publishTime", "pubDate"):
            value = payload.get(key)
            if isinstance(value, int):
                return str(value) if 1000 <= value <= 9999 else ""
            match = re.search(r"((?:19|20)\d{2})", str(value or ""))
            if match is not None:
                return match.group(1)
        return ""

    def _feature_metadata(self, value: object) -> dict[str, str]:
        parts = [part.strip() for part in re.split(r"[·•]", str(value or "")) if part.strip()]
        metadata: dict[str, str] = {}
        if parts:
            year_match = re.search(r"((?:19|20)\d{2})", parts[0])
            if year_match is not None:
                metadata["year"] = year_match.group(1)
        if len(parts) >= 2:
            metadata["genre"] = parts[1]
        if len(parts) >= 3:
            metadata["country"] = parts[2]
        if len(parts) >= 4:
            metadata["status"] = parts[3]
        return metadata

    def _site_metric_fields(self, payload: dict[str, object]) -> list[dict[str, str]]:
        fields: list[dict[str, str]] = []
        for label, keys in (
            ("站内评分", ("siteScore", "score", "rating")),
            ("热度", ("heat", "hot", "popularity")),
            ("评论", ("commentCount", "comments", "comment")),
        ):
            value = self._metric_value(payload, keys)
            if value:
                fields.append({"label": label, "value": value})
        return fields

    def _metric_value(self, payload: dict[str, object], keys: tuple[str, ...]) -> str:
        for key in keys:
            value = payload.get(key)
            if isinstance(value, dict):
                text = str(
                    value.get("value")
                    or value.get("displayName")
                    or value.get("score")
                    or value.get("text")
                    or ""
                ).strip()
            else:
                text = str(value or "").strip()
            if text:
                return text
        return ""

    def _episode_number(self, title: object) -> int:
        match = re.search(r"(?:第\s*)?0*(\d{1,4})\s*(?:集|话|期)?\s*$", str(title or "").strip())
        if match is None:
            return 0
        return int(match.group(1))

    def _to_duration_seconds(self, value: object) -> int:
        try:
            return max(0, int(math.ceil(float(value or 0))))
        except (TypeError, ValueError):
            return 0
