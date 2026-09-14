"""生成 ASS 字幕。

为什么手写 ASS 而不是 srt：
  · 能精确控制中文字体、描边、字号（srt 烧进视频要靠 ffmpeg 的 force_style，易踩坑）
  · 能做底部半透明压暗条，可读性显著提升

时间轴：按每条字幕的字数占比分配该分镜的语音时长。
一个分镜一段语音，字数占比 ≈ 时间占比，误差很小。
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from .config import Config
from .textfmt import fix_line_punct

log = logging.getLogger("hsg.subtitles")

_SENT_SPLIT = re.compile(r"(?<=[。！？!?；;])")
_CLAUSE_SPLIT = re.compile(r"(?<=[，,、：:])")


def _ass_time(sec: float) -> str:
    sec = max(0.0, sec)
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = sec % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def split_cues(text: str, max_chars: int) -> list[str]:
    """把一段口播切成若干条字幕（每条不超过 max_chars 字）。"""
    text = re.sub(r"\s+", "", text or "")
    if not text:
        return []
    parts: list[str] = []
    for sent in _SENT_SPLIT.split(text):
        if not sent:
            continue
        if len(sent) <= max_chars:
            parts.append(sent)
            continue
        buf = ""
        for cl in _CLAUSE_SPLIT.split(sent):
            if not cl:
                continue
            if len(buf) + len(cl) <= max_chars:
                buf += cl
            else:
                if buf:
                    parts.append(buf)
                while len(cl) > max_chars:
                    parts.append(cl[:max_chars])
                    cl = cl[max_chars:]
                buf = cl
        if buf:
            parts.append(buf)

    merged: list[str] = []
    for p in parts:
        if merged and len(merged[-1]) + len(p) <= max_chars:
            merged[-1] += p
        else:
            merged.append(p)
    return merged


def _wrap(cue: str, per_line: int, max_lines: int) -> str:
    """用 \\N 手动折行（WrapStyle=2 不自动换行）。"""
    lines = [cue[i : i + per_line] for i in range(0, len(cue), per_line)]
    if len(lines) > max_lines:
        head = lines[: max_lines - 1]
        head.append("".join(lines[max_lines - 1 :]))
        lines = head
    return r"\N".join(fix_line_punct(lines))


def build_ass(
    cues: list[str],
    total_duration: float,
    out_path: Path,
    size: tuple[int, int],
    cfg: Config,
    *,
    start_offset: float = 0.0,
    end_offset: float = 0.0,
) -> Path:
    """把 cues 按字数比例铺满 [start_offset, total_duration - end_offset]。"""
    sc = cfg.subtitle
    tw, th = size
    portrait = th >= tw
    font_size = int(sc.font_size_portrait if portrait else sc.font_size_landscape)
    per_line = int(sc.max_chars_per_line)
    max_lines = int(sc.max_lines)
    margin_v = int(th * (1.0 - float(sc.margin_v_ratio)))
    font_name = str(sc.font_name)

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {tw}
PlayResY: {th}
WrapStyle: 2
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.709

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Bg,{font_name},{font_size},&H73000000,&H73000000,&H73000000,&H73000000,0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1
Style: Main,{font_name},{font_size},{sc.primary_color},&H000000FF,{sc.outline_color},&H64000000,1,0,0,0,100,100,0,0,1,{int(sc.outline)},{int(sc.shadow)},2,{int(tw*0.05)},{int(tw*0.05)},{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    out_path.parent.mkdir(parents=True, exist_ok=True)
    if not cues:
        out_path.write_text(header, encoding="utf-8")
        return out_path

    avail_start = max(0.0, start_offset)
    avail_end = max(avail_start + 0.5, total_duration - end_offset)
    avail = avail_end - avail_start
    total_chars = sum(max(1, len(c)) for c in cues) or 1

    events: list[str] = []
    t = avail_start
    bar_h = int(font_size * (max_lines * 1.35) + 24)
    for cue in cues:
        share = avail * (max(1, len(cue)) / total_chars)
        seg_end = min(avail_end, t + max(0.9, share))
        if sc.bottom_bar:
            y2 = th - margin_v + int(font_size * 0.35)
            y1 = max(0, y2 - bar_h)
            drawing = (
                f"{{\\an7\\pos(0,{y1})\\p1\\bord0\\shad0}}"
                f"m 0 0 l {tw} 0 l {tw} {bar_h} l 0 {bar_h}{{\\p0}}"
            )
            events.append(f"Dialogue: 0,{_ass_time(t)},{_ass_time(seg_end)},Bg,,0,0,0,,{drawing}")
        events.append(
            f"Dialogue: 0,{_ass_time(t)},{_ass_time(seg_end)},Main,,0,0,0,,"
            f"{_wrap(cue, per_line, max_lines)}"
        )
        t = seg_end
        if t >= avail_end:
            break

    out_path.write_text(header + "\n".join(events) + "\n", encoding="utf-8")
    return out_path


def cues_for(scene, cfg: Config) -> list[str]:
    sc = cfg.subtitle
    return split_cues(getattr(scene, "text", "") or "",
                      int(sc.max_chars_per_line) * int(sc.max_lines))
