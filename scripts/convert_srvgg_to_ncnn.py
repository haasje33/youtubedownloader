"""Convert a Real-ESRGAN "SRVGGNetCompact" checkpoint (.pth) to an ncnn model.

    python scripts/convert_srvgg_to_ncnn.py realesr-general-x4v3.pth OUTPUT_DIR/realesr-general-x4v3

Writes OUTPUT_DIR/realesr-general-x4v3.param and .bin, usable with
realesrgan-ncnn-vulkan (`-n realesr-general-x4v3 -s 4`).

Real-ESRGAN only publishes this model as a PyTorch checkpoint. PyTorch is not
needed here: the checkpoint is a zip of a pickle plus raw tensor data, and the
network is a plain stack of 3x3 convolutions with PReLU, so both reading the
weights and writing the ncnn graph are simple.

Network (basicsr SRVGGNetCompact):
    conv(3->F) PReLU, N x [conv(F->F) PReLU], conv(F->3*s*s), PixelShuffle(s)
    + nearest-neighbour upscale of the input (residual)
"""

from __future__ import annotations

import pickle
import re
import struct
import sys
import zipfile
from pathlib import Path

import numpy as np

_DTYPES = {
    "FloatStorage": np.float32,
    "HalfStorage": np.float16,
    "DoubleStorage": np.float64,
}


class _StorageType:
    def __init__(self, name: str):
        self.dtype = _DTYPES[name]


def _rebuild_tensor_v2(storage, offset, size, stride, *_args):
    itemsize = storage.dtype.itemsize
    return np.lib.stride_tricks.as_strided(
        storage[offset:], shape=tuple(size), strides=tuple(s * itemsize for s in stride)
    ).copy()


def load_checkpoint(path: Path) -> dict[str, np.ndarray]:
    """Load a torch.save() zip checkpoint into numpy arrays (no PyTorch needed)."""
    with zipfile.ZipFile(path) as archive:
        pkl_name = next(n for n in archive.namelist() if n.endswith("/data.pkl"))
        prefix = pkl_name[: -len("data.pkl")]

        class Unpickler(pickle.Unpickler):
            def find_class(self, module, name):
                if module == "torch._utils" and name == "_rebuild_tensor_v2":
                    return _rebuild_tensor_v2
                if module == "torch" and name in _DTYPES:
                    return _StorageType(name)
                if module == "collections" and name == "OrderedDict":
                    import collections
                    return collections.OrderedDict
                raise pickle.UnpicklingError(f"unsupported object in checkpoint: {module}.{name}")

            def persistent_load(self, pid):
                _, storage_type, key, _location, _numel = pid
                raw = archive.read(f"{prefix}data/{key}")
                return np.frombuffer(raw, dtype=storage_type.dtype)

        state = Unpickler(archive.open(pkl_name)).load()
    for key in ("params_ema", "params"):
        if isinstance(state, dict) and key in state:
            state = state[key]
            break
    return {k: np.asarray(v, dtype=np.float32) for k, v in state.items()}


def describe(state: dict[str, np.ndarray]) -> tuple[list[int], int]:
    """Indices of the conv layers in `body` and the upscale factor."""
    convs = sorted(int(m.group(1)) for k in state
                   if (m := re.fullmatch(r"body\.(\d+)\.weight", k)) and state[k].ndim == 4)
    last = state[f"body.{convs[-1]}.weight"]
    upscale = int(round((last.shape[0] / 3) ** 0.5))
    if upscale * upscale * 3 != last.shape[0]:
        raise ValueError("Not an SRVGGNetCompact checkpoint")
    return convs, upscale


def convert(pth: Path, out_base: Path) -> None:
    state = load_checkpoint(pth)
    convs, upscale = describe(state)

    layers: list[str] = []
    blobs = 0
    weights = bytearray()

    def add(line: str, new_blobs: int) -> None:
        nonlocal blobs
        layers.append(line)
        blobs += new_blobs

    add("Input data 0 1 data", 1)
    add("Split split0 1 2 data data_a data_b", 2)
    current = "data_a"
    for n, idx in enumerate(convs):
        w, b = state[f"body.{idx}.weight"], state[f"body.{idx}.bias"]
        out_ch, in_ch, kh, kw = w.shape
        name = f"conv{n}"
        add(f"Convolution {name} 1 1 {current} {name} 0={out_ch} 1={kw} 11={kh} 4={kw // 2} 14={kh // 2} "
            f"5=1 6={w.size}", 1)
        weights += struct.pack("<I", 0)  # float32 weight tag
        weights += w.astype("<f4").tobytes()
        weights += b.astype("<f4").tobytes()
        current = name
        slope_key = f"body.{idx + 1}.weight"
        if slope_key in state and state[slope_key].ndim == 1:
            slope = state[slope_key]
            add(f"PReLU prelu{n} 1 1 {current} prelu{n} 0={slope.size}", 1)
            weights += slope.astype("<f4").tobytes()
            current = f"prelu{n}"
    add(f"PixelShuffle shuffle 1 1 {current} shuffled 0={upscale}", 1)
    add(f"Interp upsample 1 1 data_b upsampled 0=1 1={float(upscale)} 2={float(upscale)}", 1)
    add("BinaryOp add 2 1 shuffled upsampled output 0=0", 1)

    out_base.parent.mkdir(parents=True, exist_ok=True)
    param = "7767517\n" + f"{len(layers)} {blobs}\n" + "\n".join(layers) + "\n"
    out_base.with_suffix(".param").write_text(param)
    out_base.with_suffix(".bin").write_bytes(bytes(weights))
    print(f"Wrote {out_base}.param/.bin ({len(convs)} convolutions, x{upscale})")


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    convert(Path(sys.argv[1]), Path(sys.argv[2]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
