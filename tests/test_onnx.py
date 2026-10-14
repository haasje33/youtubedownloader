"""Tests for the ONNX conversion (scripts/convert_to_onnx.py) and the ONNX/CUDA engine.

Tiny random networks with the real architectures stand in for the large models;
the engine runs on the CPU here, exactly as it does on the GPU otherwise.
"""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")
onnx = pytest.importorskip("onnx")
ort = pytest.importorskip("onnxruntime")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import convert_to_onnx  # noqa: E402
import reference_nets  # noqa: E402

from ytdownloader import downloader, formats, onnx_engine, tools, upscale  # noqa: E402
from ytdownloader.upscale import Content  # noqa: E402

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")


def _conv(rng, out_ch, in_ch):
    return rng.normal(0, 0.05, (out_ch, in_ch, 3, 3)).astype(np.float32), \
        rng.normal(0, 0.01, out_ch).astype(np.float32)


def tiny_rrdbnet(scale=4, nf=8, gc=4, seed=0):
    rng = np.random.default_rng(seed)
    in_ch = {4: 3, 2: 12}[scale]
    state = {}

    def put(name, out_ch, in_ch_):
        state[f"{name}.weight"], state[f"{name}.bias"] = _conv(rng, out_ch, in_ch_)

    put("conv_first", nf, in_ch)
    for r in (1, 2, 3):
        for i in range(1, 5):
            put(f"body.0.rdb{r}.conv{i}", gc, nf + (i - 1) * gc)
        put(f"body.0.rdb{r}.conv5", nf, nf + 4 * gc)
    for name in ("conv_body", "conv_up1", "conv_up2", "conv_hr"):
        put(name, nf, nf)
    put("conv_last", 3, nf)
    return state


def tiny_srvgg(scale=4, nf=8, num_conv=2, seed=1):
    rng = np.random.default_rng(seed)
    state, idx = {}, 0
    for in_ch in [3] + [nf] * num_conv:
        state[f"body.{idx}.weight"], state[f"body.{idx}.bias"] = _conv(rng, nf, in_ch)
        state[f"body.{idx + 1}.weight"] = rng.uniform(0.1, 0.3, nf).astype(np.float32)
        idx += 2
    state[f"body.{idx}.weight"], state[f"body.{idx}.bias"] = _conv(rng, 3 * scale * scale, nf)
    return state


def _run(model, image):
    session = ort.InferenceSession(model.SerializeToString(), providers=["CPUExecutionProvider"])
    return session.run(None, {"frame": image[None]})[0][0]


IMAGE = np.random.default_rng(42).integers(0, 256, (10, 14, 3), dtype=np.uint8)


@pytest.mark.parametrize("state,scale", [
    (tiny_rrdbnet(4), 4),
    (tiny_rrdbnet(2), 2),
    (tiny_srvgg(4), 4),
])
def test_onnx_matches_pytorch_semantics(state, scale):
    model, model_scale = convert_to_onnx.build_model(state, fp16=False)
    assert model_scale == scale
    out = _run(model, IMAGE)
    ref = reference_nets.run(state, IMAGE)
    assert out.shape == (10 * scale, 14 * scale, 3) == ref.shape
    assert np.abs(out.astype(int) - ref.astype(int)).max() <= 1


def test_fp16_model_is_valid_and_close():
    state = tiny_rrdbnet(4)
    fp32 = _run(convert_to_onnx.build_model(state, fp16=False)[0], IMAGE)
    model16, _ = convert_to_onnx.build_model(state, fp16=True)  # check_model(full_check) inside
    fp16 = _run(model16, IMAGE)  # ONNX Runtime adds casts where the CPU lacks fp16 kernels
    assert np.abs(fp16.astype(int) - fp32.astype(int)).max() <= 3


@pytest.fixture
def tiny_models(tmp_path, monkeypatch):
    directory = tmp_path / "onnx-models"
    directory.mkdir()
    for name, state in [("realesrgan-x4plus", tiny_rrdbnet(4)), ("realesrgan-x2plus", tiny_rrdbnet(2)),
                        ("realesr-general-x4v3", tiny_srvgg(4)), ("realesr-animevideov3", tiny_srvgg(4, seed=3))]:
        model, _ = convert_to_onnx.build_model(state, fp16=False)
        onnx.save(model, str(directory / f"{name}.onnx"))
    monkeypatch.setenv("YTD_ONNX_MODELS", str(directory))
    monkeypatch.setenv("YTD_UPSCALE_ENGINE", "onnx-cpu")
    onnx_engine.cuda_status.cache_clear()
    tools.discover.cache_clear()
    yield directory
    onnx_engine.cuda_status.cache_clear()


def test_tiled_matches_whole_frame(tiny_models, monkeypatch):
    frame = np.random.default_rng(7).integers(0, 256, (70, 90, 3), dtype=np.uint8)
    for name, scale in (("realesr-general-x4v3", 4), ("realesrgan-x2plus", 2)):
        up = onnx_engine.OnnxUpscaler(tiny_models / f"{name}.onnx", scale, use_cuda=False)
        monkeypatch.setattr(onnx_engine, "TILE_THRESHOLD", 10**9)
        whole = up.upscale(frame)
        monkeypatch.setattr(onnx_engine, "TILE_THRESHOLD", 0)
        monkeypatch.setattr(onnx_engine, "TILE", 32)
        tiled = up.upscale(frame)
        assert whole.shape == tiled.shape == (70 * scale, 90 * scale, 3)
        # The tiny networks see only a few pixels, so the tile overlap hides every seam.
        assert np.array_equal(whole, tiled)


def test_odd_sizes_with_x2_model(tiny_models):
    up = onnx_engine.OnnxUpscaler(tiny_models / "realesrgan-x2plus.onnx", 2, use_cuda=False)
    assert up.upscale(np.zeros((33, 51, 3), np.uint8)).shape == (66, 102, 3)


@pytest.mark.parametrize("w,h,target,content,model,scale", [
    (640, 360, 1080, Content.REALISTIC, "realesrgan-x4plus", 4),
    (1280, 720, 1440, Content.REALISTIC, "realesrgan-x2plus", 2),
    (960, 540, 1080, Content.REALISTIC, "realesrgan-x2plus", 2),
    (1280, 720, 1440, Content.FAST, "realesr-general-x4v3", 4),
    (640, 360, 1080, Content.ANIMATION, "realesr-animevideov3", 4),
])
def test_onnx_plan(w, h, target, content, model, scale):
    p = upscale.plan(w, h, target, content, engine="cuda")
    assert (p.model, p.scale) == (model, scale)


@pytest.mark.skipif(not (FFMPEG and FFPROBE), reason="ffmpeg/ffprobe not installed")
@pytest.mark.parametrize("content,target", [(Content.FAST, 1080), (Content.REALISTIC, 1440)])
def test_onnx_engine_end_to_end(tmp_path, tiny_models, content, target):
    source = tmp_path / "clip.mp4"
    # 1 s of moving video then 1 s of a still frame, with audio.
    subprocess.run([FFMPEG, "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=10:duration=1",
                    "-f", "lavfi", "-i", "color=c=red:size=160x90:rate=10:duration=1",
                    "-f", "lavfi", "-i", "sine=duration=2",
                    "-filter_complex", "[0:v][1:v]concat=n=2:v=1[v]", "-map", "[v]", "-map", "2:a",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(source)], check=True)
    assert upscale.preferred_engine() == "onnx-cpu"
    events = []
    result = downloader.upscale_file(source, tmp_path / "out", target, content, on_progress=events.append)
    info = upscale.probe(result)
    assert (info.width, info.height) == ({1080: 1920, 1440: 2560}[target], target)
    assert info.audio_codec == "aac"
    frames = subprocess.run([FFPROBE, "-v", "error", "-count_frames", "-select_streams", "v:0",
                             "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", str(result)],
                            capture_output=True, text=True, check=True).stdout.strip()
    assert int(frames) == 20
    assert any(e.stage == "upscale" for e in events)
    assert [p.name for p in (tmp_path / "out").iterdir()] == [result.name]


@pytest.mark.skipif(not (FFMPEG and FFPROBE), reason="ffmpeg/ffprobe not installed")
def test_onnx_engine_cancel(tmp_path, tiny_models):
    import threading

    source = tmp_path / "clip.mp4"
    subprocess.run([FFMPEG, "-loglevel", "error", "-y", "-f", "lavfi",
                    "-i", "testsrc2=size=160x90:rate=10:duration=1", "-c:v", "libx264",
                    "-pix_fmt", "yuv420p", str(source)], check=True)
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(downloader.Cancelled):
        downloader.upscale_file(source, tmp_path / "out", 1080, Content.FAST, cancel_event=cancel)
    assert list((tmp_path / "out").iterdir()) == []


def test_encoder_order_per_platform():
    available = frozenset({"hevc_videotoolbox", "hevc_nvenc", "h264_nvenc", "libx264", "libx265",
                           "h264_videotoolbox", "h264_qsv"})
    hevc = [a[1] for a in downloader.encoder_attempts(available, 1080, 30, "hevc")]
    assert hevc[:3] == ["hevc_videotoolbox", "hevc_nvenc", "libx265"]
    h264 = [a[1] for a in downloader.encoder_attempts(available, 1080, 30, "h264")]
    assert h264 == ["h264_nvenc", "h264_qsv", "libx264", "h264_videotoolbox"]


def test_working_encoders_skip_unusable_hardware(monkeypatch):
    monkeypatch.setattr(tools, "ffmpeg_encoders", lambda ffmpeg: frozenset({"h264_nvenc", "libx264"}))
    monkeypatch.setattr(tools, "encoder_works", lambda ffmpeg, args: args[1] != "h264_nvenc")
    monkeypatch.setattr(tools, "discover", lambda: tools.Toolset("ffmpeg", "ffprobe", None))
    assert [a[1] for a in downloader.working_encoder_attempts(1080, 30, "h264")] == ["libx264"]


def test_windows_compatibility_rules():
    assert formats.target_codec("win32") == "h264" and formats.target_codec("darwin") == "hevc"
    assert formats.is_compatible("avc1.640028", "win32")
    assert not formats.is_compatible("hvc1", "win32")  # HEVC needs a paid extension on Windows
    assert formats.is_compatible("hvc1", "darwin")
    assert not formats.is_compatible("vp9", "darwin")
