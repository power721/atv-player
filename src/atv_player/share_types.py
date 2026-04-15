from __future__ import annotations

SHARE_TYPE_NAME_BY_ID: dict[str, str] = {
    "10": "百度",
    "9": "天翼",
    "5": "夸克",
    "7": "UC",
    "0": "阿里",
    "8": "115",
    "3": "123",
    "2": "迅雷",
    "6": "移动",
    "1": "PikPak",
}


def get_share_type_name(share_type: str) -> str:
    return SHARE_TYPE_NAME_BY_ID.get(str(share_type), "")
