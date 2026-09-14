"""画面合成（Pillow 离线合成，不用 ffmpeg 滤镜链堆版面）。

分两层出图，视频阶段再做运动：
  · 背景层 bg.png —— 配图按 motion_scale 放大后铺满，微微压暗（这层在 ffmpeg 里缓移）
  · 前景层 fg.png —— 透明 RGBA：上下压暗渐变 + 标题/章节/图注（这层不动）
这样配图在缓慢横移时，文字是纹丝不动的，比整帧缩放耐看得多。

版面分区（避免标题和字幕打架）：
   0.045h  小字：栏目名 · 章节号
   0.085h  章节标题（仅每章第一屏 + 片头）
   0.72h   图注（小字）
   0.78h+  字幕区：留给 ASS 烧上去的字幕，这里不画东西
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from .config import Config
from .textfmt import fix_line_punct

log = logging.getLogger("hsg.media")

_FONT_BOLD = Path("C:/Windows/Fonts/msyhbd.ttc")
_FONT_REG = Path("C:/Windows/Fonts/msyh.ttc")
_FONT_HEI = Path("C:/Windows/Fonts/simhei.ttf")


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    for cand in ((_FONT_BOLD, _FONT_HEI) if bold else (_FONT_REG, _FONT_HEI)):
        if cand.exists():
            try:
                return ImageFont.truetype(str(cand), size)
            except OSError:
                continue
    return ImageFont.load_default(size)


def _even(n: float) -> int:
    v = int(round(n))
    return v - (v % 2)


def _cover_crop(im: Image.Image, size: tuple[int, int]) -> Image.Image:
    tw, th = size
    sw, sh = im.size
    scale = max(tw / sw, th / sh)
    nw, nh = max(1, int(sw * scale)), max(1, int(sh * scale))
    im = im.resize((nw, nh), Image.LANCZOS)
    left, top = (nw - tw) // 2, (nh - th) // 2
    return im.crop((left, top, left + tw, top + th))


def _gradient(size: tuple[int, int], base: tuple[int, int, int]) -> Image.Image:
    tw, th = size
    light = tuple(min(255, int(c * 1.9) + 24) for c in base)
    small = Image.new("RGB", (2, 2))
    small.putpixel((0, 0), light)
    small.putpixel((1, 0), base)
    small.putpixel((0, 1), base)
    small.putpixel((1, 1), tuple(max(0, int(c * 0.6)) for c in base))
    return small.resize((tw, th), Image.BICUBIC)


def _wrap_cjk(text: str, font: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    lines: list[str] = []
    for para in (text or "").split("\n"):
        cur = ""
        for ch in para:
            trial = cur + ch
            if font.getlength(trial) <= max_width or not cur:
                cur = trial
            else:
                lines.append(cur)
                cur = ch
        lines.append(cur)
    # 别让标点落在行首（折行硬伤）
    return [ln for ln in fix_line_punct([ln for ln in lines if ln.strip()])] or [""]


def _draw_block(draw: ImageDraw.ImageDraw, lines: list[str], font, cx: int, top: int,
                *, fill=(255, 255, 255), outline=(0, 0, 0), outline_w: int = 3,
                line_gap: int = 10) -> int:
    y = top
    ascent, descent = font.getmetrics()
    for ln in lines:
        w = font.getlength(ln)
        if outline_w > 0:
            draw.text((cx - w / 2, y), ln, font=font, fill=fill,
                      stroke_width=outline_w, stroke_fill=outline)
        else:
            draw.text((cx - w / 2, y), ln, font=font, fill=fill)
        y += ascent + descent + line_gap
    return y


def _vertical_scrim(size: tuple[int, int], y0: float, y1: float,
                    a0: int, a1: int) -> Image.Image:
    """竖向透明渐变遮罩（用于上下压暗，提升文字可读性）。"""
    tw, th = size
    ya, yb = int(th * y0), int(th * y1)
    h = max(1, yb - ya)
    strip = Image.new("RGBA", (1, h))
    px = strip.load()
    for i in range(h):
        a = int(a0 + (a1 - a0) * (i / max(1, h - 1)))
        px[0, i] = (0, 0, 0, max(0, min(255, a)))
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    layer.paste(strip.resize((tw, h), Image.BILINEAR), (0, ya))
    return layer


def build_layers(
    bg_out: Path,
    fg_out: Path,
    size: tuple[int, int],
    cfg: Config,
    *,
    image_path: Path | None = None,
    kicker: str = "",
    title: str = "",
    caption: str = "",
    credit: str = "",
    darken: float = 0.34,
) -> tuple[Path, Path]:
    """合成一个分镜的背景层与前景层。"""
    tw, th = size
    ms = float(cfg.video.get("motion_scale", 1.16)) if bool(cfg.video.get("motion", True)) else 1.0
    bw, bh = _even(tw * ms), _even(th * ms)

    # ---------------- 背景层
    bg_hex = str(cfg.video.get("fallback_bg", "0x101820")).replace("0x", "").replace("#", "")
    try:
        base_rgb = tuple(int(bg_hex[i : i + 2], 16) for i in (0, 2, 4))
    except (ValueError, IndexError):
        base_rgb = (16, 24, 32)

    if image_path and Path(image_path).exists():
        try:
            with Image.open(image_path) as src:
                bg = _cover_crop(src.convert("RGB"), (bw, bh))
        except Exception as exc:  # noqa: BLE001
            log.warning("配图处理失败(%s)，用渐变底图：%s", Path(image_path).name, exc)
            bg = _gradient((bw, bh), base_rgb)
    else:
        bg = _gradient((bw, bh), base_rgb)

    if darken > 0:
        bg = Image.blend(bg, Image.new("RGB", (bw, bh), (0, 0, 0)), darken)
    # 轻微模糊：历史影像略柔一些更耐看，也让文字更清楚
    bg = bg.filter(ImageFilter.GaussianBlur(radius=0.6))
    bg_out.parent.mkdir(parents=True, exist_ok=True)
    bg.save(bg_out, "JPEG", quality=92)

    # ---------------- 前景层
    fg = Image.new("RGBA", (tw, th), (0, 0, 0, 0))
    fg.alpha_composite(_vertical_scrim((tw, th), 0.0, 0.34, 165, 0))
    fg.alpha_composite(_vertical_scrim((tw, th), 0.60, 1.0, 0, 150))
    draw = ImageDraw.Draw(fg)
    margin_x = int(tw * 0.07)

    # ---- 顶部小字（栏目名 · 章节）
    if kicker:
        f = _font(int(min(tw * 0.030, th * 0.020)), bold=True)
        lines = _wrap_cjk(kicker, f, tw - 2 * margin_x)[:1]
        _draw_block(draw, lines, f, tw // 2, int(th * 0.045),
                    fill=(255, 219, 120), outline_w=2, line_gap=6)

    # ---- 章节标题：字号自适应，最多 2 行，不侵入字幕区
    if title:
        avail_top = int(th * 0.085)
        avail_bottom = int(th * 0.255)
        avail_h = max(60, avail_bottom - avail_top)
        base = int(min(tw * 0.072, th * 0.048))
        f = _font(base, bold=True)
        lines = _wrap_cjk(title, f, tw - 2 * margin_x)
        while (len(lines) > 2 or (f.getmetrics()[0] + f.getmetrics()[1] + 12) * len(lines) > avail_h) \
                and base > 26:
            base = int(base * 0.9)
            f = _font(base, bold=True)
            lines = _wrap_cjk(title, f, tw - 2 * margin_x)
        if len(lines) > 2:
            lines = lines[:2]
            lines[-1] = lines[-1][:-1] + "…"
        a, d = f.getmetrics()
        block_h = (a + d + 12) * len(lines)
        y = avail_top + max(0, (avail_h - block_h) // 2)
        # 标题压暗条（只在标题区）
        bar = Image.new("RGBA", (tw, block_h + 30), (0, 0, 0, 110))
        fg.alpha_composite(bar, (0, max(0, y - 15)))
        _draw_block(draw, lines, f, tw // 2, y, fill=(255, 255, 255),
                    outline_w=3, line_gap=12)

    # ---- 图注（贴着字幕区上方）
    if caption:
        f = _font(int(min(tw * 0.026, th * 0.017)))
        lines = _wrap_cjk(caption, f, tw - 2 * margin_x)[:2]
        a, d = f.getmetrics()
        y = int(th * 0.72) - (a + d + 8) * len(lines)
        _draw_block(draw, lines, f, tw // 2, max(0, y), fill=(214, 220, 232),
                    outline_w=2, line_gap=8)

    # ---- 图片来源（
    if credit and bool(cfg.images.get("show_credit", False)):
        f = _font(int(min(tw * 0.019, th * 0.012)))
        txt = f"图源：{credit}"[:60]
        w = f.getlength(txt)
        draw.text((tw - margin_x - w, th - int(th * 0.135)), txt, font=f,
                  fill=(190, 196, 206), stroke_width=2, stroke_fill=(0, 0, 0))

    fg_out.parent.mkdir(parents=True, exist_ok=True)
    fg.save(fg_out, "PNG", optimize=True)
    return bg_out, fg_out


def cover_title_layout(title: str) -> str:
    """封面标题的折行布局：在第一个冒号后插一个换行。

    中文没有词边界，纯按宽度硬折会把「古代」拆成「古/代」、「到底」拆成「到/底」。
    本项目的标题统一是「小切口：具体疑问」结构，所以在冒号处断一次，
    再让 _wrap_cjk 按 \\n 分段各自折行，断点就落在短语边界上。
    """
    return re.sub(r"([：:])\s*", r"\1\n", title or "", count=1)


def build_cover(
    out_path: Path,
    size: tuple[int, int],
    cfg: Config,
    *,
    title: str,
    kicker: str = "",
    subtitle: str = "",
    foot: str = "",
    image_path: Path | None = None,
) -> Path:
    """生成封面图（单张 JPG，不是视频用的两层）。

    为什么要它：发布到任何平台都要一张封面/缩略图，而截图截出来的是「画面 + 字幕」，
    标题往往被字幕压住、也不够大。封面按平台习惯做成纯图：配图压暗当底，
    标题大字居中，顶部栏目名，底部一句补充信息。

    和分镜画面的区别：封面只有一张图（前景直接合成到背景上），不放字幕区，
    所以标题可以放得更大、位置更居中 —— 缩略图尺寸下也要看得清。
    """
    tw, th = size
    portrait = th >= tw
    title_font_px = int(min(tw * (0.105 if portrait else 0.070), th * (0.055 if portrait else 0.105)))
    margin_x = int(tw * 0.08)

    # ---------------- 背景
    bg_hex = str(cfg.video.get("fallback_bg", "0x101820")).replace("0x", "").replace("#", "")
    try:
        base_rgb = tuple(int(bg_hex[i:i + 2], 16) for i in (0, 2, 4))
    except (ValueError, IndexError):
        base_rgb = (16, 24, 32)
    bg = None
    if image_path and Path(image_path).exists():
        try:
            with Image.open(image_path) as src:
                bg = _cover_crop(src.convert("RGB"), (tw, th))
        except Exception as exc:  # noqa: BLE001
            log.warning("封面配图处理失败(%s)，用渐变底：%s", Path(image_path).name, exc)
    if bg is None:
        bg = _gradient((tw, th), base_rgb)
    # 封面比画面压得更暗 —— 标题大、要压得住图
    darken = float(cfg.video.get("cover_darken", 0.5))
    if darken > 0:
        bg = Image.blend(bg, Image.new("RGB", (tw, th), (0, 0, 0)), min(0.85, max(0.0, darken)))
    bg = bg.filter(ImageFilter.GaussianBlur(radius=0.8))
    if bg.mode != "RGB":
        bg = bg.convert("RGB")

    # ---------------- 前景（直接合成到背景上，封面就一张图）
    fg = Image.new("RGBA", (tw, th), (0, 0, 0, 0))
    fg.alpha_composite(_vertical_scrim((tw, th), 0.0, 0.42, 150, 0))
    fg.alpha_composite(_vertical_scrim((tw, th), 0.55, 1.0, 0, 130))
    draw = ImageDraw.Draw(fg)

    # 顶部：栏目名
    y = int(th * 0.075)
    if kicker:
        f = _font(int(min(tw * 0.040, th * 0.022)), bold=True)
        lines = _wrap_cjk(kicker, f, tw - 2 * margin_x)[:1]
        _draw_block(draw, lines, f, tw // 2, y, fill=(255, 219, 120), outline_w=2, line_gap=6)
        y += (f.getmetrics()[0] + f.getmetrics()[1]) + int(th * 0.055)

    # 中部：主标题（字号自适应，最多 3 行，不超出可用高度）
    #
    # 折行要避开「断在词中间」：中文没有词边界，纯按宽度硬折会把「古代」拆成
    # 「古/代」、「到底」拆成「到/底」（视觉检查抓到过，很难看）。
    # 本项目的标题统一是「小切口：具体疑问」结构，所以在冒号处优先断一次，
    # _wrap_cjk 会按 \n 分段各自折行，两段各自贴边 → 断点自然落在短语边界上。
    title_layout = cover_title_layout(title)
    avail_h = int(th * (0.40 if portrait else 0.34))
    base = title_font_px
    f = _font(base, bold=True)
    lines = [ln for ln in _wrap_cjk(title_layout, f, tw - 2 * margin_x) if ln.strip()]
    while base > 30 and (
        len(lines) > 3
        or (f.getmetrics()[0] + f.getmetrics()[1] + 14) * len(lines) > avail_h
    ):
        base = int(base * 0.92)
        f = _font(base, bold=True)
        lines = [ln for ln in _wrap_cjk(title_layout, f, tw - 2 * margin_x) if ln.strip()]
    if len(lines) > 3:
        lines = lines[:3]
        lines[-1] = lines[-1][:-1] + "…"
    a, d = f.getmetrics()
    block_h = (a + d + 14) * len(lines)
    ty = y + max(0, (avail_h - block_h) // 2)
    bar = Image.new("RGBA", (tw, block_h + 36), (0, 0, 0, 105))
    fg.alpha_composite(bar, (0, max(0, ty - 18)))
    end_y = _draw_block(draw, lines, f, tw // 2, ty, fill=(255, 255, 255),
                        outline_w=4, line_gap=14)

    # 标题下：副标题（本期问题/年代）
    if subtitle:
        f2 = _font(int(min(tw * 0.034, th * 0.021)))
        sub = _wrap_cjk(subtitle, f2, tw - 2 * margin_x)[:3]
        _draw_block(draw, sub, f2, tw // 2, end_y + int(th * 0.030),
                    fill=(226, 231, 240), outline_w=2, line_gap=8)

    # 底部：补充信息（如 slogan）。
    # 位置放在 0.885h 左右而不是贴底 —— 竖版平台（抖音/视频号）底部约 15% 会被
    # 账号信息与按钮盖住，贴底的那行字等于白写。
    if foot:
        f3 = _font(int(min(tw * 0.026, th * 0.016)), bold=True)
        lines3 = _wrap_cjk(foot, f3, tw - 2 * margin_x)[:1]
        a3, d3 = f3.getmetrics()
        _draw_block(draw, lines3, f3, tw // 2, th - int(th * 0.115) - a3 - d3,
                    fill=(255, 214, 82), outline_w=2, line_gap=6)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    Image.alpha_composite(bg.convert("RGBA"), fg).convert("RGB").save(
        out_path, "JPEG", quality=93)
    log.info("封面：%s（%dx%d，%s）", out_path.name, tw, th,
             "竖版" if portrait else "横版")
    return out_path


def build_text_card(
    bg_out: Path,
    fg_out: Path,
    size: tuple[int, int],
    cfg: Config,
    *,
    lines: list[str],
    slogan: str = "",
    subtitle: str = "",
    bg: str = "0x101820",
) -> tuple[Path, Path]:
    """片头 / 片尾的纯文字画面（三级层次：主标题 / 介绍语 / 补充信息）。"""
    tw, th = size
    ms = float(cfg.video.get("motion_scale", 1.16)) if bool(cfg.video.get("motion", True)) else 1.0
    hexv = str(bg).replace("0x", "").replace("#", "")
    try:
        rgb = tuple(int(hexv[i : i + 2], 16) for i in (0, 2, 4))
    except (ValueError, IndexError):
        rgb = (16, 24, 32)
    bg_img = _gradient((_even(tw * ms), _even(th * ms)), rgb)
    bg_out.parent.mkdir(parents=True, exist_ok=True)
    bg_img.save(bg_out, "JPEG", quality=92)

    fg = Image.new("RGBA", (tw, th), (0, 0, 0, 0))
    draw = ImageDraw.Draw(fg)
    portrait = th >= tw

    f = _font(int(tw * (0.105 if portrait else 0.062)), bold=True)
    wrapped: list[str] = []
    for ln in lines:
        wrapped += _wrap_cjk(ln, f, int(tw * 0.86))
    a, d = f.getmetrics()
    line_h = a + d + 18

    fs = fx = None
    slogan_lines: list[str] = []
    slogan_h = 0
    if slogan:
        fs = _font(int(tw * (0.052 if portrait else 0.034)), bold=True)
        slogan_lines = _wrap_cjk(slogan, fs, int(tw * 0.86))
        sa, sd = fs.getmetrics()
        slogan_h = (sa + sd + 10) * len(slogan_lines)
    sub_lines: list[str] = []
    sub_h = 0
    if subtitle:
        fx = _font(int(tw * (0.032 if portrait else 0.022)))
        sub_lines = _wrap_cjk(subtitle, fx, int(tw * 0.86))
        ua, ud = fx.getmetrics()
        sub_h = (ua + ud + 8) * len(sub_lines)

    total = line_h * len(wrapped) + (slogan_h + 26 if slogan_h else 0) + (sub_h + 26 if sub_h else 0)
    y = max(int(th * 0.06), (th - total) // 2)
    _draw_block(draw, wrapped, f, tw // 2, y, fill=(255, 255, 255), outline_w=3, line_gap=18)
    y += line_h * len(wrapped)
    if slogan_lines and fs is not None:
        y += 26
        _draw_block(draw, slogan_lines, fs, tw // 2, y, fill=(255, 214, 82),
                    outline_w=2, line_gap=10)
        y += slogan_h
    if sub_lines and fx is not None:
        y += 26
        _draw_block(draw, sub_lines, fx, tw // 2, y, fill=(205, 210, 220),
                    outline_w=2, line_gap=8)

    fg_out.parent.mkdir(parents=True, exist_ok=True)
    fg.save(fg_out, "PNG", optimize=True)
    return bg_out, fg_out
