import pytest

from ytdownloader import formats
from ytdownloader.formats import Mode


def vid(fid, w, h, vcodec, ext="mp4", fps=30, tbr=1000, size=None, **extra):
    return {"format_id": fid, "width": w, "height": h, "vcodec": vcodec, "acodec": "none",
            "ext": ext, "fps": fps, "tbr": tbr, "filesize": size, "protocol": "https",
            "format": f"{fid} - {w}x{h}", **extra}


def aud(fid, acodec, ext, abr, **extra):
    return {"format_id": fid, "vcodec": "none", "acodec": acodec, "ext": ext, "abr": abr,
            "tbr": abr, "protocol": "https", "format": f"{fid} - audio", **extra}


# A trimmed-down version of what YouTube returns for a 4K video.
YOUTUBE = [
    {"format_id": "sb0", "ext": "mhtml", "vcodec": "none", "acodec": "none", "width": 160, "height": 90},
    aud("139", "mp4a.40.5", "m4a", 48),
    aud("140", "mp4a.40.2", "m4a", 129, filesize=5_000_000),
    aud("140-drc", "mp4a.40.2", "m4a", 129, format_note="medium, DRC"),
    aud("251", "opus", "webm", 135),
    vid("160", 256, 144, "avc1.4d400c"),
    vid("134", 640, 360, "avc1.4d401e"),
    vid("243", 640, 360, "vp9", ext="webm"),
    vid("136", 1280, 720, "avc1.64001f", fps=60),
    vid("247", 1280, 720, "vp9", ext="webm", fps=60),
    vid("137", 1920, 1080, "avc1.640028", fps=60, tbr=4000, size=80_000_000),
    vid("248", 1920, 1080, "vp9", ext="webm", fps=60, tbr=2500),
    vid("399", 1920, 1080, "av01.0.09M.08", fps=60, tbr=2000),
    vid("271", 2560, 1440, "vp9", ext="webm", fps=60, tbr=9000),
    vid("400", 2560, 1440, "av01.0.12M.08", fps=60, tbr=6000),
    vid("313", 3840, 2160, "vp9", ext="webm", fps=60, tbr=18000, size=400_000_000),
    vid("401", 3840, 2160, "av01.0.13M.08", fps=60, tbr=12000),
    vid("701", 3840, 2160, "av01.0.13M.10", fps=60, tbr=20000, dynamic_range="HDR10"),
    vid("571", 7680, 4320, "av01.0.17M.08", fps=60, tbr=40000),
]


def test_available_tiers_caps_at_4k_and_skips_storyboards():
    assert formats.available_tiers(YOUTUBE) == [2160, 1440, 1080, 720, 360, 144]


@pytest.mark.parametrize("w,h,tier", [
    (1920, 1080, 1080),
    (1920, 800, 1080),    # cinema scope
    (1080, 1920, 1080),   # vertical / Shorts
    (1440, 1080, 1080),   # 4:3
    (3840, 1600, 2160),
    (3840, 2160, 2160),
    (2560, 1440, 1440),
    (854, 480, 480),
    (256, 144, 144),
])
def test_format_tier(w, h, tier):
    assert formats.format_tier({"width": w, "height": h}) == tier


def test_compatible_prefers_h264_up_to_1080p():
    sel = formats.select(YOUTUBE, 1080, Mode.COMPATIBLE)
    assert sel.video["format_id"] == "137"
    assert sel.audio["format_id"] == "140"
    assert not sel.needs_conversion


def test_compatible_4k_uses_sdr_vp9_and_needs_conversion():
    sel = formats.select(YOUTUBE, 2160, Mode.COMPATIBLE)
    assert sel.video["format_id"] == "313"
    assert sel.needs_conversion
    assert sel.tier == 2160


def test_original_4k_prefers_sdr_av1():
    sel = formats.select(YOUTUBE, 2160, Mode.ORIGINAL)
    assert sel.video["format_id"] == "401"


def test_original_1080_prefers_av1():
    assert formats.select(YOUTUBE, 1080, Mode.ORIGINAL).video["format_id"] == "399"


def test_requested_tier_missing_falls_back_to_next_lower():
    assert formats.select(YOUTUBE, 480, Mode.COMPATIBLE).video["format_id"] == "134"


def test_requested_tier_below_everything_uses_lowest():
    only_hd = [f for f in YOUTUBE if f.get("height", 0) is None or (f.get("height") or 0) >= 720
               or f["vcodec"] == "none"]
    sel = formats.select(only_hd, 144, Mode.COMPATIBLE)
    assert sel.tier == 720


def test_audio_prefers_original_language():
    fmts = [vid("137", 1920, 1080, "avc1"),
            aud("140-dub", "mp4a.40.2", "m4a", 129, language_preference=-1),
            aud("140-orig", "mp4a.40.2", "m4a", 129, language_preference=10)]
    assert formats.select(fmts, 1080, Mode.COMPATIBLE).audio["format_id"] == "140-orig"


def test_drm_formats_are_ignored():
    fmts = [vid("drm", 3840, 2160, "avc1", has_drm=True), vid("ok", 1920, 1080, "avc1"),
            aud("140", "mp4a.40.2", "m4a", 129)]
    assert formats.available_tiers(fmts) == [1080]
    assert formats.select(fmts, 2160, Mode.COMPATIBLE).video["format_id"] == "ok"


def test_combined_only_site():
    fmts = [
        {"format_id": "sd", "width": 640, "height": 360, "vcodec": "avc1", "acodec": "mp4a.40.2", "ext": "mp4"},
        {"format_id": "hd", "width": 1280, "height": 720, "vcodec": "avc1", "acodec": "mp4a.40.2", "ext": "mp4"},
    ]
    sel = formats.select(fmts, 2160, Mode.COMPATIBLE)
    assert sel.video["format_id"] == "hd" and sel.audio is None
    assert sel.as_yt_dlp_format() is sel.video


def test_no_video_raises():
    with pytest.raises(formats.NoSuitableFormat):
        formats.select([aud("140", "mp4a.40.2", "m4a", 129)], 1080, Mode.COMPATIBLE)


def test_merged_format_dict():
    sel = formats.select(YOUTUBE, 1080, Mode.COMPATIBLE)
    merged = sel.as_yt_dlp_format()
    assert merged["format_id"] == "137+140"
    assert merged["ext"] == "mp4"
    assert merged["requested_formats"] == [sel.video, sel.audio]
    assert merged["height"] == 1080 and merged["acodec"] == "mp4a.40.2"
    assert sel.estimated_size == 85_000_000


def test_tier_label():
    assert formats.tier_label(2160) == "2160p (4K)"
    assert formats.tier_label(480) == "480p"


def test_select_audio_prefers_highest_bitrate_original_track():
    assert formats.select_audio(YOUTUBE)["format_id"] == "251"
    dubbed = [aud("251-dub", "opus", "webm", 160, language_preference=-1),
              aud("140-orig", "mp4a.40.2", "m4a", 129, language_preference=10)]
    assert formats.select_audio(dubbed)["format_id"] == "140-orig"


def test_select_audio_falls_back_to_smallest_combined_file():
    fmts = [
        {"format_id": "hd", "width": 1280, "height": 720, "vcodec": "avc1", "acodec": "mp4a", "abr": 128},
        {"format_id": "sd", "width": 640, "height": 360, "vcodec": "avc1", "acodec": "mp4a", "abr": 128},
    ]
    assert formats.select_audio(fmts)["format_id"] == "sd"
    with pytest.raises(formats.NoSuitableFormat):
        formats.select_audio([vid("137", 1920, 1080, "avc1")])
