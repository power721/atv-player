import base64
import os
import struct

from Crypto.Cipher import AES

from atv_player.proxy.cenc import (
    CENC_FRAGMENT_KEY,
    CencRangeReader,
    build_cenc_media_fragment,
    derive_content_key,
    parse_cenc_media_fragment,
)

# 用真实 spade_a 派生算法生成的参考向量（与 media-vault 插件实现对齐）。
REAL_SPADE_A = "lbwb8GG4KMRVvhvuU6kY4FSmKOh4oCnpUq4D1FOZAsh9gzWHhw=="
REAL_KEY_HEX = "86c22eed79003130e78191b09b7588e5"


class FakeResponse:
    def __init__(self, content: bytes, content_range: str | None = None) -> None:
        self.status_code = 206
        self.content = content
        self.headers = {"Content-Range": content_range} if content_range else {}


class FakeRangeGet:
    """按 Range 从内存中的字节切片，模拟支持 Range 的上游。"""

    def __init__(self, data: bytes) -> None:
        self.data = data

    def __call__(self, url, headers=None, timeout=None, follow_redirects=True):
        range_header = (headers or {}).get("Range", "")
        if range_header == "bytes=0-0":
            return FakeResponse(b"x", f"bytes 0-0/{len(self.data)}")
        start_text, end_text = range_header[len("bytes=") :].split("-")
        start, end = int(start_text), int(end_text)
        return FakeResponse(
            self.data[start : end + 1],
            f"bytes {start}-{min(end, len(self.data) - 1)}/{len(self.data)}",
        )


def _ctr(key: bytes, iv: bytes, length: int) -> bytes:
    cipher = AES.new(
        key,
        AES.MODE_CTR,
        nonce=b"",
        initial_value=int.from_bytes(iv.ljust(16, b"\0"), "big"),
    )
    return cipher.encrypt(b"\0" * length)


def _box(fourcc: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", len(payload) + 8) + fourcc + payload


def _full_box(fourcc: bytes, version: int, flags: int, payload: bytes) -> bytes:
    return _box(fourcc, bytes([version]) + flags.to_bytes(3, "big") + payload)


def _make_spade_for_key(key: bytes) -> str:
    """按 derive_content_key 的变换逆推，构造派生出指定 key 的合法 spade_a。"""
    hexbytes = key.hex().encode()  # 变换后 value[1:33] 必须是 hex ASCII
    after = [0x30] + list(hexbytes)
    value = bytearray(64)
    va, vb = 85, 246
    for index in range(33):
        previous = va if index & 1 else vb
        before = ((after[index] + 21 + index.bit_count()) & 0xFF) ^ previous
        value[index] = before
        if index & 1:
            va = before
        else:
            vb = before
    header = value[0] ^ value[1] ^ 64  # 令 v8 = 63，落在校验区间内
    raw = bytes([header]) + bytes(value[:63]) + bytes(16)
    return base64.b64encode(raw).decode()


class SyntheticCencMedia:
    """最小化整段 CENC MP4：两个 trak（encv/enca）各一个样本。"""

    def __init__(self) -> None:
        # 正向构造：合法 spade_a → 派生 key → 用该 key 加密样本。
        self.key = os.urandom(16)
        self.spade_a = _make_spade_for_key(self.key)
        self.plain_video = bytes((index * 7 + 1) & 0xFF for index in range(97))
        self.plain_audio = bytes((index * 13 + 5) & 0xFF for index in range(37))
        self.iv_video = bytes(range(0x10, 0x18))
        self.iv_audio = bytes(range(0x20, 0x28))
        enc_video = bytes(
            value ^ key_stream
            for value, key_stream in zip(
                self.plain_video, _ctr(self.key, self.iv_video, len(self.plain_video))
            )
        )
        enc_audio = bytes(
            value ^ key_stream
            for value, key_stream in zip(
                self.plain_audio, _ctr(self.key, self.iv_audio, len(self.plain_audio))
            )
        )

        ftyp = _box(b"ftyp", b"isom\x00\x00\x02\x00isom")
        # 布局：ftyp + moov + mdat(enc_video + enc_audio) + free(IV 表)。
        aux = self.iv_video + self.iv_audio

        def build(video_offset: int, audio_offset: int, video_aux: int, audio_aux: int) -> bytes:
            def make_trak(entry_fourcc: bytes, original_fourcc: bytes, sample_offset: int, size: int, aux_offset: int) -> bytes:
                stsz = _full_box(b"stsz", 0, 0, struct.pack(">II", 0, 1) + struct.pack(">I", size))
                stco = _full_box(b"stco", 0, 0, struct.pack(">I", 1) + struct.pack(">I", sample_offset))
                stsc = _full_box(b"stsc", 0, 0, struct.pack(">I", 1) + struct.pack(">III", 1, 1, 1))
                saiz = _full_box(b"saiz", 0, 0, bytes([0]) + struct.pack(">I", 1) + bytes([8]))
                # saio version 0 用 32 位偏移（与 reader 解析、真实文件一致）。
                saio = _full_box(b"saio", 0, 0, struct.pack(">I", 1) + struct.pack(">I", aux_offset))
                sinf = _box(b"sinf", _box(b"frma", original_fourcc))
                sample_entry = _box(entry_fourcc, b"\x00" * 8 + sinf)
                stbl = _box(b"stbl", sample_entry + stsz + stco + stsc + saiz + saio)
                return _box(b"trak", _box(b"tkhd", b"\x00" * 92) + stbl)

            return _box(
                b"moov",
                _box(b"mvhd", b"\x00" * 100)
                + make_trak(b"encv", b"hvc1", video_offset, len(enc_video), video_aux)
                + make_trak(b"enca", b"mp4a", audio_offset, len(enc_audio), audio_aux),
            )

        # 两遍构造：偏移量不改变 box 尺寸，先量 moov 长度再回填真实偏移。
        moov_size = len(build(0, 0, 0, 0))
        mdat_offset = len(ftyp) + moov_size + 8
        self.video_offset = video_offset = mdat_offset
        self.audio_offset = audio_offset = mdat_offset + len(enc_video)
        free_payload_start = audio_offset + len(enc_audio) + 8
        moov = build(
            video_offset,
            audio_offset,
            free_payload_start,              # 视频 IV 表
            free_payload_start + len(self.iv_video),  # 音频 IV 表
        )
        assert len(moov) == moov_size
        mdat = _box(b"mdat", enc_video + enc_audio)
        free = _box(b"free", aux)
        self.media = ftyp + moov + mdat + free
        self.mdat_payload_offset = mdat_offset

    def reader(self) -> CencRangeReader:
        return CencRangeReader(
            "http://fake/cenc.mp4",
            {},
            self.spade_a,
            get=FakeRangeGet(self.media),
        )


def test_derive_content_key_matches_reference_vector() -> None:
    assert derive_content_key(REAL_SPADE_A).hex() == REAL_KEY_HEX


def test_cenc_fragment_round_trip() -> None:
    fragment = build_cenc_media_fragment(REAL_SPADE_A)
    url = f"https://cdn.example/video.mp4?sign=1{fragment}"
    parsed = parse_cenc_media_fragment(url)
    assert parsed is not None
    media_url, spade_a = parsed
    assert media_url == "https://cdn.example/video.mp4?sign=1"
    assert spade_a == REAL_SPADE_A


def test_parse_cenc_fragment_rejects_invalid_payloads() -> None:
    assert parse_cenc_media_fragment("https://cdn.example/video.mp4") is None
    assert parse_cenc_media_fragment(f"https://cdn.example/video.mp4#{CENC_FRAGMENT_KEY}=!!!!") is None
    encoded = base64.urlsafe_b64encode(b'{"spade_a": ""}').decode()
    assert parse_cenc_media_fragment(f"https://cdn.example/v.mp4#{CENC_FRAGMENT_KEY}={encoded}") is None


def test_reader_parses_index_and_restores_fourcc() -> None:
    synthetic = SyntheticCencMedia()
    index = synthetic.reader().ensure_index()
    assert index.total_size == len(synthetic.media)
    assert len(index.samples) == 2
    assert index.samples[0].offset == synthetic.video_offset
    assert index.samples[1].offset == synthetic.audio_offset
    assert b"sinf" not in index.prefix
    assert b"encv" not in index.prefix
    assert b"enca" not in index.prefix
    assert b"hvc1" in index.prefix
    assert b"mp4a" in index.prefix


def test_reader_decrypts_full_and_partial_samples() -> None:
    synthetic = SyntheticCencMedia()
    reader = synthetic.reader()
    payload = reader.read_range(
        synthetic.video_offset,
        synthetic.audio_offset + len(synthetic.plain_audio) - 1,
    )
    video = payload[: len(synthetic.plain_video)]
    audio = payload[len(synthetic.plain_video) :]
    assert video == synthetic.plain_video
    assert audio == synthetic.plain_audio
    # 从视频样本中间开始读 16 字节。
    mid = synthetic.video_offset + 10
    piece = reader.read_range(mid, mid + 15)
    keystream = _ctr(synthetic.key, synthetic.iv_video, len(synthetic.plain_video))
    expect = bytes(
        synthetic.media[mid + offset] ^ keystream[10 + offset] for offset in range(16)
    )
    assert piece == expect


def test_reader_serves_prefix_from_patched_moov() -> None:
    synthetic = SyntheticCencMedia()
    reader = synthetic.reader()
    index = reader.ensure_index()
    assert reader.read_range(0, index.prefix_end - 1) == index.prefix


def test_reader_clamps_range_beyond_eof() -> None:
    synthetic = SyntheticCencMedia()
    reader = synthetic.reader()
    index = reader.ensure_index()
    assert reader.read_range(len(synthetic.media) + 100, len(synthetic.media) + 500) == b""
    tail = reader.read_range(len(synthetic.media) - 8, len(synthetic.media) + 99)
    assert tail == synthetic.media[-8:]
