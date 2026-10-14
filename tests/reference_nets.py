"""Plain numpy versions of the Real-ESRGAN networks (PyTorch semantics), used to
check the ONNX graphs written by scripts/convert_to_onnx.py."""

import re

import numpy as np


def conv(x, w, b):  # x: C,H,W ; w: O,C,kh,kw
    c, h, wd = x.shape
    kh, kw = w.shape[2:]
    xp = np.pad(x, ((0, 0), (kh // 2, kh // 2), (kw // 2, kw // 2)))
    cols = np.stack([xp[:, i:i + h, j:j + wd] for i in range(kh) for j in range(kw)], axis=1)
    out = w.reshape(w.shape[0], c * kh * kw) @ cols.reshape(c * kh * kw, h * wd)
    return out.reshape(w.shape[0], h, wd) + b[:, None, None]


def lrelu(x):
    return np.where(x >= 0, x, 0.2 * x)


def up(x, s):
    return np.repeat(np.repeat(x, s, axis=1), s, axis=2)


def pixel_shuffle(x, s):
    c, h, w = x.shape
    return x.reshape(c // (s * s), s, s, h, w).transpose(0, 3, 1, 4, 2).reshape(c // (s * s), h * s, w * s)


def pixel_unshuffle(x, s):
    c, h, w = x.shape
    return x.reshape(c, h // s, s, w // s, s).transpose(0, 2, 4, 1, 3).reshape(c * s * s, h // s, w // s)


def rrdbnet(state, x):
    p = lambda name: (state[f"{name}.weight"], state[f"{name}.bias"])  # noqa: E731
    in_ch = state["conv_first.weight"].shape[1]
    unshuffle = {3: 1, 12: 2, 48: 4}[in_ch]
    feat = conv(pixel_unshuffle(x, unshuffle) if unshuffle > 1 else x, *p("conv_first"))
    num_block = 1 + max(int(m.group(1)) for k in state if (m := re.match(r"body\.(\d+)\.", k)))

    def rdb(inp, prefix):
        outs = [inp]
        for i in range(1, 5):
            outs.append(lrelu(conv(np.concatenate(outs), *p(f"{prefix}.conv{i}"))))
        return conv(np.concatenate(outs), *p(f"{prefix}.conv5")) * 0.2 + inp

    body = feat
    for b in range(num_block):
        out = body
        for r in (1, 2, 3):
            out = rdb(out, f"body.{b}.rdb{r}")
        body = out * 0.2 + body
    feat = feat + conv(body, *p("conv_body"))
    feat = lrelu(conv(up(feat, 2), *p("conv_up1")))
    feat = lrelu(conv(up(feat, 2), *p("conv_up2")))
    return conv(lrelu(conv(feat, *p("conv_hr"))), *p("conv_last"))


def srvgg(state, x):
    convs = sorted(int(m.group(1)) for k in state
                   if (m := re.fullmatch(r"body\.(\d+)\.weight", k)) and state[k].ndim == 4)
    out = x
    for idx in convs:
        out = conv(out, state[f"body.{idx}.weight"], state[f"body.{idx}.bias"])
        slope = state.get(f"body.{idx + 1}.weight")
        if slope is not None and slope.ndim == 1:
            out = np.where(out >= 0, out, slope[:, None, None] * out)
    s = int(round((out.shape[0] / 3) ** 0.5))
    return pixel_shuffle(out, s) + up(x, s)


def run(state, image_uint8):
    """HWC uint8 -> HWC uint8, like the ONNX model."""
    x = image_uint8.astype(np.float32).transpose(2, 0, 1) / 255.0
    out = rrdbnet(state, x) if "conv_first.weight" in state else srvgg(state, x)
    return (np.clip(out * 255.0, 0, 255) + 0.5).astype(np.uint8).transpose(1, 2, 0)
