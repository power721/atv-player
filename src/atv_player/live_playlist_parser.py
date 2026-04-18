from __future__ import annotations

from atv_player.m3u_parser import ParsedChannel, ParsedGroup, ParsedPlaylist, parse_m3u


def parse_live_playlist(text: str) -> ParsedPlaylist:
    if text.lstrip().startswith("#EXTM3U"):
        return parse_m3u(text)
    return _parse_txt_playlist(text)


def _parse_txt_playlist(text: str) -> ParsedPlaylist:
    result = ParsedPlaylist()
    current_group_name = ""
    groups_by_name: dict[str, ParsedGroup] = {}
    channel_index = 0
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "," not in line:
            continue
        name, value = [part.strip() for part in line.split(",", 1)]
        if not name or not value:
            continue
        if value == "#genre#":
            current_group_name = name
            if current_group_name and current_group_name not in groups_by_name:
                group = ParsedGroup(key=f"group-{len(groups_by_name)}", name=current_group_name)
                groups_by_name[current_group_name] = group
                result.groups.append(group)
            continue
        channel = ParsedChannel(
            key=f"channel-{channel_index}",
            name=name,
            url=value,
        )
        channel_index += 1
        if current_group_name:
            groups_by_name[current_group_name].channels.append(channel)
        else:
            result.ungrouped_channels.append(channel)
    return result
