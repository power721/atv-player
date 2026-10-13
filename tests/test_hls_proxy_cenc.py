import base64
import httpx
import json
import os
import struct
from urllib.parse import urlparse

from Crypto.Cipher import AES

from atv_player.player.m3u8_ad_filter import M3U8AdFilter
from atv_player.proxy.cenc import (
    CENC_FRAGMENT_KEY,
    CencRangeReader,
    build_cenc_media_fragment,
    derive_content_key,
    parse_cenc_audio_fragment,
    parse_cenc_media_fragment,
)
from atv_player.proxy.server import LocalHlsProxyServer

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


class SyntheticFmp4CencMedia:
    """最小化 fMP4 逐片段 CENC(电影流形态):一个明文片段 + 一个加密片段。

    moov 声明 tenc(is_protected=1, 逐样本 IV), sidx 给出片段表;
    加密片段的 traf 带 trun(样本尺寸)+senc(逐样本 IV)。
    """

    def __init__(self, *, iv_size: int = 16, subsample_heads: int = 0) -> None:
        self.key = os.urandom(16)
        self.spade_a = _make_spade_for_key(self.key)
        self.iv_size = iv_size
        self.clear_samples = [
            bytes((index * 5 + 3) & 0xFF for index in range(40)),
            bytes((index * 9 + 7) & 0xFF for index in range(60)),
        ]
        self.enc_samples = [
            bytes((index * 11 + 1) & 0xFF for index in range(37)),
            bytes((index * 3 + 5) & 0xFF for index in range(53)),
            bytes((index * 7 + 2) & 0xFF for index in range(29)),
        ]
        self.ivs = [
            bytes((sample * 13 + byte) & 0xFF for byte in range(iv_size))
            for sample in range(len(self.enc_samples))
        ]
        # subsample_heads>0 时每个加密样本前 N 字节明文,其余密文。
        self.subsample_heads = subsample_heads

        def encrypt(sample: bytes, iv: bytes) -> tuple[bytes, list[tuple[int, int]] | None]:
            head = min(subsample_heads, len(sample))
            if head == 0:
                cipher = _ctr(self.key, iv, len(sample))
                return bytes(
                    value ^ key for value, key in zip(sample, cipher)
                ), None
            stream_pos = 0
            out = bytearray(sample[:head])
            cipher_len = len(sample) - head
            keystream = _ctr(self.key, iv, cipher_len)
            out += bytes(
                value ^ key for value, key in zip(sample[head:], keystream)
            )
            return bytes(out), [(head, cipher_len)]

        encrypted: list[bytes] = []
        subsample_chains: list[list[tuple[int, int]] | None] = []
        for sample, iv in zip(self.enc_samples, self.ivs):
            data, subs = encrypt(sample, iv)
            encrypted.append(data)
            subsample_chains.append(subs)

        tenc = _full_box(
            b"tenc", 0, 0, b"\x00\x00\x01" + bytes([iv_size]) + b"\x11" * 16
        )
        schi = _box(b"schi", tenc)
        schm = _full_box(b"schm", 0, 0, b"cenc" + struct.pack(">I", 1))
        sinf = _box(b"sinf", _box(b"frma", b"mp4a") + schm + schi)
        entry = _box(b"enca", b"\x00" * 28 + sinf)
        stsd = _full_box(b"stsd", 0, 0, struct.pack(">I", 1) + entry)
        stbl = _box(b"stbl", stsd)
        trak = _box(
            b"trak",
            _box(b"tkhd", b"\x00" * 92) + _box(b"mdia", _box(b"minf", stbl)),
        )
        moov = _box(b"moov", _full_box(b"mvhd", 0, 0, b"\x00" * 100) + trak)

        def make_moof(samples: list[bytes], ivs: list[bytes] | None, chains) -> bytes:
            tfhd = _full_box(b"tfhd", 0, 0x020000, struct.pack(">I", 1))
            tfdt = _full_box(b"tfdt", 0, 0, struct.pack(">I", 0))
            mfhd = _full_box(b"mfhd", 0, 0, struct.pack(">I", 1))
            senc = b""
            aux = b""
            if ivs is not None:
                body = struct.pack(">I", len(ivs))
                for iv, chain in zip(ivs, chains):
                    body += iv
                    if chain is not None:
                        body += struct.pack(">H", len(chain)) + b"".join(
                            struct.pack(">HI", clear, cipher)
                            for clear, cipher in chain
                        )
                # 红果电影封装器用 flags=0x2 表示子样本加密(非标准 0x1)。
                senc = _full_box(b"senc", 0, 0x2 if subsample_heads else 0, body)
                # 实流加密片段 traf 在 trun 后还带 saiz/saio 辅助盒
                # (输出端必须等长中和,否则 ExoPlayer 严格解析崩溃)。
                saiz = _full_box(
                    b"saiz", 0, 0,
                    bytes([0]) + struct.pack(">I", len(ivs)) + bytes([8]) * len(ivs),
                )
                saio = _full_box(
                    b"saio", 0, 0, struct.pack(">I", 1) + struct.pack(">I", 0)
                )
                aux = saiz + saio

            def build(data_offset: int) -> bytes:
                # 0x205 = data_offset + first_sample_flags + sizes(实流视频片段形态)。
                trun = _full_box(
                    b"trun", 0, 0x205,
                    struct.pack(">I", len(samples)) + struct.pack(">i", data_offset)
                    + struct.pack(">I", 0)
                    + b"".join(struct.pack(">I", len(s)) for s in samples),
                )
                return _box(b"moof", mfhd + _box(b"traf", tfhd + tfdt + trun + aux + senc))

            # data_offset 相对 moof 盒起点;实流恰好指向紧随其后的 mdat 载荷。
            return build(len(build(0)) + 8)

        frag1_moof = make_moof(self.clear_samples, None, None)
        frag1 = frag1_moof + _box(b"mdat", b"".join(self.clear_samples))
        frag2_moof = make_moof(encrypted, self.ivs, subsample_chains)
        frag2 = frag2_moof + _box(b"mdat", b"".join(encrypted))

        entries = b""
        for size in (len(frag1), len(frag2)):
            entries += struct.pack(">III", size, 1000, 0x90000000)
        sidx = _full_box(
            b"sidx", 0, 0,
            struct.pack(">IIII", 1, 1000, 0, 0) + b"\x00\x00"
            + struct.pack(">H", 2) + entries,
        )
        ftyp = _box(b"ftyp", b"isom\x00\x00\x02\x00isom")
        self.media = ftyp + moov + sidx + frag1 + frag2
        self.sidx_end = len(ftyp) + len(moov) + len(sidx)
        self.frag1_start = self.sidx_end
        self.frag1_mdat_payload = self.frag1_start + len(frag1_moof) + 8
        self.frag2_start = self.frag1_start + len(frag1)
        self.frag2_mdat_payload = self.frag2_start + len(frag2_moof) + 8

    def reader(self) -> CencRangeReader:
        return CencRangeReader(
            "http://fake/fmp4.mp4",
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


def test_reader_handles_moov_larger_than_probe_window() -> None:
    # 归墟第一季(AI剧, moov 824KB)超出 512KB 探测窗：moov 起点在窗口内但
    # 整盒越出窗口，必须补拉整盒而不是误判"moov 在尾部"。
    synthetic = SyntheticCencMedia()
    reader = CencRangeReader(
        "http://fake/cenc.mp4",
        {},
        synthetic.spade_a,
        get=FakeRangeGet(synthetic.media),
        probe_bytes=48,
    )
    index = reader.ensure_index()
    assert len(index.samples) == 2
    assert index.samples[0].offset == synthetic.video_offset
    assert index.samples[1].offset == synthetic.audio_offset
    assert index.prefix_end > 48
    payload = reader.read_range(
        synthetic.video_offset,
        synthetic.audio_offset + len(synthetic.plain_audio) - 1,
    )
    assert payload[: len(synthetic.plain_video)] == synthetic.plain_video
    assert payload[len(synthetic.plain_video) :] == synthetic.plain_audio


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


def test_cenc_fragment_round_trip_with_audio() -> None:
    fragment = build_cenc_media_fragment(
        REAL_SPADE_A,
        "https://cdn.example/audio.m4a",
        "THERESPADE==",
    )
    url = f"https://cdn.example/video.mp4?sign=1{fragment}"
    assert parse_cenc_media_fragment(url) == (
        "https://cdn.example/video.mp4?sign=1",
        REAL_SPADE_A,
    )
    assert parse_cenc_audio_fragment(url) == (
        "https://cdn.example/audio.m4a",
        "THERESPADE==",
    )


def test_parse_cenc_audio_fragment_fallback_and_rejects() -> None:
    # 片段不带音频字段(短剧音视频合一)。
    url = f"https://cdn.example/video.mp4{build_cenc_media_fragment(REAL_SPADE_A)}"
    assert parse_cenc_audio_fragment(url) is None
    assert parse_cenc_audio_fragment("https://cdn.example/video.mp4") is None
    # 音频未单独给密钥时回退视频的 spade_a。
    encoded = base64.urlsafe_b64encode(
        json.dumps({"spade_a": REAL_SPADE_A, "audio": "https://cdn.example/a.m4a"}).encode()
    ).decode()
    parsed = parse_cenc_audio_fragment(
        f"https://cdn.example/v.mp4#{CENC_FRAGMENT_KEY}={encoded}"
    )
    assert parsed == ("https://cdn.example/a.m4a", REAL_SPADE_A)


def test_create_cenc_media_url_attaches_audio_session() -> None:
    synthetic = SyntheticCencMedia()
    server = LocalHlsProxyServer(get=FakeRangeGet(synthetic.media))
    server.start()

    video_url = server.create_cenc_media_url(
        "http://fake/cenc.mp4",
        {"User-Agent": "t"},
        spade_a=synthetic.spade_a,
        audio_url="http://fake/audio.m4a",
        audio_spade_a=synthetic.spade_a,
    )
    audio_url = server.cenc_external_audio_url(video_url)
    assert audio_url.startswith(f"http://{server.host}:{server.port}/cenc/")
    assert audio_url.endswith(".m4a")
    # 非本代理地址/空地址返回空串。
    assert server.cenc_external_audio_url("https://elsewhere.example/x.mp4") == ""
    assert server.cenc_external_audio_url("") == ""

    # 音频会话按 .m4a 路由经真实 HTTP 服务解密内容。
    response = httpx.get(audio_url, timeout=10)
    assert response.status_code == 200
    reader = synthetic.reader()
    index = reader.ensure_index()
    assert response.content == reader.read_range(0, index.total_size - 1)


def test_create_cenc_media_url_without_audio_leaves_getter_empty() -> None:
    synthetic = SyntheticCencMedia()
    server = LocalHlsProxyServer(get=FakeRangeGet(synthetic.media))
    video_url = server.create_cenc_media_url(
        "http://fake/cenc.mp4", {}, spade_a=synthetic.spade_a
    )
    assert server.cenc_external_audio_url(video_url) == ""


def test_ad_filter_prepare_routes_cenc_audio_to_external_track() -> None:
    synthetic = SyntheticCencMedia()
    upstream = FakeRangeGet(synthetic.media)
    server = LocalHlsProxyServer(get=upstream)
    server.start()
    ad_filter = M3U8AdFilter(proxy_server=server, get=upstream)

    url = "http://fake/cenc.mp4" + build_cenc_media_fragment(
        synthetic.spade_a, "http://fake/audio.m4a"
    )
    prepared = ad_filter.prepare(url, {"User-Agent": "t"})
    assert prepared.startswith(f"http://{server.host}:{server.port}/cenc/")
    audio_url = ad_filter.cenc_external_audio_url(prepared)
    assert audio_url.endswith(".m4a")
    assert httpx.get(audio_url, timeout=10).status_code == 200

    # 无音频字段的片段(短剧)getter 返回空串,不外挂。
    plain_url = "http://fake/cenc.mp4" + build_cenc_media_fragment(synthetic.spade_a)
    plain_prepared = ad_filter.prepare(plain_url, {"User-Agent": "t"})
    assert ad_filter.cenc_external_audio_url(plain_prepared) == ""


def test_fmp4_reader_index_and_encrypted_fragment_decryption() -> None:
    synthetic = SyntheticFmp4CencMedia()
    reader = synthetic.reader()
    index = reader.ensure_index()
    assert len(index.samples) == 0
    assert len(index.fragments) == 2
    assert index.fragments[0][0] == synthetic.frag1_start
    assert index.fragments[1][0] == synthetic.frag2_start

    whole = reader.read_range(0, index.total_size - 1)
    # 明文片段原样透传。
    clear_region = b"".join(synthetic.clear_samples)
    assert whole[
        synthetic.frag1_mdat_payload : synthetic.frag1_mdat_payload + len(clear_region)
    ] == clear_region
    # 加密片段解出明文。
    plain = b"".join(synthetic.enc_samples)
    assert whole[
        synthetic.frag2_mdat_payload : synthetic.frag2_mdat_payload + len(plain)
    ] == plain


def test_fmp4_reader_partial_windows_match_full_read() -> None:
    synthetic = SyntheticFmp4CencMedia(subsample_heads=16)
    reader = synthetic.reader()
    index = reader.ensure_index()
    whole = reader.read_range(0, index.total_size - 1)
    # 从密文区中间开始(子样本部分重叠)、窗口只盖住片段尾部:
    # 必须与整读逐字节一致,且返回长度恒等于请求长度(防密钥流截短缩块)。
    for start, end in [
        (synthetic.frag2_mdat_payload + 30, synthetic.frag2_mdat_payload + 80),
        (synthetic.frag2_mdat_payload + 21, index.total_size - 1),
        (synthetic.frag2_start, synthetic.frag2_mdat_payload + 40),
    ]:
        piece = reader.read_range(start, end)
        assert len(piece) == end - start + 1
        assert piece == whole[start : end + 1]
    # 跨多个样本的任意窗口同样守恒。
    piece = reader.read_range(
        synthetic.frag2_mdat_payload + 5, synthetic.frag2_mdat_payload + 100
    )
    assert len(piece) == 96
    assert piece == whole[synthetic.frag2_mdat_payload + 5 : synthetic.frag2_mdat_payload + 101]


def test_fmp4_reader_subsample_encryption_only_cipher_ranges() -> None:
    synthetic = SyntheticFmp4CencMedia(subsample_heads=16)
    reader = synthetic.reader()
    index = reader.ensure_index()
    whole = reader.read_range(0, index.total_size - 1)
    plain = b"".join(synthetic.enc_samples)
    region = whole[
        synthetic.frag2_mdat_payload : synthetic.frag2_mdat_payload + len(plain)
    ]
    assert region == plain
    cursor = 0
    for sample in synthetic.enc_samples:
        assert region[cursor : cursor + 16] == sample[:16]
        cursor += len(sample)


def test_fmp4_serve_neutralizes_cenc_aux_boxes() -> None:
    # 明文输出(moov 已剥 tenc)残留 saiz/saio/senc 时,ExoPlayer 会在
    # FragmentedMp4Extractor.parseTraf 的 encryptionBox 非空断言上崩溃;
    # 输出必须把它们等长改写为 free,且除类型字段外逐字节不变。
    synthetic = SyntheticFmp4CencMedia(subsample_heads=16)
    reader = synthetic.reader()
    index = reader.ensure_index()
    whole = reader.read_range(0, index.total_size - 1)

    moof_size = struct.unpack_from(">I", whole, synthetic.frag2_start)[0]
    served = whole[synthetic.frag2_start : synthetic.frag2_start + moof_size]
    original = synthetic.media[
        synthetic.frag2_start : synthetic.frag2_start + moof_size
    ]
    diffs = [i for i in range(moof_size) if served[i] != original[i]]
    groups: list[list[int]] = []
    for position in diffs:
        if groups and position - groups[-1][-1] <= 1:
            groups[-1].append(position)
        else:
            groups.append([position])
    assert [original[g[0] : g[0] + 4] for g in groups] == [b"saiz", b"saio", b"senc"]
    assert [served[g[0] : g[0] + 4] for g in groups] == [b"free"] * 3
    assert b"senc" not in served and b"saiz" not in served and b"saio" not in served

    # 试看片段(无加密辅助盒)原样透传。
    frag1_size = struct.unpack_from(">I", whole, synthetic.frag1_start)[0]
    frag1_moof = whole[synthetic.frag1_start : synthetic.frag1_start + frag1_size]
    assert frag1_moof == synthetic.media[
        synthetic.frag1_start : synthetic.frag1_start + frag1_size
    ]


def test_fmp4_reader_without_tenc_stays_passthrough() -> None:
    # tenc 未声明加密(is_protected=0)时不进 fMP4 解密,整文件透传。
    synthetic = SyntheticFmp4CencMedia()
    media = bytearray(synthetic.media)
    marker = media.find(b"\x00\x00\x01\x10" + b"\x11" * 16)
    assert marker >= 4
    media[marker + 2] = 0  # is_protected 1 -> 0
    passthrough = bytes(media)
    reader = CencRangeReader(
        "http://fake/fmp4.mp4", {}, synthetic.spade_a, get=FakeRangeGet(passthrough)
    )
    index = reader.ensure_index()
    assert index.fragments == []
    body = reader.read_range(synthetic.frag2_mdat_payload, len(passthrough) - 1)
    assert body.startswith(passthrough[synthetic.frag2_mdat_payload :])


class TruncatingRangeGet:
    """模拟上游短响应:每次响应都比请求短 10 字节,逼出补拉路径。"""

    def __init__(self, data: bytes) -> None:
        self.data = data

    def __call__(self, url, headers=None, timeout=None, follow_redirects=True):
        range_header = (headers or {}).get("Range", "")
        if range_header == "bytes=0-0":
            return FakeResponse(b"x", f"bytes 0-0/{len(self.data)}")
        start_text, end_text = range_header[len("bytes=") :].split("-")
        start, end = int(start_text), int(end_text)
        span = end - start + 1
        keep = span if span <= 64 else span - 10
        return FakeResponse(
            self.data[start : start + keep],
            f"bytes {start}-{start + keep - 1}/{len(self.data)}",
        )


def test_reader_fills_short_upstream_responses() -> None:
    synthetic = SyntheticCencMedia()
    upstream = TruncatingRangeGet(synthetic.media)
    reader = CencRangeReader(
        "http://fake/cenc.mp4", {}, synthetic.spade_a, get=upstream
    )
    index = reader.ensure_index()
    whole = reader.read_range(0, index.total_size - 1)
    reference = synthetic.reader()
    assert whole == reference.read_range(0, index.total_size - 1)

