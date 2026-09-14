#!/usr/bin/env python3
"""Objective video-slide layout check: do the title band and subtitle band overlap?

WHY THIS EXISTS
    Eyeballing a rendered frame (or asking a vision model) is unreliable for "is the
    title sitting on top of the subtitles?". This measures it instead: it counts
    near-white pixels per scanline, groups them into text bands, and asserts the
    title band and subtitle band are separated by a real gap.

    A real bug this caught: title drawn at 0.79h-0.93h while ASS subtitles were drawn
    at 0.79h-0.88h -> visually "a bit busy", numerically a hard overlap.

USAGE
    # extract a frame first (mid-segment is best, subtitles are showing)
    ffmpeg -hide_banner -loglevel error -y -ss 20 -i final.mp4 -frames:v 1 frame.png
    python check_layout.py frame.png [more_frames.png ...]

    # no args -> falls back to ./data/tmp/smoke/f2_{portrait,landscape}.png

EXIT CODES
    0 = all frames passed (or nothing to check)
    1 = at least one frame has an overlap / missing band

TUNING
    TITLE_ZONE / SUB_ZONE below assume the zoning used by this project's media.build_layers():
      ~0.045h 栏目名·章节号 | ~0.085-0.255h 章节标题 | ~0.72h 图注 | ~0.78-1.00h 字幕
    (配图是整屏铺满的，没有独立的图片区)
    Adjust the constants if a project zones its canvas differently.
"""

from __future__ import annotations

import sys
from pathlib import Path

try:
    from PIL import Image
except ImportError:  # pragma: no cover
    print("需要 Pillow:  python -m pip install pillow", file=sys.stderr)
    raise SystemExit(2)

# Fraction-of-height window that counts as "the title band".
TITLE_ZONE = (0.05, 0.32)
# Anything at/after this fraction is "the subtitle band".
SUB_ZONE = 0.78
# Minimum vertical gap (px) required between the two bands.
MIN_GAP_PX = 5
# A band must be at least this tall (px) to not be dismissed as antialiasing noise.
MIN_BAND_PX = 6
# Row is "text" when at least this many sampled pixels are near-white.
WHITE_LEVEL = 235
SAMPLE_STEP = 3          # sample every Nth column; 3 is plenty and ~3x faster


def bright_bands(path: Path) -> list[tuple[int, int]]:
    """Return [(start_y, end_y), ...] scanline bands containing near-white pixels."""
    im = Image.open(path).convert("L")
    w, h = im.size
    px = im.load()

    rows: list[int] = []
    for y in range(h):
        cnt = 0
        for x in range(0, w, SAMPLE_STEP):
            if px[x, y] > WHITE_LEVEL:
                cnt += 1
        rows.append(cnt)

    thresh = max(3, w // 200)
    bands: list[tuple[int, int]] = []
    start: int | None = None
    for y, c in enumerate(rows):
        if c >= thresh and start is None:
            start = y
        elif c < thresh and start is not None:
            if y - start >= MIN_BAND_PX:
                bands.append((start, y))
            start = None
    if start is not None:
        bands.append((start, h))
    return bands


def check(path: Path) -> bool:
    if not path.exists():
        print(f"缺帧 {path}")
        return False

    im = Image.open(path)
    w, h = im.size
    bands = bright_bands(path)

    print(f"\n=== {path.stem}  {w}x{h} ===")
    for a, b in bands:
        print(f"  文字带  y={a:5d}~{b:5d}   ({a/h:.3f}h ~ {b/h:.3f}h)  高={b-a}px")

    title = [bd for bd in bands if TITLE_ZONE[0] * h <= bd[0] < TITLE_ZONE[1] * h]
    sub = [bd for bd in bands if bd[0] >= SUB_ZONE * h]

    if title and sub:
        gap = sub[0][0] - title[-1][1]
        ok = gap > MIN_GAP_PX
        print(f"  标题带 {title[-1]}  字幕带 {sub[0]}  间隔 = {gap}px "
              f"{'✅ 未重叠' if ok else '❌ 重叠/过近'}")
        return ok

    print(f"  标题带={title} 字幕带={sub}")
    print("  ⚠️  未能同时识别出两条文字带 —— 可能是该帧没有字幕，"
          "或版面分区常量与本项目不符，需人工确认")
    return False


def main(argv: list[str]) -> int:
    default = [Path(f"data/tmp/smoke/{n}/frame.png") for n in ("portrait", "landscape")]
    paths = [Path(a) for a in argv[1:]] or default
    results = [check(p) for p in paths]
    bad = results.count(False)
    print(f"\n结果：{len(results) - bad} 通过 / {bad} 失败")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
