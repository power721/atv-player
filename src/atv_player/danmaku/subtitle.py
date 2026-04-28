from __future__ import annotations

from dataclasses import dataclass
from xml.etree import ElementTree


@dataclass(frozen=True, slots=True)
class _SubtitleLine:
    start: float
    end: float
    line_index: int
    content: str


@dataclass(frozen=True, slots=True)
class _SubtitleCue:
    start: float
    end: float
    text: str


def _format_srt_timestamp(value: float) -> str:
    total_milliseconds = max(0, int(round(value * 1000)))
    hours, remainder = divmod(total_milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, milliseconds = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{milliseconds:03d}"


def _format_ass_timestamp(value: float) -> str:
    total_centiseconds = max(0, int(round(value * 100)))
    hours, remainder = divmod(total_centiseconds, 360_000)
    minutes, remainder = divmod(remainder, 6_000)
    seconds, centiseconds = divmod(remainder, 100)
    return f"{hours:d}:{minutes:02d}:{seconds:02d}.{centiseconds:02d}"


def _parse_danmaku_xml(xml_text: str) -> list[tuple[float, str]]:
    if not xml_text.strip():
        return []
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError:
        return []
    records: list[tuple[float, str]] = []
    for node in root.findall(".//d"):
        payload = str(node.attrib.get("p") or "")
        pieces = payload.split(",")
        if not pieces:
            continue
        try:
            time_offset = max(0.0, float(pieces[0]))
        except (TypeError, ValueError):
            continue
        content = "".join(node.itertext()).strip()
        if not content:
            continue
        records.append((time_offset, content))
    records.sort(key=lambda item: item[0])
    return records


def _assign_lines(records: list[tuple[float, str]], line_count: int, duration_seconds: float) -> list[_SubtitleLine]:
    available_at = [0.0] * line_count
    lines: list[_SubtitleLine] = []
    for start, content in records:
        slot = next((index for index, end in enumerate(available_at) if end <= start), None)
        if slot is None:
            continue
        end = start + duration_seconds
        available_at[slot] = end
        lines.append(_SubtitleLine(start=start, end=end, line_index=slot, content=content))
    return lines


def _build_cues(lines: list[_SubtitleLine], line_count: int) -> list[_SubtitleCue]:
    if not lines:
        return []
    time_points = sorted({line.start for line in lines} | {line.end for line in lines})
    cues: list[_SubtitleCue] = []
    for start, end in zip(time_points, time_points[1:], strict=False):
        active = [line for line in lines if line.start <= start < line.end]
        if not active:
            continue
        ordered = [""] * line_count
        for line in active:
            ordered[line.line_index] = line.content
        text = "\n".join(content for content in ordered if content)
        if not text:
            continue
        if cues and cues[-1].text == text and abs(cues[-1].end - start) < 0.001:
            previous = cues[-1]
            cues[-1] = _SubtitleCue(start=previous.start, end=end, text=previous.text)
            continue
        cues.append(_SubtitleCue(start=start, end=end, text=text))
    return cues


def render_danmaku_srt(xml_text: str, line_count: int = 1, duration_seconds: float = 4.0) -> str:
    normalized_line_count = max(1, min(int(line_count), 5))
    normalized_duration = max(1.0, float(duration_seconds))
    records = _parse_danmaku_xml(xml_text)
    lines = _assign_lines(records, normalized_line_count, normalized_duration)
    cues = _build_cues(lines, normalized_line_count)
    if not cues:
        return ""
    blocks: list[str] = []
    for index, cue in enumerate(cues, start=1):
        blocks.extend(
            [
                str(index),
                f"{_format_srt_timestamp(cue.start)} --> {_format_srt_timestamp(cue.end)}",
                cue.text,
                "",
            ]
        )
    return "\n".join(blocks)


def _escape_ass_text(value: str) -> str:
    return value.replace("\\", r"\\").replace("{", r"\{").replace("}", r"\}")


def render_danmaku_ass(xml_text: str, line_count: int = 1, duration_seconds: float = 4.0) -> str:
    normalized_line_count = max(1, min(int(line_count), 5))
    normalized_duration = max(1.0, float(duration_seconds))
    records = _parse_danmaku_xml(xml_text)
    lines = _assign_lines(records, normalized_line_count, normalized_duration)
    cues = _build_cues(lines, normalized_line_count)
    if not cues:
        return ""

    header = "\n".join(
        [
            "[Script Info]",
            "ScriptType: v4.00+",
            "WrapStyle: 2",
            "ScaledBorderAndShadow: yes",
            "PlayResX: 1920",
            "PlayResY: 1080",
            "",
            "[V4+ Styles]",
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
            "Style: Danmaku,sans-serif,32,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,0,0,0,0,100,100,0,0,1,1,0,8,24,24,4,1",
            "",
            "[Events]",
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
        ]
    )

    events: list[str] = []
    for cue in cues:
        text = r"\N".join(_escape_ass_text(part) for part in cue.text.splitlines())
        events.append(
            f"Dialogue: 0,{_format_ass_timestamp(cue.start)},{_format_ass_timestamp(cue.end)},Danmaku,,0,0,0,,{text}"
        )
    return "\n".join([header, *events, ""])
