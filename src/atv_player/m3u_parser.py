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


@dataclass(slots=True)
class ParsedGroup:
    key: str
    name: str
    channels: list[ParsedChannel] = field(default_factory=list)


@dataclass(slots=True)
class ParsedPlaylist:
    groups: list[ParsedGroup] = field(default_factory=list)
    ungrouped_channels: list[ParsedChannel] = field(default_factory=list)


def parse_m3u(text: str) -> ParsedPlaylist:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    result = ParsedPlaylist()
    groups_by_name: dict[str, ParsedGroup] = {}
    pending_name = ""
    pending_group = ""
    pending_logo = ""
    channel_index = 0
    for line in lines:
        if line.startswith("#EXTINF:"):
            attrs = dict(_ATTR_RE.findall(line))
            pending_group = attrs.get("group-title", "").strip()
            pending_logo = attrs.get("tvg-logo", "").strip()
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
        pending_name = ""
        pending_group = ""
        pending_logo = ""
    return result
