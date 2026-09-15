"""从已有 metadata.json 重新渲染（复用文稿和语音，只重做配图 / 画面 / 编码）。

为什么需要这个：改动配图策略、画面版面、编码参数这类**下游**设置时，
没必要重新花 DeepSeek 和 TTS 的钱重跑全流程。文稿已经在 metadata 里，
语音已经在 data/audio/ 下（文件名带文本哈希，能直接按文字找到），
所以可以原样复用，只把配图和成片重做一遍。

    python scripts/rerender.py                                  # 用最新的 metadata
    python scripts/rerender.py --metadata data/output/xxx_metadata.json
    python scripts/rerender.py -o portrait --no-images           # 只重出竖屏，且不换图

注意：重渲染会覆盖 data/output/ 里同名的成片。想留旧版先把它们挪走。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from hsg import images as images_mod          # noqa: E402
from hsg import tts as tts_mod, video         # noqa: E402
from hsg.config import ensure_dirs, load_config  # noqa: E402
from hsg.models import Chapter, Scene, Story  # noqa: E402
from hsg.pipeline import render_orientation    # noqa: E402


def load_story(meta_path: Path, cfg, audio_dir: Path, log) -> tuple[Story, dict]:
    """返回 (Story, metadata 原始字典)。第二个返回值给调用方看当初的 tts 设定。"""
    data = json.loads(meta_path.read_text(encoding="utf-8"))
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
        period=str(data.get("period") or ""),
        period_start=int(data.get("period_start") or 0),
        period_end=int(data.get("period_end") or 0),
        chapters=chapters,
    )
    missing = [s.index for s in story.all_scenes if s.audio_path is None]
    if missing:
        log.warning("有 %d 个分镜找不到语音缓存（%s…）", len(missing), missing[:5])
        log.warning("  音频缓存 key = 文本 + **音色** + **语速**，所以重渲染必须跟当初一致：")
        log.warning("  当前按 音色=%s 语速=%s 找；找不到就用 --voice / --speed 指定当初的值。",
                    voice, speed)
        log.warning("  查某期当初用的音色：看 metadata 里的 tts 字段，"
                    "或看生成记录 data\\生成记录.md 的「模型」行。")
    log.info("从 metadata 载入：《%s》%d 章 / %d 个分镜 / 语音 %.2f 分钟（音色 %s 语速 %s）",
             story.title, len(chapters), len(story.all_scenes), story.duration / 60, voice, speed)
    return story, data


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--metadata", help="metadata.json 路径（默认取 data/output 下最新的）")
    ap.add_argument("-o", "--orientation", default="both",
                    choices=["both", "portrait", "landscape"])
    ap.add_argument("--no-images", action="store_true", help="沿用已有配图，不重新找图")
    ap.add_argument("--images", action="store_true", help="强制重新找图（默认行为）")
    ap.add_argument("--no-bgm", action="store_true")
    ap.add_argument("--speed", type=float, help="必须与当初生成时一致（音频缓存 key 含语速）")
    ap.add_argument("--voice", help="必须与当初生成时一致（音频缓存 key 含音色）"
                                   "；不改 config 也能重渲染旧期")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)-12s %(message)s",
                        datefmt="%H:%M:%S", stream=sys.stdout)
    for noisy in ("httpx", "httpcore", "PIL"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    log = logging.getLogger("hsg.rerender")

    cfg = load_config()
    ensure_dirs(cfg)
    if args.speed:
        # 音频缓存按「文本+音色+语速」命名，重渲染必须用当初那套设定，否则找不到语音
        cfg.tts[str(cfg.tts.provider)]["speed"] = float(args.speed)
    if args.voice:
        # 换过音色之后，旧期必须用 --voice 指回当初的音色；否则 18/18 全 miss
        cfg.tts[str(cfg.tts.provider)]["voice_id"] = str(args.voice)
    if args.no_bgm:
        cfg.bgm["enabled"] = False

    out_dir = cfg.paths.get_path("output_dir")
    if args.metadata:
        meta = Path(args.metadata)
    else:
        # 默认挑最新的 metadata，但要跳过「只写稿、没出语音」的 plan 产物 ——
        # 那种 metadata 里根本没有语音记录，重渲染必然 0 命中（踩过）。
        cands = sorted(out_dir.glob("*_metadata.json"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
        usable: list[Path] = []
        plan_only: list[Path] = []
        for p in cands:
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                continue
            (plan_only if d.get("plan_only") else usable).append(p)
        meta = usable[0] if usable else None
        if usable and plan_only:
            log.info("自动跳过了 %d 个 plan 产物（只写稿、没有语音记录，无法重渲染）",
                     len(plan_only))
        if not usable:
            log.error("data/output 下找不到可重渲染的 metadata"
                      "（%d 个 plan 产物只有稿件，没有语音）。", len(plan_only))
            log.error("  先跑一期完整流程（run.bat），或用 --metadata 指定具体的 metadata.json")
            return 1
    if not meta or not meta.exists():
        log.error("找不到 metadata.json，先在 data/output/ 下放一个或用 --metadata 指定")
        return 1
    log.info("metadata：%s", meta)

    audio_dir = cfg.paths.get_path("audio_dir")
    story, raw = load_story(meta, cfg, audio_dir, log)
    # 一个分镜都没语音时别往下走：渲染出来是一部只有片头片尾的空片，
    # 看起来「跑成功了」，实际上什么都没做 —— 这种失败最难查。
    if not any(s.audio_path for s in story.all_scenes):
        log.error("一个分镜的语音都没找到 —— 继续渲染只会得到一部空片，已中止。")
        log.error("  音频缓存 key = 文本 + 音色 + 语速，必须跟当初完全一致。")
        log.error("  这期 metadata 里记的设定：%s",
                  raw.get("tts_spec") or raw.get("tts")
                  or "（未记录 —— 2026-09-14 之前的产物都没记，看 生成记录.md 的「模型」行）")
        log.error("  例：python scripts\\rerender.py --voice audiobook_male_1 "
                  "--speed 1.1 -o portrait")
        return 1
    video.check_ffmpeg()

    # ---- 配图
    if not args.no_images:
        used: list[int] = []
        src_log: list[dict] = []
        cache_dir = cfg.paths.get_path("image_dir")
        from concurrent.futures import ThreadPoolExecutor

        scenes = [s for s in story.all_scenes if s.duration > 0]
        period = ((story.period_start, story.period_end)
                  if story.period_start and story.period_end else None)
        # 与 pipeline 一致：先把场景级中文检索词翻成英文，否则 clean 策略下场景词空转
        from hsg.config import ApiKeys as _ApiKeys
        from hsg.llm import LLM as _LLM

        # 先初始化：万一 _LLM 打开失败，后面的 scene_en / scene_kind 也不会 NameError
        scene_en: dict[int, str] = {}
        scene_kind: dict[int, str] = {}
        with _LLM(cfg, _ApiKeys.from_env()) as _llm:
            scene_en = images_mod.translate_scene_queries(
                [(s.index, s.image_query) for s in scenes], cfg, _llm)
            # 与 pipeline 一致：判每个分镜的画面类型，分派「器物」/「场景」风格后缀
            scene_kind = images_mod.classify_scene_kinds(
                [(s.index, s.image_query) for s in scenes], cfg, _llm)

        def _grab(s: Scene) -> None:
            queries = [s.image_query]
            queries_en: list[str] = []
            if scene_en.get(s.index):
                queries_en.append(scene_en[s.index])
            ch = story.chapters[s.chapter_index - 1] if 0 < s.chapter_index <= len(story.chapters) else None
            if ch:
                for q in ch.image_queries:
                    if q and q not in queries:
                        queries.append(q)
                queries_en += [q for q in ch.image_queries_en if q and q not in queries_en]
            p, credit, src, attempts = images_mod.fetch_for_scene(
                s.index, queries, cfg, cache_dir, used,
                queries_en=queries_en, period=period,
                kind=scene_kind.get(s.index, images_mod.KIND_OBJECT))
            if p:
                s.image_path, s.image_credit, s.image_source = p, credit, src
                pick = next((a for a in reversed(attempts) if a.get("picked")), {}) or {}
                s.image_license = str(pick.get("license") or "")
            src_log.append({"scene": s.index, "queries": queries, "queries_en": queries_en,
                            "result": p.name if p else None, "source": src,
                            "license": s.image_license, "credit": credit,
                            "attempts": attempts})

        with ThreadPoolExecutor(max_workers=max(1, int(cfg.images.get("concurrency", 4)))) as pool:
            list(pool.map(_grab, scenes))
        got = sum(1 for s in story.all_scenes if s.image_path)
        clean = sum(1 for s in story.all_scenes if s.image_license)
        images_mod.save_source_log(cache_dir / "_sources.json", src_log, cfg)
        log.info("配图完成：%d/%d 个分镜拿到新图（%d 张版权已标注；策略 %s；图源记录已刷新）",
                 got, len(scenes), clean, cfg.images.get("license_policy"))
        for s in sorted(scenes, key=lambda x: x.index):
            log.info("  分镜 %2d：%s", s.index, (s.image_credit or "⚠️版权未明")[:64])
    else:
        cache_dir = cfg.paths.get_path("image_dir")
        for s in story.all_scenes:
            p = cache_dir / f"scene_{s.index:03d}.jpg"
            if p.exists():
                s.image_path = p
        log.info("沿用已有配图：%d 张", sum(1 for s in story.all_scenes if s.image_path))

    # ---- 渲染
    orients = list(cfg.video.orientations.keys()) if args.orientation == "both" else [args.orientation]
    stamp = ""
    for orient in orients:
        final, cover = render_orientation(story, cfg, orient)
        st = video.probe_streams(final)
        log.info("[%s] 成片：%s（%.1fs = %.2f 分钟，%sx%s，音轨 %s）",
                 orient, final, st.get("duration", 0), st.get("duration", 0) / 60,
                 (st.get("video") or {}).get("width"), (st.get("video") or {}).get("height"),
                 (st.get("audio") or {}).get("codec") or "无")
        if cover:
            log.info("[%s] 封面：%s", orient, cover)
        stamp = final.name
    log.info("完成。文件名沿用日期前缀：%s", stamp)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
