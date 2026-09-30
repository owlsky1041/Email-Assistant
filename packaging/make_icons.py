#!/usr/bin/env python3
"""生成三平台应用图标。

    python packaging/make_icons.py

产出（提交到版本库，构建时直接使用）：

    packaging/icon.png    256×256   Linux / AppImage
    packaging/icon.ico    多尺寸     Windows exe 与 Inno Setup
    packaging/icon.icns   多尺寸     macOS .app

设计：圆角方形渐变底 + 白色信封 + 右下角知识库圆点。
纯代码绘制，不依赖外部素材，也便于日后调整配色。
"""

from __future__ import annotations

import sys
from pathlib import Path

try:
    from PIL import Image, ImageDraw
except ImportError:
    print("需要 Pillow：pip install Pillow", file=sys.stderr)
    raise SystemExit(1)

OUT_DIR = Path(__file__).resolve().parent

#: 底色渐变（深蓝 -> 亮蓝），与托盘图标的蓝保持一致
TOP_COLOR = (32, 84, 168)
BOTTOM_COLOR = (58, 132, 224)
ACCENT = (255, 176, 32)

#: 需要输出的尺寸。Windows 图标最多用到 256，macOS 需要 512/1024。
SIZES = (16, 24, 32, 48, 64, 128, 256, 512, 1024)


def _lerp(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))  # type: ignore[return-value]


def _rounded_mask(size: int, radius_ratio: float = 0.22) -> Image.Image:
    mask = Image.new("L", (size, size), 0)
    draw = ImageDraw.Draw(mask)
    radius = int(size * radius_ratio)
    draw.rounded_rectangle((0, 0, size - 1, size - 1), radius=radius, fill=255)
    return mask


def render(size: int) -> Image.Image:
    """绘制指定尺寸的图标（RGBA）。"""
    # 先在 4 倍尺寸上绘制再缩小，得到平滑边缘（无需外部抗锯齿）
    scale = 4
    s = size * scale
    canvas = Image.new("RGBA", (s, s), (0, 0, 0, 0))

    # --- 背景渐变 ---
    gradient = Image.new("RGB", (1, s))
    for y in range(s):
        gradient.putpixel((0, y), _lerp(TOP_COLOR, BOTTOM_COLOR, y / max(s - 1, 1)))
    gradient = gradient.resize((s, s))
    canvas.paste(gradient, (0, 0), _rounded_mask(s))

    draw = ImageDraw.Draw(canvas)

    # --- 信封主体 ---
    margin = s * 0.20
    top = s * 0.30
    bottom = s * 0.70
    left = margin
    right = s - margin
    body = (left, top, right, bottom)

    shadow = (int(s * 0.012), int(s * 0.018))
    draw.rounded_rectangle(
        (body[0] + shadow[0], body[1] + shadow[1], body[2] + shadow[0], body[3] + shadow[1]),
        radius=s * 0.045,
        fill=(0, 0, 0, 60),
    )
    draw.rounded_rectangle(body, radius=s * 0.045, fill=(255, 255, 255, 255))

    # --- 信封折角（V 形）---
    fold_depth = (bottom - top) * 0.52
    draw.line(
        [(body[0], body[1] + s * 0.02), ((body[0] + body[2]) / 2, body[1] + fold_depth),
         (body[2], body[1] + s * 0.02)],
        fill=TOP_COLOR + (255,),
        width=max(2, int(s * 0.030)),
        joint="curve",
    )

    # --- 右下角强调圆点（代表知识库 / 检索）---
    r = s * 0.115
    cx, cy = s * 0.745, s * 0.735
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(255, 255, 255, 255))
    r2 = r * 0.62
    draw.ellipse((cx - r2, cy - r2, cx + r2, cy + r2), fill=ACCENT + (255,))

    return canvas.resize((size, size), Image.LANCZOS)


def main() -> int:
    master = render(1024)

    png = OUT_DIR / "icon.png"
    render(256).save(png, "PNG")
    print(f"  {png.name}  (256×256)")

    ico = OUT_DIR / "icon.ico"
    master.save(ico, "ICO", sizes=[(s, s) for s in SIZES if s <= 256])
    print(f"  {ico.name}  ({len([s for s in SIZES if s <= 256])} 种尺寸)")

    icns = OUT_DIR / "icon.icns"
    try:
        # Pillow 的 ICNS 写出支持有限，失败时降级为提示
        master.save(icns, "ICNS")
        print(f"  {icns.name}  (macOS)")
    except Exception as exc:  # noqa: BLE001
        print(f"  ! 生成 icon.icns 失败：{exc}")
        print("    macOS 上可用：mkdir icon.iconset && sips -z ... && iconutil -c icns")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
