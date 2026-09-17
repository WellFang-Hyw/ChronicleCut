"""T3/T4：从影视素材里抽帧、定格放大，以及往画面上加剪辑标注元素。

设计上的关键取巧（这是整个「切片当背景」能低成本落地的原因）：

    **定格放大 = 抽一帧 → 裁一块放大 → 当成静态图，交给现有的
    `video.encode_segment` 走原来的静态图路径。**

    所以不需要写任何 zoompan/crop 滤镜表达式，也不碰原来的编码链路，
    而且天然继承了原有的缓移动效（画面上那点轻微推拉）。

  代价是「定格期间画面不再动」（本来定格就是这个意思），
  想让它动就换个手法（`slow_push`）。

标注元素（T4）：画面上打的大字（callout）与人物名条（nametag）。
画字的实现在 `media.py`（`draw_callout` / `draw_nametag`），本模块只做搬运：
抽帧、裁切放大、以及按像素验收位置的 `ink_band`。

放在 media.py 的原因：版面代码只能有一份。切片镜头和静态图镜头如果各自
实现一遍标注排版，迟早会「这一段有大字、那一段没有」或者字号对不上，
而且项目里那套「折行不拆词、描边 + 底条、位置按像素验收」的经验没法共享。
"""
from __future__ import annotations

import logging
from pathlib import Path

from PIL import Image

from . import media
from .config import Config
# 画字（大字/人名条）的实现在 media.py：版面代码只留一份
from .media import CALLOUT_CY, NAMETAG_CY, draw_callout, draw_nametag  # noqa: F401,E402

log = logging.getLogger("hsg.frames")


def grab_frame(clip: Path, at: float, out: Path) -> Path:
    """从素材里抽一帧存成 jpg。

    ⚠️ `-ss` 必须放在 `-i` **之前**（输入侧 seek）。放输出侧会让滤镜拿不到数据，
    抽出来是 0 字节 —— 这个坑在频谱图那轮踩过一次。
    """
    from .video import run_ffmpeg
    out.parent.mkdir(parents=True, exist_ok=True)
    # 素材在别的目录，只能给绝对路径；但用正斜杠，避开 Windows 反斜杠转义
    src = str(clip).replace("\\", "/")
    run_ffmpeg(["-ss", f"{max(0.0, float(at)):.3f}", "-i", src,
                "-frames:v", "1", "-q:v", "2", "-y", out.name],
               cwd=out.parent, desc=f"抽帧 {out.name}")
    if not out.exists() or out.stat().st_size == 0:
        raise RuntimeError(f"抽帧失败（0 字节）：{clip} @ {at}s")
    return out


def zoom_frame(src: Path, out: Path, zoom: float = 1.55,
               focus: tuple[float, float] = (0.5, 0.5)) -> Path:
    """把一帧裁一块再放大 —— 这就是「定格放大」那一下。

    `focus` 是要放大的位置（0-1 的相对坐标），默认画面正中。
    放大倍数会被夹在 1.0–3.0：再大就糊成马赛克了。
    """
    z = min(3.0, max(1.0, float(zoom)))
    with Image.open(src) as im:
        im = im.convert("RGB")
        w, h = im.size
        cw, ch = max(8, int(w / z)), max(8, int(h / z))
        cx = min(max(0.0, focus[0]), 1.0) * w
        cy = min(max(0.0, focus[1]), 1.0) * h
        left = int(min(max(0, cx - cw / 2), w - cw))
        top = int(min(max(0, cy - ch / 2), h - ch))
        crop = im.crop((left, top, left + cw, top + ch))
        # 放大回原尺寸（Lanczos 比默认的重采样干净）
        big = crop.resize((w, h), Image.LANCZOS)
    out.parent.mkdir(parents=True, exist_ok=True)
    big.save(out, "JPEG", quality=94)
    return out


def prepare_freeze(clip: Path, workdir: Path, name: str, cfg: Config, *,
                   at: float = 0.0, zoom: float = 1.55) -> Path:
    """抽帧 + 放大，产出一张能直接当背景的静态图（定格放大手法用）。"""
    raw = grab_frame(clip, at, workdir / f"{name}_frame.jpg")
    return zoom_frame(raw, workdir / f"{name}_zoom.jpg", zoom=zoom)



def overlay_layer(out: Path, size: tuple[int, int], cfg: Config, *,
                  kicker: str = "", title: str = "", caption: str = "",
                  credit: str = "", callout: str = "", nametag: str = "") -> Path:
    """只画前景层（切片镜头用；静态图镜头走 media.build_layers）。

    实现上故意调 `media.build_layers` 传 `image_path=None`：它会把渐变底图也画一遍
    （白花一张图的开销，可忽略），换来的是**版面代码只有一份**，不会两边慢慢跑偏。
    """
    tmp_bg = out.with_name(out.stem + "_bg_unused.jpg")
    _, fg_path = media.build_layers(tmp_bg, out, size, cfg, image_path=None,
                                    kicker=kicker, title=title, caption=caption,
                                    credit=credit, callout=callout, nametag=nametag)
    try:
        tmp_bg.unlink()          # 这张渐变底图没人用
    except OSError:
        pass
    return fg_path


def ink_band(png: Path, y0: float, y1: float, x0: float = 0.0, x1: float = 1.0,
             min_luma: int = 0) -> int:
    """数这个矩形区域里有几个不透明像素 —— 位置类改动靠它按像素验收，不靠眼睛看。

    （为什么要这个工具：上一轮靠视觉模型目测标语位置，结论正好判反了。）

    `x0`/`x1` 给的是**横向范围**（比例）：要验「关键词在左上角」就得同时限定
    纵向条带和左侧范围，只看纵向分不出居中还是靠左。

    `min_luma`：只数**足够亮的像素**（文字是近白色）。默认 0 = 数所有不透明像素，
    但那样会把半透明遮罩（压暗条/渐变）也算进来 —— 踩过：验「没给大字时这里是空的」
    永远不通过，因为遮罩本来就有 alpha。
    """
    with Image.open(png) as im:
        im = im.convert("RGBA")
        w, h = im.size
        box = (int(w * x0), int(h * y0), int(w * x1), int(h * y1))
        crop = im.crop(box)
        alpha = crop.getchannel("A")
        rgb = crop.convert("RGB")
        if min_luma <= 0:
            return sum(1 for v in alpha.getdata() if v > 40)
        return sum(1 for a, px in zip(alpha.getdata(), rgb.getdata())
                   if a > 40 and (px[0] + px[1] + px[2]) / 3 >= min_luma)
