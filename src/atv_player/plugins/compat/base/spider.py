from __future__ import annotations

import json
import re
import time
from abc import ABCMeta
from hashlib import sha256
from pathlib import Path

import requests
from lxml import etree

_CACHE_ROOT = Path.home() / ".cache" / "atv-player" / "plugins" / "spider-cache"


def set_cache_root(path: Path | str) -> None:
    global _CACHE_ROOT
    _CACHE_ROOT = Path(path)
    _CACHE_ROOT.mkdir(parents=True, exist_ok=True)


def _cache_path(key: str) -> Path:
    _CACHE_ROOT.mkdir(parents=True, exist_ok=True)
    return _CACHE_ROOT / f"{sha256(key.encode('utf-8')).hexdigest()}.cache"


def _buffer_and_close_response(response):
    try:
        if hasattr(response, "content"):
            _ = response.content
    finally:
        close = getattr(response, "close", None)
        if callable(close):
            close()
    return response


class Spider(metaclass=ABCMeta):
    def __init__(self) -> None:
        self.extend = ""

    def init(self, extend: str = "") -> None:
        self.extend = extend

    def homeContent(self, filter):
        return {"class": [], "list": []}

    def categoryContent(self, tid, pg, filter, extend):
        return {"list": [], "page": pg, "pagecount": 1, "total": 0}

    def detailContent(self, ids):
        return {"list": []}

    def searchContent(self, key, quick, pg="1"):
        raise NotImplementedError

    def playerContent(self, flag, id, vipFlags):
        raise NotImplementedError

    def getName(self):
        return ""

    def danmaku(self):
        return False

    def fetch(
        self,
        url,
        params=None,
        cookies=None,
        headers=None,
        timeout=5,
        verify=True,
        stream=False,
        allow_redirects=True,
    ):
        response = requests.get(
            url,
            params=params,
            cookies=cookies,
            headers=headers,
            timeout=timeout,
            verify=verify,
            stream=stream,
            allow_redirects=allow_redirects,
        )
        response.encoding = "utf-8"
        return _buffer_and_close_response(response)

    def post(
        self,
        url,
        params=None,
        data=None,
        json=None,
        cookies=None,
        headers=None,
        timeout=5,
        verify=True,
        stream=False,
        allow_redirects=True,
    ):
        response = requests.post(
            url,
            params=params,
            data=data,
            json=json,
            cookies=cookies,
            headers=headers,
            timeout=timeout,
            verify=verify,
            stream=stream,
            allow_redirects=allow_redirects,
        )
        response.encoding = "utf-8"
        return _buffer_and_close_response(response)

    def regStr(self, reg, src, group=1):
        match = re.search(reg, src)
        return match.group(group) if match else ""

    def removeHtmlTags(self, src):
        return re.sub(re.compile("<.*?>"), "", src)

    def html(self, content):
        return etree.HTML(content)

    def cleanText(self, src):
        return re.sub(
            "[\U0001F600-\U0001F64F\U0001F300-\U0001F5FF\U0001F680-\U0001F6FF\U0001F1E0-\U0001F1FF]",
            "",
            src,
        )

    def log(self, msg):
        if isinstance(msg, (dict, list)):
            print(json.dumps(msg, ensure_ascii=False))
            return
        print(str(msg))

    def getCache(self, key):
        try:
            value = _cache_path(str(key)).read_text(encoding="utf-8")
        except OSError:
            return None
        if len(value) > 0:
            if (value.startswith("{") and value.endswith("}")) or (
                value.startswith("[") and value.endswith("]")
            ):
                value = json.loads(value)
                if isinstance(value, dict):
                    if "expiresAt" not in value or value["expiresAt"] >= int(time.time()):
                        return value
                    self.delCache(key)
                    return None
            return value
        return None

    def setCache(self, key, value):
        if isinstance(value, (int, float)):
            value = str(value)
        elif isinstance(value, (dict, list)):
            value = json.dumps(value, ensure_ascii=False)
        else:
            value = str(value)
        if len(value) == 0:
            return "failed"
        try:
            _cache_path(str(key)).write_text(value, encoding="utf-8")
        except OSError:
            return "failed"
        return "succeed"

    def delCache(self, key):
        path = _cache_path(str(key))
        try:
            path.unlink(missing_ok=True)
        except OSError:
            return "failed"
        return "succeed"
