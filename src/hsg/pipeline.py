"""流水线编排：素材 → 大纲 → 写稿 → 校验 → TTS → 配图 → 画面 → 成片。

每一段都可以单独跑（见 cli.py），出问题时能定位到具体阶段。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from datetime import datetime
from pathlib import Path

from concurrent.futures import ThreadPoolExecutor

from . import history as history_mod
from . import images as images_mod
from . import media, tts as tts_mod, verify, video
from .config import ApiKeys, Config, ensure_dirs, get_channel, provider_banner
from .llm import LLM
from .material import fetch_material
from .models import Story
from .outline import build_outline
from .subtitles import build_ass, cues_for
from .writer import rewrite_for_length, write_all

log = logging.getLogger("hsg.pipeline")


# ---------------------------------------------------------------- 工具
def safe_filename(s: str, limit: int = 40) -> str:
    s = re.sub(r"[\\/:*?\"<>|\s]+", "_", s or "").strip("_")
    return (s[:limit] or "story").strip("_")


def timeline_rows(story: Story) -> list[dict]:
    rows: list[dict] = []
    t = 0.0
    for ch in story.chapters:
        for s in ch.scenes:
            rows.append({
                "scene": s.index,
                "chapter": ch.index,
                "heading": ch.heading,
                "chars": s.chars,
                "seconds": round(s.duration, 2),
                "start": round(t, 2),
                "image": s.image_path.name if s.image_path else "(渐变底图)",
                "source": s.image_source,
                "query": s.image_query,
            })
            t += s.duration
    return rows


def print_timeline(rows: list[dict]) -> None:
    log.info("─" * 78)
    log.info("%-5s %-4s %-18s %6s %7s %8s  %s",
             "分镜", "章", "章节", "字数", "秒", "累计", "配图")
    for r in rows:
        log.info("%-5d %-4d %-18s %6d %7.1f %8.1f  %s",
                 r["scene"], r["chapter"], r["heading"][:16], r["chars"],
                 r["seconds"], r["start"], r["image"])
    log.info("─" * 78)


# ---------------------------------------------------------------- 分镜编号
def chapter_feedback(issues: list[dict], scenes_by_idx: dict, chapter_index: int) -> str:
    """挑出**属于某一章**的必修问题，拼成给改写用的反馈串。

    为什么单独抽出来：原先直接用全量 issues 里所有 fail 当反馈，
    会把别章的问题也发给这一章去改（改写方向被带偏，还可能改出无关内容）。
    没有分镜号的条目（scene=0，属章节级结构问题）仍然保留，避免漏掉。
    """
    return "\n".join(
        f"· {i['detail']}" for i in issues
        if i.get("kind") == "fail"
        and (not i.get("scene")
             or (scenes_by_idx.get(i["scene"]) is not None
                 and scenes_by_idx[i["scene"]].chapter_index == chapter_index))
    )


def renumber_scenes(story: Story) -> None:
    """按章节顺序重编全局分镜号，并重设 is_chapter_start。

    为什么必须有这个函数：writer 里的改写（rewrite_for_length）是从
    `_scenes_from(..., start_index=0)` 建新分镜的，也就是**每章都从 0 开始编号**。
    只要有一次改写没有紧接着重编号，就会出现：
      · 两个章节都带 index=0/1/2 的分镜 → 片段目录 seg_000 互相覆盖
      · 音频文件名 scene_000_<hash>.mp3 撞名
      · 时间轴与稿件的分镜号错位
    所以每次改写之后都必须调它，尤其是 fit_length（时长自适应）那条路径。
    """
    story.chapters.sort(key=lambda c: c.index)
    n = 1
    for ch in story.chapters:
        for k, s in enumerate(ch.scenes):
            s.index = n
            n += 1
            s.is_chapter_start = (k == 0)


# ---------------------------------------------------------------- 长度控制
def _pick_chapters_to_fix(story: Story, target_total: float, count: int = 4) -> list:
    have = sum(c.duration for c in story.chapters) or sum(
        s.chars for s in story.all_scenes) / 4.6
    scale = target_total / max(1.0, have)
    ranked = sorted(
        story.chapters,
        key=lambda c: -abs((c.duration or c.text.__len__() / 4.6) - c.seconds * scale),
    )
    return ranked[:count]


def fit_length(
    story: Story,
    cfg: Config,
    llm: LLM,
    tts: tts_mod.TTS,
    audio_dir: Path,
    *,
    measured: bool,
    rounds: int = 2,
    feedback_by_chapter: dict[int, str] | None = None,
) -> float:
    """把总时长拉进 [min, max]。measured=True 表示已经有了真实 TTS 时长。"""
    st = cfg.story
    lo = float(st.min_total_seconds)
    hi = float(st.max_total_seconds)
    aim = float(st.target_total_seconds)

    for rnd in range(1, rounds + 1):
        total = sum(s.duration for s in story.all_scenes) if measured else \
            sum(s.chars for s in story.all_scenes) / max(0.1, float(st.chars_per_second))
        inside = lo <= total <= hi
        log.info("时长检查（第 %d 轮，%s）：%.1fs = %.2f 分钟，目标区间 %.0f-%.0fs%s",
                 rnd, "实测" if measured else "估算", total, total / 60, lo, hi,
                 "  ✓ 合格" if inside else "  ✗ 需要调整")
        if inside:
            return total

        target_total = min(max(aim, lo * 1.03), hi * 0.97)
        picks = _pick_chapters_to_fix(story, target_total, count=len(story.chapters) if rnd == 1 else 3)
        share = target_total / max(1.0, sum(c.seconds for c in story.chapters))
        log.info("调整 %d 个章节（整体缩放系数 %.2f）", len(picks), share)
        for ch in picks:
            fb = (feedback_by_chapter or {}).get(ch.index, "")
            rewrite_for_length(ch, story, cfg, llm, ch.seconds * share, feedback=fb)
        # ★ 改写出来的分镜是「每章从 0 开始」编号的，必须在合成之前重编全局序号 ——
        #   否则 audio_tag 的文件名会撞成 scene_000_*，后面片段目录也会互相覆盖。
        renumber_scenes(story)
        # 只重合成被改写的章节
        changed = {id(s) for ch in picks for s in ch.scenes}
        new_scenes = [s for s in story.all_scenes if id(s) in changed]
        # 必须清掉旧音频缓存，否则 synthesize_all 会直接复用
        for s in new_scenes:
            if s.audio_path and s.audio_path.exists():
                s.audio_path.unlink(missing_ok=True)
            s.audio_path = None
        tts_mod.synthesize_all(new_scenes, cfg, audio_dir, tts)
        measured = True

    total = sum(s.duration for s in story.all_scenes)
    if not (lo <= total <= hi):
        log.warning("时长仍超出区间：%.1fs（%.2f 分钟）。已尽力调整，"
                    "可调 config.yaml 的 story.chapters / seconds_per_chapter 后重跑。",
                    total, total / 60)
    return total


# ---------------------------------------------------------------- 主流程
def run(
    cfg: Config,
    topic: str,
    *,
    keys: ApiKeys | None = None,
    do_images: bool = True,
    do_video: bool = True,
    orientations: list[str] | None = None,
    force: bool = False,
    allow_duplicate: bool = False,
) -> dict:
    started = time.time()
    ensure_dirs(cfg)
    keys = keys or ApiKeys.from_env()
    log.info("═" * 78)
    log.info("历史小故事生成 · 主题：%s", topic)
    log.info(provider_banner(cfg))
    log.info("API Key：%s", keys.describe())

    # ---- 查重：同一个故事不重复做
    if bool(cfg.runtime.get("record_history", True)):
        dup = history_mod.find_duplicate(cfg, topic)
        if dup:
            msg = (f"这个题材已经做过了：{dup.get('title')}"
                   f"（{str(dup.get('generated_at'))[:10]}，"
                   f"{float(dup.get('speech_minutes') or 0):.1f} 分钟）\n"
                   f"  · 想换一个：直接 run（不指定 --topic），选题会自动跳过做过的\n"
                   f"  · 确实要重做：加 --allow-duplicate")
            if not allow_duplicate:
                raise RuntimeError(msg)
            log.warning("题材重复，但指定了 --allow-duplicate，继续。%s", msg)
    log.info("═" * 78)

    v = cfg.video
    paths = cfg.paths
    if orientations is None:
        orientations = list(v.orientations.keys())

    # ---------- 1. 素材
    material, mat_meta = fetch_material(topic, [], cfg, paths.get_path("material_dir"))

    # ---------- 2. 大纲
    stats = {"rule_fail": 0, "rule_warn": 0, "audit_issues": 0, "regenerated_chapters": []}
    regenerated: set[int] = set()
    with LLM(cfg, keys) as llm:
        story = build_outline(topic, cfg, llm, material)

        # ---------- 2.5 锚点核查（写稿前把编造的出处挡掉）
        stats["fact_audit"] = verify.audit_facts(story, cfg, llm)

        # ---------- 3. 写稿
        story = write_all(story, cfg, llm)

        # ---------- 4. 校验（规则层）+ 按需重写
        issues = verify.rule_check(story, cfg)
        stats["rule_fail"], stats["rule_warn"] = verify.report(issues)
        bad_chapters = sorted({i["scene"] for i in issues
                               if i["kind"] == "fail" and i["scene"]} )
        if bad_chapters:
            scenes_by_idx = {s.index: s for s in story.all_scenes}
            ch_ids = sorted({scenes_by_idx[i].chapter_index for i in bad_chapters
                             if i in scenes_by_idx})
            log.info("命中问题的章节：%s，将带问题重写", ch_ids)
            regenerated.update(ch_ids)
            for ch in story.chapters:
                if ch.index in ch_ids:
                    # 只把**本章**的问题当反馈（原先用全量 issues，会把别章的问题
                    # 也发过去，改写方向被带偏）
                    fb = chapter_feedback(issues, scenes_by_idx, ch.index)
                    rewrite_for_length(ch, story, cfg, llm, ch.seconds,
                                       feedback=f"【上一轮被指出的问题，务必避免】\n{fb}\n")
            for ch in story.chapters:
                for s in ch.scenes:
                    s.audio_path = None
            renumber_scenes(story)

        # ---------- 5. 史实审校（LLM 层）
        audit = verify.llm_audit(story, cfg, llm)
        stats["audit_issues"] = len(audit)
        if audit:
            verify.report(audit)
            scenes_by_idx = {s.index: s for s in story.all_scenes}
            ch_ids = sorted({scenes_by_idx[i["scene"]].chapter_index
                             for i in audit if i["scene"] in scenes_by_idx})
            for ch in story.chapters:
                if ch.index in ch_ids:
                    fb = "\n".join(f"· 分镜 {i['scene']}：{i['detail']}" for i in audit
                                   if scenes_by_idx.get(i["scene"], None)
                                   and scenes_by_idx[i["scene"]].chapter_index == ch.index)
                    regenerated.add(ch.index)
                    rewrite_for_length(ch, story, cfg, llm, ch.seconds,
                                       feedback=f"【史实校对员指出的问题，务必改正】\n{fb}\n")
            renumber_scenes(story)
        else:
            log.info("史实审校：未发现需要修改的问题")

        # ---------- 5.5 复检：改写之后必须再验一遍（这是「有问题要有校验」的落点）
        recheck = verify.final_check(story, cfg, llm)
        stats["recheck_fail"] = len(recheck["fails"])
        stats["recheck_warn"] = len(recheck["warns"])
        if recheck["fails"]:
            verify.report(recheck["fails"])
            story.notes = [f"分镜 {i['scene'] or '-'}：{i['detail']}" for i in recheck["fails"]]
            log.warning("复检仍有 %d 条必须处理的问题（已写进稿件的「待人工核对」清单）",
                        len(recheck["fails"]))
            if bool(cfg.verify.get("block_on_fail", False)):
                raise verify.VerifyFailed(
                    f"复检仍有 {len(recheck['fails'])} 条史实问题未解决，"
                    f"按 verify.block_on_fail=true 中止出片。\n"
                    f"  · 看清单：{story.notes[:3]}\n"
                    f"  · 想放宽：把 config.yaml 的 verify.block_on_fail 改成 false"
                )
        else:
            log.info("复检通过：改写后没有再发现必须处理的问题（提示 %d 条）",
                     len(recheck["warns"]))

    # ---------- 6. 写稿字符量 → 先做一次「零成本」长度预估
    st = cfg.story
    est = sum(s.chars for s in story.all_scenes) / max(0.1, float(st.chars_per_second))
    log.info("预估语音时长 %.1fs（%.2f 分钟），区间 %.0f-%.0fs",
             est, est / 60, float(st.min_total_seconds), float(st.max_total_seconds))

    # ---------- 7. TTS
    audio_dir = paths.get_path("audio_dir")
    with tts_mod.TTS(cfg, keys) as tts:
        tts_mod.synthesize_all(story.all_scenes, cfg, audio_dir, tts)
        total = sum(s.duration for s in story.all_scenes)
        if total <= 0:
            # 一段都没合成出来就别往下走了：后面的「按时长自适应改写」此时毫无意义，
            # 而且会把真正的原因（余额不足/限流/没装兜底）埋在一堆日志里
            raise tts_mod.TTSFailed(
                f"全部 {len(story.all_scenes)} 段语音合成失败，已中止。\n"
                f"  常见原因：\n"
                f"   · MiniMax 余额不足（错误码 1008 insufficient balance）\n"
                f"   · 触发限流（1002 rate limit exceeded）—— 隔一会儿重跑，或把\n"
                f"     tts.concurrency 调小\n"
                f"   · 兜底语音没装：pip install edge-tts\n"
                f"  往上翻第一条 [hsg.tts] ERROR 就能看到具体是哪一种。"
            )

        # ---------- 8. 实测时长 → 拉进区间（在区间内会直接返回）
        total = fit_length(story, cfg, llm, tts, audio_dir, measured=True)

    ok = sum(1 for s in story.all_scenes if s.duration > 0)
    log.info("语音总时长 %.1fs = %.2f 分钟，%d/%d 段成功",
             total, total / 60, ok, len(story.all_scenes))
    if ok < len(story.all_scenes):
        log.warning("有 %d 段语音失败，这些分镜会被跳过（成片会短一点）",
                    len(story.all_scenes) - ok)

    # ---------- 9. 配图
    src_log: list[dict] = []
    if do_images:
        used: list[int] = []
        workers = max(1, int(cfg.images.get("concurrency", 4)))
        cache_dir = paths.get_path("image_dir")
        scenes = [s for s in story.all_scenes if s.duration > 0]
        period = ((story.period_start, story.period_end)
                  if story.period_start and story.period_end else None)

        def _grab(s) -> None:
            queries = [s.image_query]
            queries_en: list[str] = []
            ch = story.chapters[s.chapter_index - 1] if 0 < s.chapter_index <= len(story.chapters) else None
            if ch:
                for q in ch.image_queries:
                    if q and q not in queries:
                        queries.append(q)
                queries_en = [q for q in ch.image_queries_en if q]
            p, credit, src, attempts = images_mod.fetch_for_scene(
                s.index, queries, cfg, cache_dir, used,
                queries_en=queries_en, period=period)
            s.image_path, s.image_credit, s.image_source = p, credit, src
            pick = next((a for a in reversed(attempts) if a.get("picked")), {}) or {}
            s.image_license = str(pick.get("license") or "")
            src_log.append({"scene": s.index, "queries": queries, "queries_en": queries_en,
                            "result": p.name if p else None, "source": src,
                            "license": s.image_license, "credit": credit,
                            "attempts": attempts})

        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(_grab, scenes))
        images_mod.save_source_log(cache_dir / "_sources.json", src_log, cfg)
        got = sum(1 for s in story.all_scenes if s.image_path)
        clean = sum(1 for s in story.all_scenes if s.image_license)
        log.info("配图完成：%d/%d 个分镜拿到图（其中 %d 张版权已标注；策略 %s）",
                 got, len(scenes), clean, cfg.images.get("license_policy"))
        if got and clean < got:
            log.warning("有 %d 张图的版权状态不明 —— 要公开发布请把 "
                        "images.license_policy 设为 clean 并确认图源可用",
                        got - clean)

    rows = timeline_rows(story)
    if bool(cfg.runtime.get("print_timeline", True)):
        print_timeline(rows)

    # ---------- 10. 成片
    outputs: list[str] = []
    covers: list[str] = []
    video_seconds: dict[str, float] = {}
    if do_video:
        for orient in orientations:
            out, cover = render_orientation(story, cfg, orient)
            outputs.append(str(out))
            if cover:
                covers.append(str(cover))
            video_seconds[orient] = round(video.media_duration(out), 2)

    # ---------- 11. 产物
    stamp = datetime.now().strftime("%Y%m%d")
    out_dir = paths.get_path("output_dir")
    stem = f"{stamp}_{safe_filename(story.title)}"
    stats["regenerated_chapters"] = sorted(regenerated)
    stats["images_found"] = sum(1 for s in story.all_scenes if s.image_path)
    stats["images_total"] = len(story.all_scenes)
    # 把「这期是用什么音色/语速生成的」写进 metadata。
    # 为什么必须有：音频缓存 key 含音色与语速，rerender 旧期时必须知道当初的设置，
    # 否则 0 命中、渲染出空片（实测踩过 —— 换音色后旧期全废，而 metadata 里查不到依据）。
    tts_sub = cfg.tts[str(cfg.tts.provider)]
    tts_spec = {
        "provider": str(cfg.tts.provider),
        "voice_id": str(tts_sub.get("voice_id") or ""),
        "speed": tts_sub.get("speed"),
        "chars_per_second": float(cfg.story.get("chars_per_second") or 0),
    }
    llm_sub = cfg.llm[str(cfg.llm.provider)]
    script_md = write_script(story, cfg, out_dir / f"{stem}_脚本.md", total)
    meta_json = write_metadata(story, cfg, out_dir / f"{stem}_metadata.json", rows, {
        "topic": topic, "material": mat_meta, "total_seconds": total,
        "ok_scenes": ok, "outputs": outputs, "video_seconds": video_seconds,
        "verify": stats, "elapsed_seconds": round(time.time() - started, 1),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "tts_spec": tts_spec,
        "text_model": f"{cfg.llm.provider}/{llm_sub.get('model')}",
        "covers": covers,
    })

    # ---------- 12. 生成记录（用于查重 + 留档）
    if bool(cfg.runtime.get("record_history", True)):
        try:
            rec = history_mod.build_record(
                story, cfg,
                total_seconds=total, video_seconds=video_seconds, outputs=outputs,
                script=str(script_md), metadata=str(meta_json),
                material_meta=mat_meta, verify_stats=stats,
                elapsed=time.time() - started,
            )
            history_mod.append_record(cfg, rec)
        except Exception as exc:  # noqa: BLE001
            log.warning("生成记录写入失败（不影响成片）：%s", exc)

    log.info("═" * 78)
    log.info("完成：%.2f 分钟 / %d 章 / %d 个分镜", total / 60, len(story.chapters), len(story.all_scenes))
    for o in outputs:
        log.info("成片：%s", o)
    for c in covers:
        log.info("封面：%s", c)
    log.info("稿件：%s", script_md)
    log.info("元数据：%s", meta_json)
    return {"story": story, "total_seconds": total, "outputs": outputs,
            "covers": covers,
            "script": str(script_md), "metadata": str(meta_json),
            "video_seconds": video_seconds, "verify": stats,
            "elapsed_seconds": round(time.time() - started, 1)}


# ---------------------------------------------------------------- 渲染
def render_orientation(story: Story, cfg: Config, orient: str) -> tuple[Path, Path | None]:
    """渲染一个朝向：返回 (成片路径, 封面路径或 None)。"""
    v = cfg.video
    size = (int(v.orientations[orient].width), int(v.orientations[orient].height))
    seg_root = cfg.paths.get_path("segment_dir") / orient
    slide_root = cfg.paths.get_path("slide_dir") / orient
    seg_root.mkdir(parents=True, exist_ok=True)
    slide_root.mkdir(parents=True, exist_ok=True)
    tail = float(v.get("tail_padding", 0.55))
    fade = float(v.get("transition_seconds", 0.4)) if str(v.get("transition", "fade")) == "fade" else 0.0
    sc_enabled = bool(cfg.subtitle.get("enabled", True))
    channel = get_channel(cfg)

    scenes = [s for s in story.all_scenes if s.duration > 0]
    names: list[str] = []

    # ---- 片头
    if bool(v.get("intro", True)):
        intro_text = f"{channel}。本期为您讲述《{story.title}》。{story.hook}"
        p, dur = _tts_cached(cfg, seg_root, "intro", intro_text) if bool(
            cfg.story.get("intro_speak", True)) else (None, 0.0)
        dur = dur + float(cfg.story.get("intro_seconds_pad", 1.2)) if dur else 3.5
        bg, fg = media.build_text_card(
            slide_root / "intro_bg.jpg", slide_root / "intro_fg.png", size, cfg,
            lines=[channel], slogan=str(v.get("intro_slogan") or ""),
            subtitle=story.title + (f" · {story.period}" if story.period else ""),
        )
        cues = [story.title] if sc_enabled and story.title else []
        ass = build_ass(cues, dur, slide_root / "intro.ass", size, cfg) if sc_enabled else None
        names.append(_encode(seg_root, "intro", bg, fg, p, ass, size, dur, cfg,
                             fade_in=fade, fade_out=fade, mode=0))

    # ---- 正文分镜
    for s in scenes:
        ch = story.chapters[s.chapter_index - 1] if 0 < s.chapter_index <= len(story.chapters) else None
        kicker = f"{channel} · 第{ch.index}章 {ch.heading}" if ch else channel
        title = ch.heading if (s.is_chapter_start and ch) else ""
        is_first = s is scenes[0]
        is_last = s is scenes[-1]
        dur = s.duration + tail
        bg, fg = media.build_layers(
            slide_root / f"scene_{s.index:03d}_bg.jpg",
            slide_root / f"scene_{s.index:03d}_fg.png",
            size, cfg, image_path=s.image_path, kicker=kicker, title=title,
            caption=s.caption, credit=s.image_credit,
        )
        cues = cues_for(s, cfg) if sc_enabled else []
        ass = build_ass(cues, dur, slide_root / f"scene_{s.index:03d}.ass", size, cfg) if sc_enabled else None
        names.append(_encode(
            seg_root, f"seg_{s.index:03d}", bg, fg, s.audio_path, ass, size, dur, cfg,
            fade_in=fade if is_first else 0.0,
            fade_out=fade if is_last else 0.0,
            mode=s.index,
        ))

    # ---- 片尾
    if bool(v.get("outro", True)):
        outro_dur = float(cfg.story.get("outro_seconds", 4.0))
        bg, fg = media.build_text_card(
            slide_root / "outro_bg.jpg", slide_root / "outro_fg.png", size, cfg,
            lines=[str(cfg.story.get("outro_title") or "本期故事讲完了")],
            slogan=str(v.get("intro_slogan") or ""), subtitle=story.title,
        )
        # 片尾没有口播，但必须补一条等长静音轨，否则成片音轨比视频短
        silence = video.make_silence(seg_root / "outro_silence.m4a", outro_dur, cfg)
        names.append(_encode(seg_root, "outro", bg, fg, silence, None, size, outro_dur, cfg,
                             fade_in=fade, fade_out=fade, mode=2))

    master = video.concat_segments(seg_root, names, f"_concat_{orient}.mp4")
    total = video.media_duration(master)
    info = video.probe_streams(master)
    log.info("[%s] 拼接完成：%s（%.1fs，%sx%s @%s，音轨 %s）",
             orient, master.name, total,
             (info.get("video") or {}).get("width"), (info.get("video") or {}).get("height"),
             (info.get("video") or {}).get("fps"),
             (info.get("audio") or {}).get("codec") or "无")

    # ---- BGM（可选）
    final_src = master
    if bool(cfg.bgm.get("enabled", False)):
        bgm = Path(str(cfg.bgm.get("file", "")))
        # 相对路径按项目根目录解析（config 里的 data/bgm/xxx.mp3 是这么写的）
        bgm = bgm if bgm.is_absolute() else (cfg.paths.get_path("data_dir").parent / bgm)
        if bgm.exists():
            start_at = 0.0
            mode = str(cfg.bgm.get("start_mode", "after_intro"))
            if mode == "after_intro":
                first = names[0] if names else ""
                first_dur = video.media_duration(seg_root / first) if first else 0.0
                start_at = first_dur + float(cfg.bgm.get("start_padding", 0.4))
            elif mode == "custom":
                start_at = float(cfg.bgm.get("start_at", 0.0))
            final_src = video.mix_bgm(seg_root, master.name, bgm, f"_mixed_{orient}.mp4",
                                      cfg, total, start_at=start_at)
        else:
            log.warning("BGM 已启用但找不到文件：%s（跳过）", bgm)

    stamp = datetime.now().strftime("%Y%m%d")
    out_dir = cfg.paths.get_path("output_dir")
    out_dir.mkdir(parents=True, exist_ok=True)
    label = {"portrait": "竖屏", "landscape": "横屏"}.get(orient, orient)
    final = out_dir / f"{stamp}_{safe_filename(story.title)}_{label}.mp4"
    if final.resolve() != final_src.resolve():
        import shutil
        shutil.copyfile(final_src, final)

    # ---- 封面（发布用缩略图）。默认开；要关：video.cover: false
    cover: Path | None = None
    if bool(v.get("cover", True)):
        cover = media.build_cover(
            out_dir / f"{stamp}_{safe_filename(story.title)}_{label}_封面.jpg",
            size, cfg,
            title=story.title,
            kicker=channel,
            subtitle=story.angle_question or story.period,
            foot=str(v.get("intro_slogan") or ""),
            image_path=next((s.image_path for s in scenes if s.image_path), None),
        )
    return final, cover


def _tts_cached(cfg: Config, workdir: Path, name: str, text: str):
    """片头/片尾这种一次性语音（缓存 key 同样带文本哈希 + 音色 + 语速）。"""
    audio_dir = cfg.paths.get_path("audio_dir")
    sub = cfg.tts[str(cfg.tts.provider)]
    tag = tts_mod.audio_tag(text, str(sub.get("voice_id") or ""), sub.get("speed"))
    out = audio_dir / f"{name}_{tag}.mp3"
    if out.exists() and out.stat().st_size > 2048:
        return out, tts_mod.probe_duration(out)
    with tts_mod.TTS(cfg) as t:
        return t.synthesize(text, out)


def _encode(seg_root: Path, stem: str, bg: Path, fg: Path, audio: Path | None,
            ass: Path | None, size, dur: float, cfg: Config,
            *, fade_in: float, fade_out: float, mode: int) -> str:
    """把素材拷进片段目录、用纯文件名调 ffmpeg（规避 Windows 路径转义）。"""
    seg_dir = seg_root / stem
    seg_dir.mkdir(parents=True, exist_ok=True)
    import shutil

    def _cp(src: Path, name: str) -> str:
        dst = seg_dir / name
        if src.resolve() != dst.resolve():
            shutil.copyfile(src, dst)
        return name

    bg_name = _cp(bg, "bg.jpg")
    fg_name = _cp(fg, "fg.png")
    a_name = _cp(audio, "a.mp3") if audio else None
    ass_name = _cp(ass, "s.ass") if ass else None
    out_name = f"{stem}.mp4"
    video.encode_segment(seg_dir, bg_name, fg_name, a_name, ass_name, out_name,
                         size, dur, cfg, fade_in=fade_in, fade_out=fade_out,
                         motion_mode=mode)
    return f"{stem}/{out_name}"


# ---------------------------------------------------------------- 产物
def write_script(story: Story, cfg: Config, path: Path, total: float) -> Path:
    lines = [
        f"# {story.title}",
        "",
        f"- 主题：{story.topic}",
        f"- 本期问题：{story.angle_question or '（事件型，无提问）'}",
        f"- 年代：{story.period}（{story.period_start} ~ {story.period_end}）",
        f"- 语音总时长：{total:.1f} 秒（{total / 60:.2f} 分钟）",
        f"- 字数：{sum(s.chars for s in story.all_scenes)} 字",
        f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "",
    ]
    if story.notes:
        lines += [
            "## ⚠️ 待人工核对（自动校验未通过项）",
            "",
            "以下问题在自动改写后仍未解决。要公开发布请先人工确认或改成史实无误的表述；",
            "自己看则可以先听，但心里有数。",
            "",
        ]
        lines += [f"- {n}" for n in story.notes]
        lines += [""]
    lines += [
        "## 开篇",
        "",
        story.hook,
        "",
    ]
    for ch in story.chapters:
        lines += [f"## 第 {ch.index} 章　{ch.heading}", "", f"> 本章要点：{ch.summary}", ""]
        if ch.facts:
            lines += ["> 史实锚点（写稿只能用这些）：", ""]
            lines += [f"> · {f}" for f in ch.facts] + [""]
        for s in ch.scenes:
            lines += [f"**[分镜 {s.index}]** {s.text}", "",
                      f"　配图检索词：`{s.image_query}`　时长 {s.duration:.1f}s", ""]
    lines += ["## 配图出处与版权（仅在本地留档，不进画面）", ""]
    for s in story.all_scenes:
        if s.image_path:
            # credit 里已含许可证前缀，不用再单独打一遍
            lines.append(f"- 分镜 {s.index}：{s.image_path.name}　"
                         f"{s.image_credit or '⚠️版权未明（关键词检索，发布有风险）'}")
    if any(s.image_path and not s.image_license for s in story.all_scenes):
        lines += ["",
                  "> 标了「⚠️版权未明」的图来自关键词检索，公开发布有被追偿的风险。",
                  "> 要彻底规避：`images.license_policy: clean`（只用 CC0/公共领域/免费商用图库）。"]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def write_metadata(story: Story, cfg: Config, path: Path, rows: list[dict], extra: dict) -> Path:
    data = {
        "title": story.title,
        "topic": story.topic,
        "angle_question": story.angle_question,
        "angle_mode": str(cfg.story.get("angle_mode") or "small"),
        "period": story.period,
        "period_start": story.period_start,
        "period_end": story.period_end,
        "hook": story.hook,
        "chapters": [
            {"index": c.index, "heading": c.heading, "summary": c.summary,
             "seconds_target": c.seconds, "seconds_actual": round(c.duration, 2),
             "facts": list(c.facts),
             "image_queries": list(c.image_queries),
             "scenes": [{"index": s.index, "text": s.text, "image_query": s.image_query,
                         "caption": s.caption,
                         "image": s.image_path.name if s.image_path else None,
                         "image_source": s.image_source, "image_credit": s.image_credit,
                         "image_license": s.image_license,
                         "seconds": round(s.duration, 2)} for s in c.scenes]}
            for c in story.chapters
        ],
        "timeline": rows,
        "unresolved": list(story.notes),
        "license_policy": str(cfg.images.get("license_policy") or "clean"),
        **extra,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
