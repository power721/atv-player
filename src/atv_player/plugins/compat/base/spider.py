from __future__ import annotations

import json
import re
from abc import ABCMeta

import httpx


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
        response = httpx.get(
            url,
            params=params,
            cookies=cookies,
            headers=headers,
            timeout=timeout,
            follow_redirects=allow_redirects,
        )
        response.encoding = "utf-8"
        return response

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
        response = httpx.post(
            url,
            params=params,
            data=data,
            json=json,
            cookies=cookies,
            headers=headers,
            timeout=timeout,
            follow_redirects=allow_redirects,
        )
        response.encoding = "utf-8"
        return response

    def regStr(self, reg, src, group=1):
        match = re.search(reg, src)
        return match.group(group) if match else ""

    def removeHtmlTags(self, src):
        return re.sub(re.compile("<.*?>"), "", src)

    def cleanText(self, src):
        return re.sub(
            "[\\U0001F600-\\U0001F64F\\U0001F300-\\U0001F5FF\\U0001F680-\\U0001F6FF\\U0001F1E0-\\U0001F1FF]",
            "",
            src,
        )

    def log(self, msg):
        if isinstance(msg, (dict, list)):
            print(json.dumps(msg, ensure_ascii=False))
            return
        print(str(msg))
