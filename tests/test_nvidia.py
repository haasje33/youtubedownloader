"""Tests for the NVIDIA package (download, verify, unpack) and the TensorRT quality gate."""

import hashlib
import http.server
import io
import json
import threading
import zipfile
from functools import partial
from pathlib import Path

import pytest

from ytdownloader import nvidia_pack


@pytest.fixture
def server(tmp_path):
    root = tmp_path / "www"
    root.mkdir()
    handler = partial(http.server.SimpleHTTPRequestHandler, directory=str(root))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield root, f"http://127.0.0.1:{httpd.server_address[1]}/"
    httpd.shutdown()


def make_pack(root: Path, base: str, version: str, part_size: int, files: dict) -> dict:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    archive = buffer.getvalue()
    parts = []
    for i in range(0, len(archive), part_size):
        chunk = archive[i:i + part_size]
        name = f"pack.part{len(parts) + 1}"
        (root / name).write_bytes(chunk)
        parts.append({"url": base + name, "sha256": hashlib.sha256(chunk).hexdigest(), "size": len(chunk)})
    return {"version": version, "sha256": hashlib.sha256(archive).hexdigest(), "size": len(archive), "parts": parts}


@pytest.fixture
def pack_env(tmp_path, monkeypatch):
    install_root = tmp_path / "programdata"
    info_file = tmp_path / "nvidia_pack.json"
    monkeypatch.setenv("YTD_NVIDIA_PACK_ROOT", str(install_root))
    monkeypatch.setenv("YTD_NVIDIA_PACK_INFO", str(info_file))
    monkeypatch.delenv("PROGRAMDATA", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    return install_root, info_file


def test_download_and_install_in_parts(server, pack_env):
    www, base = server
    install_root, info_file = pack_env
    files = {"cudart64_13.dll": b"x" * 5000, "nvinfer_10.dll": bytes(range(256)) * 40}
    info = make_pack(www, base, "v1", 700, files)
    assert len(info["parts"]) > 1
    info_file.write_text(json.dumps(info))
    old = install_root / "v0"
    old.mkdir(parents=True)
    (old / nvidia_pack.MARKER).write_text("v0")

    assert nvidia_pack.needed()
    progress = []
    folder = nvidia_pack.install(lambda done, total: progress.append((done, total)))
    assert folder == install_root / "v1" == nvidia_pack.pack_dir()
    assert not nvidia_pack.needed()
    for name, data in files.items():
        assert (folder / name).read_bytes() == data
    assert progress[-1] == (info["size"], info["size"])
    assert not old.exists()  # older versions are removed


def test_damaged_download_is_rejected(server, pack_env):
    www, base = server
    install_root, info_file = pack_env
    info = make_pack(www, base, "v1", 10_000, {"a.dll": b"abc"})
    info["parts"][0]["sha256"] = "0" * 64
    info_file.write_text(json.dumps(info))
    with pytest.raises(nvidia_pack.PackError, match="damaged"):
        nvidia_pack.install()
    assert nvidia_pack.pack_dir() is None


def test_cancelled_download(server, pack_env):
    www, base = server
    _, info_file = pack_env
    info_file.write_text(json.dumps(make_pack(www, base, "v1", 10_000, {"a.dll": b"abc" * 1000})))
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(nvidia_pack.PackError, match="Cancelled"):
        nvidia_pack.install(cancel_event=cancel)


def test_install_from_parts_downloaded_by_the_installer(tmp_path, pack_env):
    install_root, info_file = pack_env
    www = tmp_path / "downloaded"
    www.mkdir()
    info = make_pack(www, "https://example.invalid/", "v2", 300, {"cudnn64_9.dll": b"y" * 2000})
    info_file.write_text(json.dumps(info))
    parts = sorted(www.iterdir(), key=lambda p: int(p.suffix[5:]))
    folder = nvidia_pack.install_parts(parts)
    assert (folder / "cudnn64_9.dll").read_bytes() == b"y" * 2000
    with pytest.raises(nvidia_pack.PackError):
        nvidia_pack.install_parts(parts[:1])


def test_unsafe_paths_are_refused(tmp_path, pack_env):
    install_root, info_file = pack_env
    www = tmp_path / "downloaded"
    www.mkdir()
    info = make_pack(www, "https://example.invalid/", "v3", 10_000, {"../evil.dll": b"x"})
    info_file.write_text(json.dumps(info))
    with pytest.raises(nvidia_pack.PackError, match="unexpected"):
        nvidia_pack.install_parts(list(www.iterdir()))
    assert not (install_root / "evil.dll").exists()


def test_no_pack_for_builds_without_one(pack_env, monkeypatch):
    monkeypatch.setenv("YTD_NVIDIA_PACK_INFO", "/nonexistent.json")
    assert nvidia_pack.pack_info() is None and not nvidia_pack.needed()


# --- TensorRT quality gate ------------------------------------------------------------

np = pytest.importorskip("numpy")
ort = pytest.importorskip("onnxruntime")
from ytdownloader import onnx_engine  # noqa: E402


class FakeSession:
    def __init__(self, offset):
        self.offset = offset

    def run(self, outputs, feeds):
        frame = feeds["frame"].astype(np.int16)
        return [np.clip(frame + self.offset, 0, 255).astype(np.uint8)]


@pytest.fixture
def gate(tmp_path, monkeypatch):
    monkeypatch.setattr(onnx_engine, "_gate_file", lambda: tmp_path / "tensorrt.json")
    monkeypatch.setattr(onnx_engine, "gpu_name", lambda: "RTX Test")
    created = []

    def install(trt_offset):
        def create(model_path, use_cuda, tensorrt=False):
            created.append("trt" if tensorrt else "cuda")
            return FakeSession(trt_offset if tensorrt else 0)
        monkeypatch.setattr(onnx_engine, "create_session", create)
    return tmp_path / "tensorrt.json", created, install


def test_tensorrt_used_when_identical(gate):
    gate_file, created, install = gate
    install(0)
    session = onnx_engine.tensorrt_session_if_identical(Path("m.onnx"), "m")
    assert isinstance(session, FakeSession) and session.offset == 0
    verdicts = json.loads(gate_file.read_text())
    assert list(verdicts.values())[0]["ok"] is True
    created.clear()
    onnx_engine.tensorrt_session_if_identical(Path("m.onnx"), "m")
    assert created == ["trt"]  # the verdict is remembered: no second comparison


def test_tensorrt_rejected_when_quality_differs(gate):
    gate_file, created, install = gate
    install(8)  # a visible difference (~30 dB)
    assert onnx_engine.tensorrt_session_if_identical(Path("m.onnx"), "m") is None
    verdict = list(json.loads(gate_file.read_text()).values())[0]
    assert verdict["ok"] is False and verdict["psnr"] < onnx_engine.PSNR_GATE
    created.clear()
    assert onnx_engine.tensorrt_session_if_identical(Path("m.onnx"), "m") is None
    assert created == []  # never tried again on this GPU/version


def test_tensorrt_failure_falls_back(gate, monkeypatch):
    gate_file, _, _ = gate

    def broken(model_path, use_cuda, tensorrt=False):
        raise RuntimeError("nvinfer_10.dll not found")
    monkeypatch.setattr(onnx_engine, "create_session", broken)
    assert onnx_engine.tensorrt_session_if_identical(Path("m.onnx"), "m") is None
    assert "nvinfer" in list(json.loads(gate_file.read_text()).values())[0]["error"]


def test_psnr():
    a = np.zeros((4, 4, 3), np.uint8)
    assert onnx_engine.psnr(a, a) == 99.0
    assert 47 < onnx_engine.psnr(a, a + 1) < 49
