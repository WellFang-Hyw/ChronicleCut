"""从 metadata.json 载入 Story，以及相关的读取工具。

为什么单独一个模块：这段逻辑有三个调用方 ——
  · `scripts/rerender.py`（重渲染）
  · `scripts/make_needs.py`（出素材需求清单）
  · `src/hsg/agent.py`（阶段 2 续跑）
放在 scripts 里会让 scripts 互相 import（脆弱），放在库里才对。

⚠️ 载入**不**填 `Scene.duration` 的真值来源只有一个：音频缓存文件探测。
   缓存 key = 文本 + 音色 + 语速，所以换过音色的期会探不到（时长记 0）。
   那种情况下别以为「没时长」，请看 metadata 里当初记的 `seconds`
   （`recorded_durations()` 就是取它）。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from . import tts as tts_mod
from .config import Config
from .models import Chapter, Scene, Story

log = logging.getLogger("hsg.storyio")


def load_story(meta_path: Path, cfg: Config, audio_dir: Path, log_) -> tuple[Story, dict]:
    """返回 (Story, metadata 原始字典)。第二个返回值给调用方看当初的 tts 设定。"""
    data = json.loads(Path(meta_path).read_text(encoding="utf-8"))
    sub = cfg.tts[str(cfg.tts.provider)]
    voice = str(sub.get("voice_id") or "")
    speed = sub.get("speed")
    chapters: list[Chapter] = []
    for c in data.get("chapters") or []:
        scenes: list[Scene] = []
        for k, s in enumerate(c.get("scenes") or []):
            text = str(s.get("text") or "")
            # 音频缓存 key = 文本 + 音色 + 语速，所以重渲染必须用**同样的语速**
            tag = tts_mod.audio_tag(text, voice, speed)
            idx = int(s.get("index") or (k + 1))
            audio = audio_dir / f"scene_{idx:03d}_{tag}.mp3"
            scenes.append(Scene(
                index=idx, text=text,
                image_query=str(s.get("image_query") or ""),
                caption=str(s.get("caption") or ""),
                chapter_index=int(c.get("index") or 1),
                is_chapter_start=(k == 0),
                audio_path=audio if audio.exists() else None,
                duration=tts_mod.probe_duration(audio) if audio.exists() else 0.0,
            ))
        chapters.append(Chapter(index=int(c.get("index") or 1),
                                heading=str(c.get("heading") or ""),
                                summary=str(c.get("summary") or ""),
                                seconds=int(c.get("seconds_target") or 60),
                                facts=list(c.get("facts") or []),
                                image_queries=list(c.get("image_queries") or []),
                                scenes=scenes))
    story = Story(
        topic=str(data.get("topic") or ""),
        title=str(data.get("title") or ""),
        angle_question=str(data.get("angle_question") or ""),
        hook=str(data.get("hook") or ""),
        # ⚠️ 这两行漏过一次，后果不小：stage 2 重建的 story 没有系列信息，
        # 于是「开场白报系列与期号」和画面顶部的系列标签**在成片里都是缺的** ——
        # 而冒烟直接构造 Story 对象，验不到这条真实路径（护栏 38 说的就是这种坑）。
        series=str(data.get("series") or ""),
        series_ep=int(data.get("series_ep") or 0),
        period=str(data.get("period") or ""),
        period_start=int(data.get("period_start") or 0),
        period_end=int(data.get("period_end") or 0),
        chapters=chapters,
        material=str(data.get("material") or ""),
        # metadata 落盘的字段名是 unresolved（见 pipeline.write_metadata），读回来必须认它。
        # ⚠️ 漏过的后果：`load_story` 读不到 → 再 `write_metadata` 就把整份
        # 「待人工核对」清单写成空（阶段 2 出的成片 metadata 也跟着空，
        # 发布前最该看的那份清单反而没了）。2026-09-17 第 2 集实测踩到。
        notes=list(data.get("notes") or data.get("unresolved") or []),
    )
    missing = [s.index for s in story.all_scenes if s.audio_path is None]
    if missing:
        log_.warning("有 %d 个分镜找不到语音缓存（%s…）", len(missing), missing[:5])
        log_.warning("  音频缓存 key = 文本 + **音色** + **语速**，所以重渲染必须跟当初一致：")
        log_.warning("  当前按 音色=%s 语速=%s 找；找不到就用 --voice / --speed 指定当初的值。",
                     voice, speed)
        log_.warning("  查某期当初用的音色：看 metadata 里的 tts_spec 字段，"
                     "或用 scripts/calib_rate.py 反查音频缓存名。")
    log_.info("从 metadata 载入：《%s》%d 章 / %d 个分镜 / 语音 %.2f 分钟（音色 %s 语速 %s）",
              story.title, len(chapters), len(story.all_scenes), story.duration / 60,
              voice, speed)
    return story, data


def recorded_durations(raw: dict) -> dict[int, float]:
    """取 metadata 里当初记的逐分镜实测秒数（`seconds`）。

    为什么不能只看 `Scene.duration`：那是 ffprobe 音频文件探出来的，
    音频缓存不在（换过音色）就全是 0。而 `seconds` 是成片当初实际用的时长，
    等价于真值 —— 出素材需求清单时必须用它，否则时长会低估。
    """
    out: dict[int, float] = {}
    for c in raw.get("chapters") or []:
        for s in c.get("scenes") or []:
            try:
                v = float(s.get("seconds") or 0)
            except (TypeError, ValueError):
                continue
            if v > 0:
                out[int(s.get("index") or 0)] = v
    return out


def fill_durations_from_metadata(story: Story, raw: dict) -> int:
    """把 metadata 记的实测时长回填到 `Scene.duration`（音频缓存找不到时的补救）。

    返回回填了几个。阶段 2 必须做这一步：EDL 是靠分镜时长铺镜头的，
    时长是 0 的话这场戏就没有画面。
    """
    rec = recorded_durations(raw)
    n = 0
    for s in story.all_scenes:
        if s.duration <= 0 and rec.get(s.index):
            s.duration = rec[s.index]
            n += 1
    return n
