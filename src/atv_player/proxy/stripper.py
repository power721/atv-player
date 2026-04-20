from __future__ import annotations

PNG_END = b"\x49\x45\x4E\x44\xAE\x42\x60\x82"
TS_SYNC = 0x47
TS_PACKET_SIZE = 188


def repair_segment_bytes(data: bytes) -> bytes:
    stripped = _strip_png_prefix(data)
    sync_index = stripped.find(bytes([TS_SYNC]))
    if sync_index < 0:
        return data
    candidate = stripped[sync_index:]
    if not _looks_like_ts_payload(candidate):
        return data
    aligned = _align_ts_packets(candidate)
    return aligned if aligned else candidate


def _strip_png_prefix(data: bytes) -> bytes:
    png_end_index = data.find(PNG_END)
    if png_end_index < 0:
        return data
    return data[png_end_index + len(PNG_END) :]


def _align_ts_packets(data: bytes) -> bytes:
    if len(data) < TS_PACKET_SIZE:
        return data
    for offset in range(min(TS_PACKET_SIZE, len(data))):
        if data[offset] != TS_SYNC:
            continue
        probe = data[offset : offset + TS_PACKET_SIZE * 2]
        if len(probe) >= TS_PACKET_SIZE * 2 and probe[TS_PACKET_SIZE] == TS_SYNC:
            trimmed = data[offset:]
            usable = len(trimmed) - (len(trimmed) % TS_PACKET_SIZE)
            return trimmed[:usable] if usable else trimmed
    return data


def _looks_like_ts_payload(data: bytes) -> bool:
    if len(data) < TS_PACKET_SIZE:
        return False
    if len(data) < TS_PACKET_SIZE * 2:
        return data[0] == TS_SYNC
    return data[0] == TS_SYNC and data[TS_PACKET_SIZE] == TS_SYNC
