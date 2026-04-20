from atv_player.proxy.adblock import is_ad_segment
from atv_player.proxy.stripper import repair_segment_bytes


def test_repair_segment_bytes_strips_png_preamble_and_returns_ts_sync() -> None:
    png_then_ts = (
        b"\x89PNG\r\n\x1a\n"
        b"\x00\x00\x00\rIHDR"
        + b"\x00" * 32
        + b"IEND\xaeB`\x82"
        + b"junk"
        + (b"\x47" + b"\x00" * 187) * 2
    )

    repaired = repair_segment_bytes(png_then_ts)

    assert repaired.startswith(b"\x47")
    assert len(repaired) == 376


def test_repair_segment_bytes_preserves_plain_ts_payload() -> None:
    plain_ts = (b"\x47" + b"\x01" * 187) * 3

    repaired = repair_segment_bytes(plain_ts)

    assert repaired == plain_ts


def test_repair_segment_bytes_falls_back_to_original_when_no_sync_found() -> None:
    payload = b"\x89PNG\r\n\x1a\nnot-ts"

    repaired = repair_segment_bytes(payload)

    assert repaired == payload


def test_is_ad_segment_uses_duration_and_url_signals_conservatively() -> None:
    assert is_ad_segment(0.5, "https://cdn.example/live/0001.ts") is True
    assert is_ad_segment(5.0, "https://media.example/video/adjump/0002.ts") is True
    assert is_ad_segment(5.0, "https://media.example/path/ad-0003.ts") is True
    assert is_ad_segment(5.0, "https://media.example/path/main-0004.ts") is False
