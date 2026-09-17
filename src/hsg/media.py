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


def _blurpad(im: Image.Image, size: tuple[int, int], blur: int = 28,
             darken: float = 0.10) -> Image.Image:
    """完整画面居中 + 四周用同一张图放大模糊当底（跟 `clips.fit=blurpad` 一个思路）。

    为什么四格漫画必须走这条：漫画格是**方形**，画布是 16:9 / 9:16。
    走 `_cover_crop`（默认）会裁掉格子的上下或左右 —— 实测把人物头部和卷轴下端切掉了
    （画面上看得出"这个人没头"，很难解释）。blurpad 保证整格都在画面里。
    """
    tw, th = size
    bg = _cover_crop(im, size).filter(ImageFilter.GaussianBlur(radius=blur))
    if darken > 0:
        bg = Image.blend(bg, Image.new("RGB", size, (0, 0, 0)), darken)
    sw, sh = im.size
    scale = min(tw / sw, th / sh)
    fg = im.resize((max(1, int(sw * scale)), max(1, int(sh * scale))), Image.LANCZOS)
    bg.paste(fg, ((tw - fg.width) // 2, (th - fg.height) // 2))
    return bg


def _cover_crop(im: Image.Image, size: tuple[int, int]) -> Image.Image:
    tw, th = size
    sw, sh = im.size
    scale = max(tw / sw, th / sh)
    nw, nh = max(1, int(sw * scale)), max(1, int(sh * scale))
    im = im.resize((nw, nh), Image.LANCZOS)
    left, top = (nw - tw) // 2, (nh - th) // 2
    return im.crop((left, top, left + tw, top + th))


def hex_rgb(value, default: tuple[int, int, int]) -> tuple[int, int, int]:
    """「0xC2571A」/「#c2571a」→ (194, 87, 26)；给了坏值就用默认色。"""
    h = str(value or "").replace("0x", "").replace("#", "").strip()
    try:
        if len(h) != 6:
            raise ValueError(h)
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]
    except (ValueError, IndexError):
        return default


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
                line_gap: int = 10, align: str = "center") -> int:
    """画一段多行文字。

    `align="center"`：`cx` 是**中心**（默认，绝大多数文字都居中）。
    `align="left"`：`cx` 变成**左边缘** —— 左上角的关键词徽标要用它。
    """
    y = top
    ascent, descent = font.getmetrics()
    for ln in lines:
        w = font.getlength(ln)
        x = cx if align == "left" else cx - w / 2
        if outline_w > 0:
            draw.text((x, y), ln, font=font, fill=fill,
                      stroke_width=outline_w, stroke_fill=outline)
        else:
            draw.text((x, y), ln, font=font, fill=fill)
        y += ascent + descent + line_gap
    return y


def _vertical_scrim(size: tuple[int, int], y0: float, y1: float,
                    a0: int, a1: int, color: tuple[int, int, int] = (0, 0, 0)) -> Image.Image:
    """竖向透明渐变遮罩（用于上下压暗，提升文字可读性）。"""
    tw, th = size
    ya, yb = int(th * y0), int(th * y1)
    h = max(1, yb - ya)
    strip = Image.new("RGBA", (1, h), (*color, 0))
    px = strip.load()
    for i in range(h):
        a = int(a0 + (a1 - a0) * (i / max(1, h - 1)))
        px[0, i] = (0, 0, 0, max(0, min(255, a)))
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    layer.paste(strip.resize((tw, h), Image.BILINEAR), (0, ya))
    return layer


# ---------------------------------------------------------------- 剪辑标注元素
# 版面分区（改这里要同步改 test_verify 的像素断言）：
#   0.045-0.075 栏目小字　0.085-0.255 章节标题　0.41-0.51 大字标注
#   0.59-0.65 人名条　0.72 附近 图注　底部 字幕
# 大字标注（关键词）默认放在**画面左上角**：它原来压在画面正中，会挡住素材的主体
# （实测观众想看的是人脸/朝堂，中间那块正是画面最有信息量的地方）。
# 同时章节标题**下移让位**（见 TITLE_TOP），两个元素都落在上三分之一里，互不重叠。
CALLOUT_CX = 0.06          # 大字标注的左边缘（比例）
CALLOUT_CY = 0.135         # 大字标注的垂直中心
CALLOUT_ALIGN = "left"     # left（左上角）| center（旧的居中样式）
TITLE_TOP = 0.20           # 章节标题区的起点（原来 0.085，给左上角的关键词让位）
NAMETAG_CY = 0.62          # 人名条的垂直中心


def _rounded_bar(size: tuple[int, int], *, fill=(0, 0, 0), alpha=150,
                 accent: tuple[int, int, int] | None = None,
                 accent_w: int = 8) -> Image.Image:
    """半透明底条 + 可选左侧色条（标注元素的底）。"""
    w, h = size
    bar = Image.new("RGBA", (w, h), fill + (alpha,))
    if accent:
        d = ImageDraw.Draw(bar)
        d.rectangle([0, 0, max(1, accent_w), h], fill=accent + (235,))
    return bar


def draw_callout(fg: Image.Image, cfg: Config, text: str) -> Image.Image:
    """在画面上打一个大字标注（解说视频里那一下「咚」的重音）。

    为什么字号这么大：2-4 个字要能在手机小屏上一眼读完，所以占比比章节标题还大；
    而且必须带底条 —— 素材画面的亮度不可控，没底条时白字会消失在亮背景里。
    """
    text = (text or "").strip()
    if not text:
        return fg
    tw, th = fg.size
    size = int(min(tw * 0.115, th * 0.075))
    font = _font(size, bold=True)
    limit = float(cfg.video.get("callout_max_width", 0.62))
    while font.getlength(text) > tw * limit and size > 20:
        size = int(size * 0.9)
        font = _font(size, bold=True)
    a, d = font.getmetrics()
    block_h = a + d
    cx = float(cfg.video.get("callout_cx", CALLOUT_CX))
    cy = float(cfg.video.get("callout_cy", CALLOUT_CY))
    align = str(cfg.video.get("callout_align", CALLOUT_ALIGN) or CALLOUT_ALIGN)
    top = int(th * cy) - block_h // 2
    pad_y = int(block_h * 0.28)
    pad_x = int(tw * 0.045)
    bar_w = int(font.getlength(text)) + 2 * pad_x
    bar = _rounded_bar((bar_w, block_h + 2 * pad_y), alpha=132,
                       accent=(212, 175, 55), accent_w=max(4, int(block_h * 0.10)))
    bar_x = max(0, (tw - bar_w) // 2) if align == "center" else max(0, int(tw * cx))
    text_x = (tw // 2) if align == "center" else int(tw * cx) + pad_x
    fg.alpha_composite(bar, (bar_x, max(0, top - pad_y)))
    draw = ImageDraw.Draw(fg)
    _draw_block(draw, [text], font, text_x, top, fill=(255, 245, 214),
                outline=(0, 0, 0), outline_w=max(4, size // 12), line_gap=0,
                align="left" if align != "center" else "center")
    return fg


def draw_nametag(fg: Image.Image, cfg: Config, text: str) -> Image.Image:
    """左下角的人物名条（观众不认识这张脸时才给）。"""
    text = (text or "").strip()
    if not text:
        return fg
    tw, th = fg.size
    size = int(min(tw * 0.032, th * 0.022))
    font = _font(size, bold=True)
    a, d = font.getmetrics()
    block_h = a + d
    top = int(th * NAMETAG_CY) - block_h // 2
    pad_x = int(size * 0.9)
    pad_y = int(block_h * 0.24)
    bar_w = int(font.getlength(text)) + 2 * pad_x
    accent_w = max(4, int(size * 0.34))
    x = int(tw * 0.07)
    bar = _rounded_bar((bar_w, block_h + 2 * pad_y), alpha=150,
                       accent=(212, 175, 55), accent_w=accent_w)
    fg.alpha_composite(bar, (x, max(0, top - pad_y)))
    draw = ImageDraw.Draw(fg)
    draw.text((x + accent_w + pad_x, top), text, font=font, fill=(255, 248, 230),
              stroke_width=2, stroke_fill=(0, 0, 0))
    return fg


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
    callout: str = "",
    nametag: str = "",
    darken: float = 0.34,
    fit: str = "",
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
                rgb_src = src.convert("RGB")
                # fit=cover（默认，配图用）/ fit=blurpad（四格漫画用，见 _blurpad 注释）
                bg = (_blurpad(rgb_src, (bw, bh))
                      if str(fit or "").lower() == "blurpad"
                      else _cover_crop(rgb_src, (bw, bh)))
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
        avail_top = int(th * float(cfg.video.get("title_top", TITLE_TOP)))
        avail_bottom = int(th * float(cfg.video.get("title_bottom", 0.38)))
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

    # ---- 剪辑标注元素（影视切片路线的 T4：大字 + 人名条）
    # 放在 build_layers 里是**故意的**：版面代码只有一份，
    # 切片镜头和静态图镜头就不会各自演化、慢慢跑偏。
    if callout:
        fg = draw_callout(fg, cfg, callout)
    if nametag:
        fg = draw_nametag(fg, cfg, nametag)

    fg_out.parent.mkdir(parents=True, exist_ok=True)
    fg.save(fg_out, "PNG", optimize=True)
    return bg_out, fg_out


_CLAUSE_SPLIT = re.compile(r"(?<=[，。、；：！？,.;:!?])")


def _wrap_by_clause(text: str, font: ImageFont.FreeTypeFont, max_width: int,
                    max_lines: int = 0) -> list[str]:
    """按标点优先断行（先把小句切出来，再贪心填行）。

    为什么不能只用 _wrap_cjk：中文没有词边界，纯按宽度硬折会把词拆到两行 ——
    视觉复核在流水线产出的封面上抓到过「县官」被拆成「县/官」。
    先按标点切成小句、再填行，断点就落在标点上，不会断词。
    单个小句本身就超宽时（中间没有标点的长句），退回硬折，这是没办法的事。
    """
    s = (text or "").strip()
    if not s:
        return []
    lines: list[str] = []
    cur = ""
    for cl in [c for c in _CLAUSE_SPLIT.split(s) if c]:
        if font.getlength(cl) > max_width:
            if cur:
                lines.append(cur)
                cur = ""
            lines.extend(_wrap_cjk(cl, font, max_width))
            continue
        if font.getlength(cur + cl) <= max_width:
            cur += cl
        else:
            if cur:
                lines.append(cur)
            cur = cl
    if cur:
        lines.append(cur)
    if max_lines and len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1][:-1] + "…"
    return lines


def cover_title_layout(title: str) -> str:
    """封面标题的折行布局：在第一个冒号后插一个换行。

    中文没有词边界，纯按宽度硬折会把「古代」拆成「古/代」、「到底」拆成「到/底」。
    本项目的标题统一是「小切口：具体疑问」结构，所以在冒号处断一次，
    再让 _wrap_cjk 按 \\n 分段各自折行，断点就落在短语边界上。
    """
    return re.sub(r"([：:])\s*", r"\1\n", title or "", count=1)


def _title_segments(title_layout: str) -> list[str]:
    """把（已按冒号断过一次的）标题拆成段：每段都希望**整段占一行**。"""
    return [s.strip() for s in (title_layout or "").split("\n") if s.strip()]


def cover_title_lines(title_layout: str, font: ImageFont.FreeTypeFont,
                      max_width: int) -> list[str]:
    """把标题排成行：**优先整段占一行**。

    为什么不直接调 _wrap_by_clause：冒号后那一段内部往往没有标点
    （「宵禁之后出门会怎样？」整段就是一个小句），标点优先断行会当场退回硬折，
    于是「怎样」被折成「怎/样」。第 7 期封面真发生过，
    是用「重建纯背景层 + 逐像素差分」定位到 y 440–937 那两行的。

    所以顺序是：整段放下 → 按标点断 → 硬折（最后手段）。
    字号由 cover_title_fit 在循环里往下缩，缩到能整段放下为止。
    """
    out: list[str] = []
    for seg in _title_segments(title_layout):
        if font.getlength(seg) <= max_width:
            out.append(seg)
        else:
            out.extend(_wrap_by_clause(seg, font, max_width))
    return [ln for ln in out if ln.strip()]


def cover_title_fit(title_layout: str, base_px: int, max_width: int, avail_h: int,
                    min_px: int = 30) -> tuple[ImageFont.FreeTypeFont, list[str]]:
    """给标题挑一个字号：从 base_px 往下缩，缩到每段能整段占一行、且不超高。

    ⚠️ 判据里必须有「还有段被硬折」这一条。只靠 len(lines) > 3 是不够的：
    「夜里的城：宵禁之后出门会怎样？」在 105px 下会折成 3 行
    （「夜里的城：」/「宵禁之后出门会怎」/「样？」），3 行并没有超额，
    字号就不会往下缩，「样？」就一直留在那儿 —— 第一次改的时候正是这么翻的车。

    抽成独立函数是为了**可测**：这个回归必须能用断言钉住，
    不能靠人眼看图（在图上看不出「怎/样」和「怎样」的区别）。
    返回 (字体, 行列表)；超过 3 行才用省略号收尾。
    """
    segs = _title_segments(title_layout)
    base = max(min_px, int(base_px))
    f = _font(base, bold=True)

    def too_wide(ff: ImageFont.FreeTypeFont) -> bool:
        return any(ff.getlength(s) > max_width for s in segs)

    lines = cover_title_lines(title_layout, f, max_width)
    while base > min_px and (
        too_wide(f)                                  # 还有段放不下 → 继续缩字号
        or len(lines) > 3
        or (f.getmetrics()[0] + f.getmetrics()[1] + 14) * len(lines) > avail_h
    ):
        base = int(base * 0.94)      # 步长 0.94：缩得更细，尽量靠缩字号保住整段
        f = _font(base, bold=True)
        lines = cover_title_lines(title_layout, f, max_width)
    if len(lines) > 3:
        lines = lines[:3]
        lines[-1] = lines[-1][:-1] + "…"
    return f, lines


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

    # ---------------- 背景（橙色风格：底图不管什么颜色，一律往橙色拉一遍）
    base_rgb = hex_rgb(cfg.video.get("cover_base", "#5A2A0C"), (90, 42, 12))
    tint_rgb = hex_rgb(cfg.video.get("cover_tint", "#C2571A"), (194, 87, 26))
    tint_alpha = float(cfg.video.get("cover_tint_alpha", 0.54))
    warm_dark = hex_rgb(cfg.video.get("cover_scrim", "#1A0A02"), (26, 10, 2))
    bg = None
    if image_path and Path(image_path).exists():
        try:
            with Image.open(image_path) as src:
                bg = _cover_crop(src.convert("RGB"), (tw, th))
        except Exception as exc:  # noqa: BLE001
            log.warning("封面配图处理失败(%s)，用渐变底：%s", Path(image_path).name, exc)
    if bg is None:
        bg = _gradient((tw, th), base_rgb)
    # 色调统一：不管底图原来是冷色还是彩色，都往橙色拉 —— 封面是最先被看到的一帧，
    # 一整套橙色比"每期颜色都不一样"更像一个栏目的片子（用户 2026-09-16 要求）。
    if tint_alpha > 0:
        bg = Image.blend(bg, Image.new("RGB", (tw, th), tint_rgb),
                         min(1.0, max(0.0, tint_alpha)))
    # 封面比画面压得更暗 —— 标题大、要压得住图（压暗也用暖色，别拉回冷调）
    darken = float(cfg.video.get("cover_darken", 0.5))
    if darken > 0:
        bg = Image.blend(bg, Image.new("RGB", (tw, th), warm_dark),
                         min(0.85, max(0.0, darken)))
    bg = bg.filter(ImageFilter.GaussianBlur(radius=0.8))
    if bg.mode != "RGB":
        bg = bg.convert("RGB")

    # ---------------- 前景（直接合成到背景上，封面就一张图）
    fg = Image.new("RGBA", (tw, th), (0, 0, 0, 0))
    fg.alpha_composite(_vertical_scrim((tw, th), 0.0, 0.42, 150, 0, warm_dark))
    fg.alpha_composite(_vertical_scrim((tw, th), 0.55, 1.0, 0, 130, warm_dark))
    draw = ImageDraw.Draw(fg)

    # 顶部：栏目名
    y = int(th * 0.075)
    if kicker:
        f = _font(int(min(tw * 0.040, th * 0.022)), bold=True)
        lines = _wrap_cjk(kicker, f, tw - 2 * margin_x)[:1]
        _draw_block(draw, lines, f, tw // 2, y, fill=(255, 219, 120), outline_w=2, line_gap=6)
        y += (f.getmetrics()[0] + f.getmetrics()[1]) + int(th * 0.018)
        # 橙色装饰条：把"栏目名"和"标题"分开，也是橙色风格的视觉锚点
        rule_w = int(tw * 0.16)
        rule_h = max(3, int(th * 0.006))
        draw.rectangle([(tw - rule_w) // 2, y, (tw + rule_w) // 2, y + rule_h],
                       fill=(*tint_rgb, 235))
        y += rule_h + int(th * 0.030)

    # 中部：主标题（字号自适应，最多 3 行，不超出可用高度）
    #
    # 折行要避开「断在词中间」：中文没有词边界，纯按宽度硬折会把「古代」拆成
    # 「古/代」、「到底」拆成「到/底」（视觉检查抓到过，很难看）。
    # 本项目的标题统一是「小切口：具体疑问」结构，所以在冒号处优先断一次，
    # 但冒号后那一段内部常常没有标点，光靠这一步不够 —— 见 cover_title_lines。
    title_layout = cover_title_layout(title)
    avail_h = int(th * (0.40 if portrait else 0.34))
    f, lines = cover_title_fit(title_layout, title_font_px, tw - 2 * margin_x, avail_h)
    a, d = f.getmetrics()
    block_h = (a + d + 14) * len(lines)
    ty = y + max(0, (avail_h - block_h) // 2)
    bar = Image.new("RGBA", (tw, block_h + 36), (0, 0, 0, 105))
    fg.alpha_composite(bar, (0, max(0, ty - 18)))
    end_y = _draw_block(draw, lines, f, tw // 2, ty, fill=(255, 255, 255),
                        outline_w=4, line_gap=14)

    # 标题下：副标题（本期问题/年代）—— 用标点优先断行，别把词拆到两行
    if subtitle:
        f2 = _font(int(min(tw * 0.034, th * 0.021)))
        sub = _wrap_by_clause(subtitle, f2, tw - 2 * margin_x, max_lines=3)
        _draw_block(draw, sub, f2, tw // 2, end_y + int(th * 0.030),
                    fill=(226, 231, 240), outline_w=2, line_gap=8)

    if foot:
        f3 = _font(int(min(tw * 0.026, th * 0.016)), bold=True)
        lines3 = _wrap_cjk(foot, f3, tw - 2 * margin_x)[:1]
        a3, d3 = f3.getmetrics()
        # 距底留白：竖版必须躲开平台底部约 15% 的遮挡区（账号信息/按钮），
        # 贴底或压线的字等于白写。
        # ⚠️ 原来写 0.115，实测字块落在距底 11.6%–13.2%，**仍在遮挡区内**
        #    —— 是「重建纯背景层 + 逐像素差分」量出来的（字块 y 1667–1698 / 1920），
        #    上一轮靠视觉模型估位置，估错了。现在留 0.19。
        #    横版（B站/YouTube）没有这个遮挡问题，保持贴着底部。
        foot_margin = int(th * (0.19 if portrait else 0.075))
        _draw_block(draw, lines3, f3, tw // 2, th - foot_margin - a3 - d3,
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
