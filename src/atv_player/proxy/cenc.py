"""红果短剧 CENC/AES-CTR MP4 流式解密代理。

上游返回的 MP4 是整段 CENC 加密（cenc-aes-ctr，全样本加密），密钥由
video_model 的 encrypt_info.spade_a 派生。本模块解析 moov 样本表，
按 Range 请求边拉边解，moov 区域做 fourcc 还原（encv/enca/sinf）。
"""

from __future__ import annotations

import base64
import binascii
import json
import struct
from dataclasses import dataclass, field
from typing import Callable

from Crypto.Cipher import AES

CENC_FRAGMENT_KEY = "hg-cenc"


def derive_content_key(spade_a: str) -> bytes:
    """spade_a → CENC content key（红果官方派生算法）。"""
    raw = base64.b64decode(spade_a + "=" * (-len(spade_a) % 4))
    if len(raw) < 33:
        raise ValueError("spade_a 密钥材料无效")
    v8 = len(raw) - (raw[0] ^ raw[1] ^ raw[2]) + 47
    if v8 <= 0 or 1 + v8 > len(raw):
        v8 = len(raw) - 1
    if v8 < 33:
        raise ValueError("spade_a 长度异常")
    value = bytearray(raw[1 : 1 + v8])
    va, vb = 85, 246
    for index in range(v8):
        previous = va if index & 1 else vb
        if index & 1:
            va = value[index]
        else:
            vb = value[index]
        value[index] = (-21 - index.bit_count() + (previous ^ value[index])) & 0xFF
    try:
        key = binascii.unhexlify(bytes(value[1:33]).decode("ascii"))
    except (ValueError, binascii.Error) as exc:
        raise ValueError("spade_a 密钥材料无效") from exc
    if len(key) != 16:
        raise ValueError("CENC 密钥长度错误")
    return key


def build_cenc_media_fragment(spade_a: str) -> str:
    payload = json.dumps({"spade_a": spade_a}, separators=(",", ":"))
    encoded = base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")
    return f"#{CENC_FRAGMENT_KEY}={encoded}"


def parse_cenc_media_fragment(url: str) -> tuple[str, str] | None:
    """从播放地址里拆出 CENC 参数，返回 (纯净媒体 URL, spade_a)。"""
    marker = f"#{CENC_FRAGMENT_KEY}="
    if marker not in url:
        return None
    clean_url, _separator, encoded = url.partition(marker)
    try:
        payload = json.loads(
            base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode("utf-8")
        )
    except (ValueError, UnicodeDecodeError):
        return None
    spade_a = str(payload.get("spade_a") or "").strip() if isinstance(payload, dict) else ""
    if not spade_a:
        return None
    return clean_url.strip(), spade_a


@dataclass
class _CencSample:
    offset: int
    size: int
    iv: bytes


@dataclass
class CencMediaIndex:
    total_size: int
    prefix: bytes = b""
    prefix_end: int = 0
    samples: list[_CencSample] = field(default_factory=list)


def _find_box(data: memoryview | bytes, fourcc: bytes, start: int) -> tuple[int, int]:
    for index in range(max(4, start), len(data) - 4):
        if data[index : index + 4] != fourcc:
            continue
        size = struct.unpack_from(">I", data, index - 4)[0]
        if size == 1 and index + 12 <= len(data):
            size = struct.unpack_from(">Q", data, index + 4)[0]
        if 8 <= size <= 500_000_000 and index - 4 + size <= len(data):
            return index - 4, size
    return -1, 0


def _box_body(data: memoryview, fourcc: bytes, start: int) -> memoryview | None:
    offset, size = _find_box(data, fourcc, start)
    return data[offset + 8 : offset + size] if offset >= 0 else None


def _parse_track_tables(moov: memoryview, track_offset: int) -> dict | None:
    """解析单个 trak 的样本表；返回样本尺寸/块偏移与 saiz/saio 信息。"""
    if track_offset < 0:
        return None
    stbl_offset, _ = _find_box(moov, b"stbl", track_offset + 8)
    if stbl_offset < 0:
        return None
    stsz = _box_body(moov, b"stsz", stbl_offset)
    stco = _box_body(moov, b"stco", stbl_offset)
    co64 = _box_body(moov, b"co64", stbl_offset)
    stsc = _box_body(moov, b"stsc", stbl_offset)
    saiz = _box_body(moov, b"saiz", stbl_offset)
    saio = _box_body(moov, b"saio", stbl_offset)
    if any(value is None for value in (stsz, stsc, saiz, saio)) or (
        stco is None and co64 is None
    ):
        return None
    default_size = struct.unpack_from(">I", stsz, 4)[0]
    sample_count = struct.unpack_from(">I", stsz, 8)[0]
    sizes = (
        [default_size] * sample_count
        if default_size
        else [
            struct.unpack_from(">I", stsz, 12 + index * 4)[0]
            for index in range(sample_count)
        ]
    )
    chunk_table = stco if stco is not None else co64
    chunk_count = struct.unpack_from(">I", chunk_table, 4)[0]
    chunk_width = 4 if stco is not None else 8
    offsets = [
        int.from_bytes(
            chunk_table[8 + index * chunk_width : 8 + (index + 1) * chunk_width],
            "big",
        )
        for index in range(chunk_count)
    ]
    entry_count = struct.unpack_from(">I", stsc, 4)[0]
    entries = [
        (
            struct.unpack_from(">I", stsc, 8 + index * 12)[0],
            struct.unpack_from(">I", stsc, 12 + index * 12)[0],
        )
        for index in range(entry_count)
    ]
    chunk_samples = [0] * chunk_count
    for index, (first_chunk, samples_per_chunk) in enumerate(entries):
        end = entries[index + 1][0] - 1 if index + 1 < len(entries) else chunk_count
        for chunk in range(first_chunk - 1, min(end, chunk_count)):
            chunk_samples[chunk] = samples_per_chunk
    saiz_flags = int.from_bytes(saiz[1:4], "big")
    saiz_cursor = 12 if saiz_flags & 1 else 4
    if len(saiz) < saiz_cursor + 5:
        return None
    default_aux_size = saiz[saiz_cursor]
    aux_count = struct.unpack_from(">I", saiz, saiz_cursor + 1)[0]
    aux_sizes = (
        [default_aux_size] * aux_count
        if default_aux_size
        else [
            int(saiz[saiz_cursor + 5 + index])
            for index in range(aux_count)
            if saiz_cursor + 5 + index < len(saiz)
        ]
    )
    if len(aux_sizes) != aux_count:
        return None
    saio_flags = int.from_bytes(saio[1:4], "big")
    saio_cursor = 12 if saio_flags & 1 else 4
    offset_width = 8 if saio[0] == 1 else 4
    if len(saio) < saio_cursor + 4 + offset_width:
        return None
    saio_entry_count = int.from_bytes(saio[saio_cursor : saio_cursor + 4], "big")
    if saio_entry_count < 1:
        return None
    aux_offset = int.from_bytes(saio[saio_cursor + 4 : saio_cursor + 4 + offset_width], "big")
    return {
        "sizes": sizes,
        "offsets": offsets,
        "chunk_samples": chunk_samples,
        "aux_sizes": aux_sizes,
        "aux_offset": aux_offset,
    }


def _patch_moov_prefix(data: bytes) -> bytes:
    """还原 ftyp+moov 区域的 fourcc：encv/enca→原格式（frma），sinf→free。"""
    result = bytearray(data)
    replacements: list[tuple[int, bytes]] = []
    position = 0
    while True:
        position = result.find(b"sinf", position)
        if position < 0:
            break
        if position >= 4:
            box_size = struct.unpack_from(">I", result, position - 4)[0]
            box = result[position - 4 : position - 4 + box_size]
            frma = box.find(b"frma")
            original = bytes(box[frma + 4 : frma + 8]) if frma >= 0 else b""
            owner = bytes(result[position - 12 : position - 8]) if position >= 12 else b""
            if owner == b"encv" and len(original) == 4:
                replacements.append((position - 12, original))
            elif owner == b"enca" and len(original) == 4:
                replacements.append((position - 12, original))
        position += 4
    for offset, original in replacements:
        result[offset : offset + 4] = original

    def replace_fourcc(old: bytes, new: bytes) -> None:
        cursor = 0
        while True:
            cursor = result.find(old, cursor)
            if cursor < 0:
                return
            result[cursor : cursor + 4] = new
            cursor += 4

    replace_fourcc(b"encv", b"hvc1")
    replace_fourcc(b"enca", b"mp4a")
    position = 0
    while True:
        position = result.find(b"sinf", position)
        if position < 0:
            break
        if position >= 4:
            size = struct.unpack_from(">I", result, position - 4)[0]
            end = position - 4 + size
            if 8 <= size < 100_000 and end <= len(result):
                result[position : position + 4] = b"free"
                result[position + 4 : end] = b"\x00" * max(0, end - position - 4)
                position = end
                continue
        position += 4
    return bytes(result)


class CencRangeReader:
    """按需解析样本表并对任意 Range 返回解密后的明文字节。"""

    def __init__(
        self,
        url: str,
        headers: dict[str, str],
        spade_a: str,
        *,
        get: Callable[..., object],
        probe_bytes: int = 512 * 1024,
    ) -> None:
        self._url = url
        self._headers = dict(headers)
        self._key = derive_content_key(spade_a)
        self._get = get
        self._probe_bytes = probe_bytes
        self._index: CencMediaIndex | None = None

    def _fetch_range(self, start: int, end: int) -> bytes:
        response = self._get(
            self._url,
            headers={**self._headers, "Range": f"bytes={start}-{end}"},
            timeout=20.0,
            follow_redirects=True,
        )
        status = int(getattr(response, "status_code", 0) or 0)
        if status not in (200, 206):
            raise ValueError(f"CENC 媒体源返回 {status}")
        content = getattr(response, "content", b"")
        if isinstance(content, str):
            content = content.encode("utf-8", errors="replace")
        return bytes(content)

    def _probe_total_size(self) -> int:
        response = self._get(
            self._url,
            headers={**self._headers, "Range": "bytes=0-0"},
            timeout=20.0,
            follow_redirects=True,
        )
        headers_obj = getattr(response, "headers", None)
        if headers_obj is not None:
            try:
                content_range = str(headers_obj.get("Content-Range") or "")
            except Exception:
                content_range = ""
            if "/" in content_range:
                try:
                    return int(content_range.rsplit("/", 1)[1])
                except ValueError:
                    pass
        return len(getattr(response, "content", b"") or b"")

    def ensure_index(self) -> CencMediaIndex:
        if self._index is not None:
            return self._index
        total_size = self._probe_total_size()
        head = self._fetch_range(0, min(self._probe_bytes, total_size - 1))
        moov_start, moov_size = _find_box(memoryview(head), b"moov", 0)
        if moov_start < 0:
            # moov 在文件尾部的情况：尾部探测。
            tail_start = max(0, total_size - self._probe_bytes)
            tail = self._fetch_range(tail_start, total_size - 1)
            moov_start, moov_size = _find_box(memoryview(tail), b"moov", 0)
            if moov_start < 0:
                raise ValueError("CENC 媒体没有可解析的 moov")
            moov_abs_start = tail_start + moov_start
            moov_bytes = self._fetch_range(moov_abs_start, moov_abs_start + moov_size - 1)
            prefix_bytes = self._fetch_range(0, moov_abs_start - 1) if moov_abs_start > 0 else b""
        else:
            if moov_start + moov_size > len(head):
                moov_bytes = self._fetch_range(0, moov_start + moov_size - 1)[moov_start:]
            else:
                moov_bytes = head[moov_start : moov_start + moov_size]
            prefix_bytes = head[:moov_start]
        self._index = self._build_index(moov_bytes, prefix_bytes, total_size)
        return self._index

    def _build_index(self, moov_bytes: bytes, prefix_bytes: bytes, total_size: int) -> CencMediaIndex:
        moov = memoryview(moov_bytes)
        samples: list[_CencSample] = []
        search = 0
        while True:
            track, track_size = _find_box(moov, b"trak", search)
            if track < 0:
                break
            tables = _parse_track_tables(moov, track)
            if tables is not None:
                samples.extend(self._build_track_samples(tables))
            search = track + max(track_size, 8)
        samples.sort(key=lambda sample: sample.offset)
        prefix = _patch_moov_prefix(prefix_bytes + moov_bytes)
        return CencMediaIndex(
            total_size=total_size,
            prefix=prefix,
            prefix_end=len(prefix),
            samples=samples,
        )

    def _build_track_samples(self, tables: dict) -> list[_CencSample]:
        sizes = tables["sizes"]
        offsets = tables["offsets"]
        chunk_samples = tables["chunk_samples"]
        aux_sizes = tables["aux_sizes"]
        aux_offset = tables["aux_offset"]
        # saio 指向文件内（mdat 区）逐样本 IV 表，按需整体拉取。
        aux_total = sum(max(size, 8) for size in aux_sizes)
        aux_bytes = self._fetch_range(aux_offset, aux_offset + aux_total - 1)
        samples: list[_CencSample] = []
        sample_index = 0
        aux_cursor = 0
        for chunk_offset, per_chunk in zip(offsets, chunk_samples):
            current = chunk_offset
            for _ in range(per_chunk):
                if sample_index >= len(sizes) or sample_index >= len(aux_sizes):
                    break
                iv = aux_bytes[aux_cursor : aux_cursor + max(aux_sizes[sample_index], 8)][:16]
                samples.append(
                    _CencSample(
                        offset=current,
                        size=sizes[sample_index],
                        iv=iv.ljust(16, b"\0"),
                    )
                )
                aux_cursor += max(aux_sizes[sample_index], 8)
                current += sizes[sample_index]
                sample_index += 1
        return samples

    def read_range(self, start: int, inclusive_end: int) -> bytes:
        index = self.ensure_index()
        inclusive_end = min(inclusive_end, index.total_size - 1)
        if start < 0 or inclusive_end < start:
            return b""
        output = bytearray()
        cursor = start
        while cursor <= inclusive_end:
            if cursor < index.prefix_end:
                window_end = min(inclusive_end, index.prefix_end - 1)
                output.extend(index.prefix[cursor : window_end + 1])
                cursor = window_end + 1
                continue
            window_end = min(inclusive_end, cursor + 1024 * 1024 - 1, index.total_size - 1)
            chunk = bytearray(self._fetch_range(cursor, window_end))
            for sample in index.samples:
                if sample.offset > window_end:
                    break
                sample_end = sample.offset + sample.size - 1
                if sample_end < cursor:
                    continue
                overlap_start = max(cursor, sample.offset)
                overlap_end = min(window_end, sample_end)
                if overlap_end < overlap_start:
                    continue
                keystream = self._keystream(sample.iv, sample.size)
                local_start = overlap_start - cursor
                local_end = overlap_end - cursor
                key_slice = keystream[
                    overlap_start - sample.offset : overlap_end - sample.offset + 1
                ]
                chunk[local_start : local_end + 1] = bytes(
                    value ^ key
                    for value, key in zip(
                        chunk[local_start : local_end + 1], key_slice
                    )
                )
            output.extend(chunk)
            cursor = window_end + 1
        return bytes(output)

    def _keystream(self, iv: bytes, length: int) -> bytes:
        cipher = AES.new(
            self._key,
            AES.MODE_CTR,
            nonce=b"",
            initial_value=int.from_bytes(iv[:16].ljust(16, b"\0"), "big"),
        )
        return cipher.encrypt(b"\0" * length)
