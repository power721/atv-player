from __future__ import annotations

import json
import time
from pathlib import Path

from atv_player.danmaku.models import DanmakuSeriesPreference
from atv_player.paths import app_data_dir


def danmaku_series_preference_path() -> Path:
    path = app_data_dir() / "danmaku-series-preferences.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


class DanmakuSeriesPreferenceStore:
    def __init__(self, path: Path | None = None) -> None:
        self._path = Path(path) if path is not None else danmaku_series_preference_path()

    def load(self, series_key: str) -> DanmakuSeriesPreference | None:
        payload = self._read_all()
        raw = payload.get(series_key)
        if not isinstance(raw, dict):
            return None
        return DanmakuSeriesPreference(series_key=series_key, **raw)

    def save(self, preference: DanmakuSeriesPreference) -> DanmakuSeriesPreference:
        payload = self._read_all()
        payload[preference.series_key] = {
            "provider": preference.provider,
            "page_url": preference.page_url,
            "title": preference.title,
            "search_title": preference.search_title,
            "updated_at": preference.updated_at or int(time.time()),
        }
        self._path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        return preference

    def _read_all(self) -> dict[str, dict[str, object]]:
        if not self._path.exists():
            return {}
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}
