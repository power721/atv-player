from __future__ import annotations

from dataclasses import dataclass, field
import re

_ATTR_RE = re.compile(r'([A-Za-z0-9_-]+)="([^"]*)"')


@dataclass(slots=True)
class ParsedChannel:
    key: str
    name: str
    url: str
    logo_url: str = ""
    headers: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class ParsedGroup:
    key: str
    name: str
    channels: list[ParsedChannel] = field(default_factory=list)


@dataclass(slots=True)
class ParsedPlaylist:
    groups: list[ParsedGroup] = field(default_factory=list)
    ungrouped_channels: list[ParsedChannel] = field(default_factory=list)


def _parse_http_headers(attrs: dict[str, str]) -> dict[str, str]:
    headers: dict[str, str] = {}
    user_agent = attrs.get("http-user-agent", "").strip()
    if user_agent:
        headers["User-Agent"] = user_agent
    raw_header = attrs.get("http-header", "").strip()
    if not raw_header:
        return headers
    for part in raw_header.split("&"):
        chunk = part.strip()
        if not chunk or "=" not in chunk:
            continue
        key, value = chunk.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key or not value:
            continue
        headers[key] = value
    return headers


def parse_m3u(text: str) -> ParsedPlaylist:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    result = ParsedPlaylist()
    groups_by_name: dict[str, ParsedGroup] = {}
    pending_attrs: dict[str, str] = {}
    pending_name = ""
    pending_group = ""
    pending_logo = ""
    channel_index = 0
    for line in lines:
        if line.startswith("#EXTINF:"):
            pending_attrs = dict(_ATTR_RE.findall(line))
            pending_group = pending_attrs.get("group-title", "").strip()
            pending_logo = pending_attrs.get("tvg-logo", "").strip()
            pending_name = line.rsplit(",", 1)[-1].strip()
            continue
        if line.startswith("#"):
            continue
        if not pending_name:
            continue
        channel = ParsedChannel(
            key=f"channel-{channel_index}",
            name=pending_name,
            url=line,
            logo_url=pending_logo,
            headers=_parse_http_headers(pending_attrs),
        )
        channel_index += 1
        if pending_group:
            group = groups_by_name.get(pending_group)
            if group is None:
                group = ParsedGroup(key=f"group-{len(groups_by_name)}", name=pending_group)
                groups_by_name[pending_group] = group
                result.groups.append(group)
            group.channels.append(channel)
        else:
            result.ungrouped_channels.append(channel)
        pending_attrs = {}
        pending_name = ""
        pending_group = ""
        pending_logo = ""
    return result
