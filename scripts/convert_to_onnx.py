"""Convert Real-ESRGAN checkpoints (.pth) to ONNX models for the NVIDIA (CUDA) engine.

    python scripts/convert_to_onnx.py CHECKPOINT.pth OUTPUT.onnx [--fp32]

Supports the two Real-ESRGAN architectures:
  * RRDBNet         RealESRGAN_x4plus, RealESRGAN_x2plus (pixel-unshuffle input)
  * SRVGGNetCompact realesr-general-x4v3, realesr-animevideov3

No PyTorch is needed: the checkpoint is read by convert_srvgg_to_ncnn.load_checkpoint
and the graph is written with the `onnx` package.

The model works directly on video frames so the GPU does all the arithmetic:
    input  "frame": uint8  [N, H, W, 3]  (RGB, as decoded by ffmpeg -pix_fmt rgb24)
    output "image": uint8  [N, H*s, W*s, 3]
By default all weights and activations are float16, which uses the tensor cores
of NVIDIA RTX GPUs.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import numpy as np
import onnx
from onnx import TensorProto, helper, numpy_helper

sys.path.insert(0, str(Path(__file__).resolve().parent))
from convert_srvgg_to_ncnn import load_checkpoint  # noqa: E402

OPSET = 17


class GraphBuilder:
    def __init__(self, fp16: bool):
        self.fp16 = fp16
        self.np_dtype = np.float16 if fp16 else np.float32
        self.onnx_dtype = TensorProto.FLOAT16 if fp16 else TensorProto.FLOAT
        self.nodes: list = []
        self.initializers: list = []
        self.count = 0

    def _name(self, prefix: str) -> str:
        self.count += 1
        return f"{prefix}_{self.count}"

    def const(self, value, dtype=None) -> str:
        name = self._name("const")
        arr = np.asarray(value, dtype=dtype or self.np_dtype)
        self.initializers.append(numpy_helper.from_array(arr, name))
        return name

    def op(self, op_type: str, inputs: list[str], **attrs) -> str:
        out = self._name(op_type.lower())
        self.nodes.append(helper.make_node(op_type, inputs, [out], name=out, **attrs))
        return out

    def conv(self, x: str, weight: np.ndarray, bias: np.ndarray) -> str:
        kh, kw = weight.shape[2:]
        return self.op("Conv", [x, self.const(weight), self.const(bias)],
                       kernel_shape=[kh, kw], pads=[kh // 2, kw // 2, kh // 2, kw // 2])

    def lrelu(self, x: str) -> str:
        return self.op("LeakyRelu", [x], alpha=0.2)

    def prelu(self, x: str, slope: np.ndarray) -> str:
        return self.op("PRelu", [x, self.const(slope.reshape(-1, 1, 1))])

    def add(self, a: str, b: str) -> str:
        return self.op("Add", [a, b])

    def scale(self, x: str, factor: float) -> str:
        return self.op("Mul", [x, self.const(factor)])

    def concat(self, xs: list[str]) -> str:
        return self.op("Concat", xs, axis=1)

    def upsample_nearest(self, x: str, factor: int) -> str:
        # Matches torch.nn.functional.interpolate(mode="nearest").
        scales = self.const([1.0, 1.0, float(factor), float(factor)], np.float32)
        return self.op("Resize", [x, "", scales], mode="nearest",
                       coordinate_transformation_mode="asymmetric", nearest_mode="floor")

    def pixel_shuffle(self, x: str, factor: int) -> str:
        return self.op("DepthToSpace", [x], blocksize=factor, mode="CRD")

    def pixel_unshuffle(self, x: str, factor: int, channels: int = 3) -> str:
        # ONNX SpaceToDepth orders channels block-major (DCR); torch.pixel_unshuffle
        # orders them channel-major. Reorder with a constant permutation.
        s2d = self.op("SpaceToDepth", [x], blocksize=factor)
        blocks = factor * factor
        perm = [(t % blocks) * channels + t // blocks for t in range(channels * blocks)]
        return self.op("Gather", [s2d, self.const(perm, np.int64)], axis=1)


def _conv_weights(state: dict, prefix: str) -> tuple[np.ndarray, np.ndarray]:
    return state[f"{prefix}.weight"], state[f"{prefix}.bias"]


def build_rrdbnet(g: GraphBuilder, x: str, state: dict) -> tuple[str, int]:
    num_block = 1 + max(int(m.group(1)) for k in state if (m := re.match(r"body\.(\d+)\.", k)))
    in_ch = state["conv_first.weight"].shape[1]
    unshuffle = {3: 1, 12: 2, 48: 4}[in_ch]
    scale = 4 // unshuffle
    feat_in = g.pixel_unshuffle(x, unshuffle) if unshuffle > 1 else x
    feat = g.conv(feat_in, *_conv_weights(state, "conv_first"))

    def rdb(inp: str, prefix: str) -> str:
        outs = [inp]
        for i in range(1, 5):
            src = g.concat(outs) if len(outs) > 1 else inp
            outs.append(g.lrelu(g.conv(src, *_conv_weights(state, f"{prefix}.conv{i}"))))
        x5 = g.conv(g.concat(outs), *_conv_weights(state, f"{prefix}.conv5"))
        return g.add(g.scale(x5, 0.2), inp)

    body = feat
    for b in range(num_block):
        out = body
        for r in (1, 2, 3):
            out = rdb(out, f"body.{b}.rdb{r}")
        body = g.add(g.scale(out, 0.2), body)
    body = g.conv(body, *_conv_weights(state, "conv_body"))
    feat = g.add(feat, body)
    feat = g.lrelu(g.conv(g.upsample_nearest(feat, 2), *_conv_weights(state, "conv_up1")))
    feat = g.lrelu(g.conv(g.upsample_nearest(feat, 2), *_conv_weights(state, "conv_up2")))
    out = g.conv(g.lrelu(g.conv(feat, *_conv_weights(state, "conv_hr"))),
                 *_conv_weights(state, "conv_last"))
    return out, scale


def build_srvgg(g: GraphBuilder, x: str, state: dict) -> tuple[str, int]:
    convs = sorted(int(m.group(1)) for k in state
                   if (m := re.fullmatch(r"body\.(\d+)\.weight", k)) and state[k].ndim == 4)
    out = x
    for idx in convs:
        out = g.conv(out, *_conv_weights(state, f"body.{idx}"))
        slope = state.get(f"body.{idx + 1}.weight")
        if slope is not None and slope.ndim == 1:
            out = g.prelu(out, slope)
    scale = int(round((state[f"body.{convs[-1]}.weight"].shape[0] / 3) ** 0.5))
    out = g.pixel_shuffle(out, scale)
    return g.add(out, g.upsample_nearest(x, scale)), scale


def build_model(state: dict, fp16: bool = True) -> tuple[onnx.ModelProto, int]:
    g = GraphBuilder(fp16)
    # uint8 NHWC frame -> float NCHW in [0, 1]
    x = g.op("Cast", ["frame"], to=g.onnx_dtype)
    x = g.op("Mul", [x, g.const(1.0 / 255.0)])
    x = g.op("Transpose", [x], perm=[0, 3, 1, 2])
    if "conv_first.weight" in state:
        out, scale = build_rrdbnet(g, x, state)
    elif any(k.startswith("body.") for k in state):
        out, scale = build_srvgg(g, x, state)
    else:
        raise ValueError("Unknown checkpoint architecture")
    # float NCHW -> uint8 NHWC, rounded and clamped
    out = g.op("Mul", [out, g.const(255.0)])
    out = g.op("Clip", [out, g.const(0.0), g.const(255.0)])
    out = g.op("Add", [out, g.const(0.5)])
    out = g.op("Cast", [out], to=TensorProto.UINT8)
    g.nodes.append(helper.make_node("Transpose", [out], ["image"], name="to_nhwc", perm=[0, 2, 3, 1]))

    graph = helper.make_graph(
        g.nodes, "realesrgan",
        [helper.make_tensor_value_info("frame", TensorProto.UINT8, ["N", "H", "W", 3])],
        [helper.make_tensor_value_info("image", TensorProto.UINT8, ["N", "H_out", "W_out", 3])],
        g.initializers,
    )
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", OPSET)],
                              producer_name="youtubedownloader")
    model.ir_version = 8
    model.metadata_props.append(onnx.StringStringEntryProto(key="scale", value=str(scale)))
    onnx.checker.check_model(model, full_check=True)
    return model, scale


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("checkpoint", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--fp32", action="store_true", help="keep float32 (for CPU testing)")
    args = parser.parse_args()
    model, scale = build_model(load_checkpoint(args.checkpoint), fp16=not args.fp32)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    onnx.save(model, str(args.output))
    print(f"Wrote {args.output} (x{scale}, {'fp32' if args.fp32 else 'fp16'}, "
          f"{args.output.stat().st_size / 1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
