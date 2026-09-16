"""Agent 编排：把生产线做成「阶段闸门 + AI 决策 + 自检」的自主流程。

为什么要有这一层（而不是一条命令跑到底）：

  · **必须停的地方要停**：整条流程里有一步机器干不了 —— 人工剪影视素材。
    `--stage 1` 出脚本和素材需求清单后**主动停下**，把「你要剪什么」说清楚；
    素材到齐后 `--stage 2` 自动接手。这就是「闸门」。
  · **该 AI 决策的才交给 AI**：镜头要什么画面、剪辑用什么手法、哪里打大字 → LLM。
    编码/拼接/混音/合规校验这些确定性的活全是代码，不让 AI 去猜。
    反过来也一样：不让代码去猜创作意图（那是上一轮踩过的坑：规则兜底把
    所有分镜都标成「必须」，等于没有优先级）。
  · **干完活必须交证据**：每一步都有可验证的自检（时长对不对、有没有音轨、
    切片占比超没超、文件在不在），最后出一份「自检报告 + 素材出处清单」。
    机器不许只说一句「完成了」。

三个阶段：

    阶段 1  脚本 → 分镜 → 素材需求清单              ← 跑完停住等人
    ── 人工：按清单剪素材 → import_clip.py 绑槽位 ──
    阶段 2  AI 导演排镜头(EDL) → 渲染 → 自检报告     ← 全自动

阶段 2 有一条**故意不自动化**的东西：不重写字稿。
脚本在阶段 1 已经出给你看过、你也照着它剪了素材，所以阶段 2 只做配音、
配图、剪辑、渲染 —— 绝不偷偷改稿。时长跑出区间只告警，不自动改写。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

from . import clips as clips_mod
from . import edl as edl_mod
from . import needs as needs_mod
from . import pipeline, storyio, video
from .config import ApiKeys, Config, ensure_dirs, provider_banner

log = logging.getLogger("hsg.agent")


# ---------------------------------------------------------------- 找文件（阶段判据）
def find_plan_metadata(cfg: Config) -> Path | None:
    """找最近一次「只写稿」的产物。

    glob 写成 `*plan*_metadata.json` 是有意的：plan 产物的命名历史上变过两次
    （`<日期>_plan_metadata.json` → `<日期>_plan_<标题>_metadata.json`），
    放开才能同时认出来；文件名里带标题是为了**同一天跑第二次不覆盖第一次**。
    """
    out = cfg.paths.get_path("output_dir")
    cands = sorted(out.glob("*plan*_metadata.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    return cands[0] if cands else None


def plan_candidates(cfg: Config) -> list[Path]:
    """所有「待出片」的脚本产物（新→旧）。多份同时在的时候，人必须知道有几份。"""
    out = cfg.paths.get_path("output_dir")
    return sorted(out.glob("*plan*_metadata.json"),
                  key=lambda p: p.stat().st_mtime, reverse=True)


def meta_brief(path) -> str:
    """从 metadata 里读出一句话题材 —— 日志里只报文件名，人看不出是哪一期。"""
    try:
        d = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:            # noqa: BLE001 —— 读不出来不该让流程挂掉
        return ""
    topic = str(d.get("topic") or d.get("title") or "")
    ser, ep = str(d.get("series") or ""), int(d.get("series_ep") or 0)
    return f"《{ser}》第{ep}集 {topic}" if ser else topic


def pending_note(cfg: Config, chosen: Path | None) -> str:
    """data/output 下有多份脚本副本时的提醒。

    为什么必须有：`--stage 2` 默认挑**最新**的那份，中间只要跑过别的期
    （或者用过 rerender），就会**静默渲染错的那一期** —— 出来的片看着正常、
    内容却不是你这次做的那集。多份在场时把清单摊开、把「怎么指定」写出来。
    """
    cands = plan_candidates(cfg)
    if len(cands) <= 1:
        return ""
    lines = [f"⚠ data/output 下有 {len(cands)} 份脚本，默认用最新那份："
             f"{chosen.name if chosen else '-'}",
             "  要指定就加 --metadata："]
    for p in cands[:8]:
        mark = "← 默认" if chosen is not None and p == chosen else ""
        lines.append(f"    --metadata \"{p}\"　{meta_brief(p)} {mark}")
    return "\n".join(lines)


def pick_plan(cfg: Config, explicit: str | Path | None = None) -> Path | None:
    """`--stage 2` 该用哪份脚本。

    优先级：显式 `--metadata` > **最新的 plan 产物** > 兜底「最新的 metadata」。

    为什么默认只认 plan 产物：阶段 2 的输入永远是阶段 1 出给你的那份稿。
    踩过的坑 —— 原来默认 `find_latest_metadata`，它 glob 所有 `*_metadata.json`：
    中间只要跑过别的期（或 rerender 写过新的 metadata），阶段 2 就**静默渲染错的那一期**，
    出来的片子看着一切正常、内容却是另一集。
    """
    if explicit:
        return Path(explicit)
    return find_plan_metadata(cfg) or find_latest_metadata(cfg)


def find_latest_metadata(cfg: Config) -> Path | None:
    out = cfg.paths.get_path("output_dir")
    cands = sorted(out.glob("*_metadata.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    return cands[0] if cands else None


def find_latest_needs(cfg: Config) -> Path | None:
    d = cfg.paths.get_path("data_dir") / "needs"
    cands = sorted(d.glob("*_素材需求.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    return cands[0] if cands else None


def find_latest_edl(cfg: Config) -> Path | None:
    d = cfg.paths.get_path("data_dir") / "edl"
    cands = sorted(d.glob("*_EDL.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    return cands[0] if cands else None


def _load_needs(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _done_videos(cfg: Config, title: str) -> list[Path]:
    if not title:
        return []
    out = cfg.paths.get_path("output_dir")
    safe = pipeline.safe_filename(title)
    return sorted(out.glob(f"*{safe}_*.mp4"))


# ---------------------------------------------------------------- 阶段判断
def status(cfg: Config) -> int:
    """看现在卡在哪、下一步该敲什么命令（这是「人机交接点」的唯一入口）。"""
    ensure_dirs(cfg)
    meta = find_latest_metadata(cfg)
    needs_path = find_latest_needs(cfg)
    edl_path = find_latest_edl(cfg)
    index = clips_mod.load_index(clips_mod.index_path(cfg))
    n_clips = len(index.get("clips") or [])

    print("=" * 66)
    print("历史解说视频 · 生产线状态")
    print("=" * 66)
    print(f"文本模型：{provider_banner(cfg).splitlines()[0] if provider_banner(cfg) else '-'}")
    print(f"素材库：{n_clips} 条素材")
    print()

    if meta is None:
        print("▶ 阶段 0：还没有脚本。")
        print('  下一步：run.bat agent --stage 1 -t "你的题材"')
        return 0
    cands = plan_candidates(cfg)
    if len(cands) > 1:
        print(f"脚本：{len(cands)} 份（--stage 2 默认用最新那份）")
        for i, p in enumerate(cands[:8]):
            mark = "← 最新，就是它" if p == meta else ""
            print(f"  {'▶' if i == 0 else ' '} {p.name}　{meta_brief(p)} {mark}")
    else:
        print(f"✓ 脚本：{meta.name}　{meta_brief(meta)}")

    if needs_path is None:
        print("▶ 阶段 1 没走完：还没有素材需求清单。")
        print("  下一步：python scripts\\make_needs.py")
        return 0
    need_obj = _load_needs(needs_path)
    cov = needs_mod.coverage(need_obj, index)
    print(f"✓ 需求清单：{needs_path.name}")
    print(f"  槽位 {cov['slots_total']} 个，需素材 {cov['need_seconds']:.0f} 秒"
          f"（真要剪的约 {cov['fresh_seconds']:.0f} 秒）")

    done = _done_videos(cfg, str(need_obj.get("title") or ""))
    if done and (edl_path is None or edl_path.stat().st_mtime < done[-1].stat().st_mtime):
        print()
        print("✓ 阶段 2 已完成，成片：")
        for p in done:
            print(f"  {p}")
        return 0

    print(f"  覆盖度：已绑定 {len(cov['bound'])}　有候选 {len(cov['suggested'])}　"
          f"还缺 {len(cov['missing'])}（关键位置 {len(cov['missing_must'])}）")
    print()
    if cov["missing_must"]:
        print("▶ 阶段 1.5：等你去剪素材（这是唯一必须人工的环节）")
        print("  照这份清单剪：")
        print(f"    {needs_path.with_suffix('').with_name(needs_path.stem.replace('_素材需求', '') + '_素材需求.md')}")
        print("  先剪关键位置这几条：")
        for slot in cov["missing_must"][:6]:
            row = next((r for r in need_obj["slots"] if r["slot"] == slot), {})
            print(f"    `{slot}`  {float(row.get('dur') or 0):.1f}s  {row.get('need') or ''}")
        print()
        print("  剪好后导入（一个片段可绑同场多个槽位）：")
        print("    python scripts\\import_clip.py --file <你的.mp4> --id <id> \\")
        print('        --title "片名" --year 1994 --people 曹操 --era 东汉末 \\')
        print(f"        --slots {cov['missing_must'][0]}")
        print()
        print("  剪完再看一次状态：run.bat agent")
        return 0

    print("▶ 阶段 2：素材够了，可以自动出片。")
    if n_clips == 0:
        print("  ⚠ 素材库是空的：这一期会全部走回退画面（AI 生成图 / 图库图 + 缓移），"
              "不会有影视切片。")
    if edl_path:
        print(f"  已有剪辑表（会重新生成）：{edl_path.name}")
    print("  下一步：run.bat agent --stage 2")
    print("          （或直接跑 run.bat agent --stage 2 --metadata <某期 metadata.json>）")
    return 0


# ---------------------------------------------------------------- 阶段 1
def stage1(cfg: Config, args) -> int:
    """脚本 → 分镜 → 素材需求清单，然后停下等素材。"""
    from .cli import cmd_plan

    ensure_dirs(cfg)
    log.info("═" * 70)
    log.info("阶段 1：出脚本 + 分镜 + 素材需求清单")
    log.info("═" * 70)
    rc = cmd_plan(cfg, args)
    if rc != 0:
        return rc

    meta = find_plan_metadata(cfg)
    if meta is None:
        log.error("阶段 1 没产出 metadata，后面没法继续")
        return 1

    keys = ApiKeys.from_env()
    story, raw = storyio.load_story(meta, cfg, cfg.paths.get_path("audio_dir"), log)
    recorded = storyio.recorded_durations(raw)
    index = clips_mod.load_index(clips_mod.index_path(cfg))

    from .llm import LLM
    if keys.deepseek:
        with LLM(cfg, keys) as llm:
            need_obj = needs_mod.build_needs(story, cfg, llm, recorded=recorded)
    else:
        log.warning("没有 DeepSeek key → 需求清单只有规则兜底（缺人物与标注词建议）")
        need_obj = needs_mod.build_needs(story, cfg, None, recorded=recorded)

    md, js = needs_mod.save(need_obj, cfg, index)
    cov = needs_mod.coverage(need_obj, index)
    log.info("═" * 70)
    log.info("阶段 1 完成，现在**停下**等你剪素材。")
    log.info("  素材需求清单：%s", md)
    log.info("  槽位 %d 个，铺满画面需 %.0f 秒；真要剪的约 %.0f 秒",
             cov["slots_total"], cov["need_seconds"], cov["fresh_seconds"])
    log.info("  关键位置 %d 条（先剪这些）", len(cov["missing_must"]))
    for slot in cov["missing_must"]:
        row = next((r for r in need_obj["slots"] if r["slot"] == slot), {})
        log.info("    %s  %.1fs  %s", slot, float(row.get("dur") or 0), row.get("need") or "")
    log.info("  剪好导入后再跑：run.bat agent --stage 2")
    log.info("═" * 70)
    return 0


# ---------------------------------------------------------------- 阶段 2
def _prepare_story(cfg: Config, meta: Path, keys: ApiKeys, *, do_images: bool):
    """载入脚本 → 补齐缺失的语音 → 补齐缺失的配图。返回 (story, raw, llm_ctx)。"""
    story, raw = storyio.load_story(meta, cfg, cfg.paths.get_path("audio_dir"), log)
    filled = storyio.fill_durations_from_metadata(story, raw)
    if filled:
        log.info("有 %d 个分镜的语音缓存不在（换过音色？）→ 用 metadata 里记的实测时长回填",
                 filled)

    missing_audio = [s for s in story.all_scenes if s.duration <= 0]
    if missing_audio:
        log.info("有 %d 个分镜还没有语音，开始合成", len(missing_audio))
        with tts_mod.TTS(cfg, keys) as tts:
            tts_mod.synthesize_all(story.all_scenes, cfg, cfg.paths.get_path("audio_dir"), tts)
        ok = sum(1 for s in story.all_scenes if s.duration > 0)
        if ok < len(story.all_scenes):
            log.warning("有 %d 段语音没合成出来，这些分镜会被跳过",
                        len(story.all_scenes) - ok)

    total = sum(s.duration for s in story.all_scenes)
    lo = float(cfg.story.get("min_total_seconds", 300))
    hi = float(cfg.story.get("max_total_seconds", 600))
    log.info("语音总时长 %.1fs = %.2f 分钟（目标区间 %.0f-%.0fs）", total, total / 60, lo, hi)
    if not (lo <= total <= hi):
        # ← 这里**故意**不自动改写：脚本你在阶段 1 已经看过、也照着它剪了素材
        log.warning("时长跑出区间了。阶段 2 不会自动改写文稿（你已经照它剪了素材）——"
                    "要改就回阶段 1 重出稿，或调 story.seconds_per_chapter。")
    return story, raw, total


def _selfcheck(cfg: Config, story, edl: dict, outs: list[Path]) -> list[str]:
    """出片后的自检：文件、时长、画幅、音轨、切片占比。返回问题清单。"""
    problems: list[str] = []
    want = sum(float(p.get("dur") or 0) for p in edl.get("scenes") or [])
    share = edl_mod.clip_share(edl)
    cap_share = float(cfg.clips.get("max_share", 0.45))
    for out in outs:
        if not out.exists() or out.stat().st_size < 1024:
            problems.append(f"成片不存在或过小：{out}")
            continue
        info = video.probe_streams(out)
        v = info.get("video") or {}
        dur = float(info.get("duration") or 0)
        if not v:
            problems.append(f"{out.name}：没有视频流")
        if not (info.get("audio") or {}).get("codec"):
            problems.append(f"{out.name}：没有音轨（旁白会听不见）")
        if dur < want * 0.9:
            problems.append(f"{out.name}：只有 {dur:.1f}s，正文应有 {want:.0f}s，可能缺片段")
        if dur > want * 1.35 + 30:
            problems.append(f"{out.name}：{dur:.1f}s 比正文 {want:.0f}s 长出太多，可能有残留")
        log.info("自检 [%s] %.1fs　%sx%s@%s　音轨 %s　切片占比 %.1f%%",
                 out.name, dur, v.get("width"), v.get("height"), v.get("fps"),
                 (info.get("audio") or {}).get("codec") or "无", share * 100)
    if share > cap_share:
        problems.append(f"切片占比 {share:.1%} 超过合规上限 {cap_share:.1%}")
    return problems


def _source_report(cfg: Config, story, edl: dict, index: dict) -> Path:
    """素材出处清单（发布举证用）。每条用到的素材都要能说出片名/年份/来源。"""
    used: dict[str, dict] = {}
    for p in edl.get("scenes") or []:
        for sh in p.get("shots") or []:
            cid = str(sh.get("clip_id") or "")
            if not cid or str(sh.get("kind")) not in ("clip", "reuse"):
                continue
            rec = used.setdefault(cid, {"dur": 0.0, "scenes": set()})
            rec["dur"] += float(sh.get("dur") or 0)
            rec["scenes"].add(int(p.get("scene") or 0))
    by_id = {str(c.get("id")): c for c in (index.get("clips") or [])}
    lines = [f"# 素材出处清单 · {edl.get('title') or edl.get('topic')}", "",
             f"生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}", ""]
    total = sum(float(p.get("dur") or 0) for p in edl.get("scenes") or [])
    share = edl_mod.clip_share(edl)
    lines += [f"- 成片画面总长：{total:.0f} 秒",
              f"- 使用影视素材：{len(used)} 条，合计 "
              f"{sum(r['dur'] for r in used.values()):.0f} 秒",
              f"- **切片占比：{share:.1%}**（合规上限 "
              f"{float(cfg.clips.get('max_share', 0.45)):.0%}）",
              f"- 单段上限：{float(cfg.clips.get('max_seconds', 10)):.0f} 秒（全部已去音轨）", "",
              "| 素材 id | 片名 | 年份 | 人物 | 时长占比 | 用在分镜 | 描述 |",
              "|---|---|---|---|---|---|---|"]
    for cid, rec in sorted(used.items()):
        c = by_id.get(cid, {})
        lines.append(f"| `{cid}` | {c.get('title') or '?'} | {c.get('year') or '?'} | "
                     f"{'、'.join(c.get('people') or []) or '—'} | "
                     f"{rec['dur']:.1f}s | "
                     f"{'、'.join(str(x) for x in sorted(rec['scenes']))} | "
                     f"{c.get('desc') or ''} |")
    lines += ["", "> 未在此表中的画面为 AI 生成图或公共领域图库图（见 data/images/_sources.json）。", ""]
    out_dir = cfg.paths.get_path("output_dir")
    stamp = datetime.now().strftime("%Y%m%d")
    p = out_dir / f"{stamp}_{pipeline.safe_filename(edl.get('title') or '未命名')}_素材出处.md"
    p.write_text("\n".join(lines), encoding="utf-8")
    return p


def stage2(cfg: Config, args) -> int:
    """阶段 2：补齐语音/配图 → AI 导演排镜头 → 合规校验 → 渲染 → 自检报告。"""
    ensure_dirs(cfg)
    keys = ApiKeys.from_env()
    meta = pick_plan(cfg, getattr(args, "metadata", None))
    if meta is None or not Path(meta).exists():
        log.error("找不到 metadata：先跑 --stage 1 出脚本")
        return 1
    log.info("═" * 70)
    log.info("阶段 2：AI 剪辑 + 渲染")
    log.info("  脚本：%s", Path(meta).name)
    log.info("  这一期：%s", meta_brief(meta) or "（metadata 里没记题材）")
    log.info("═" * 70)
    note = pending_note(cfg, Path(meta))
    if note:
        for line in note.splitlines():
            log.warning("%s", line)

    do_images = not bool(getattr(args, "no_images", False))
    do_video = not bool(getattr(args, "no_video", False))
    orient_arg = str(getattr(args, "orientation", "both") or "both")
    orients = (list(cfg.video.orientations.keys()) if orient_arg == "both" else [orient_arg])

    story, raw, total = _prepare_story(cfg, Path(meta), keys, do_images=do_images)
    scenes = [s for s in story.all_scenes if s.duration > 0]

    index = clips_mod.load_index(clips_mod.index_path(cfg))
    from .llm import LLM
    src_log: list[dict] = []
    with LLM(cfg, keys) as llm:
        # 配图：回退画面（没找到影视素材的分镜）靠它出图，走和主流程同一条链路
        if do_images and any(s.image_path is None for s in scenes):
            from . import pipeline as _pl
            src_log = _pl.images_for_story(story, cfg, llm)

        recorded = storyio.recorded_durations(raw)
        need_obj = needs_mod.build_needs(story, cfg, llm, recorded=recorded)
        cov = needs_mod.coverage(need_obj, index)
        log.info("素材覆盖：已绑定 %d / 有候选 %d / 还缺 %d（关键 %d）；"
                 "素材库共 %d 条",
                 len(cov["bound"]), len(cov["suggested"]), len(cov["missing"]),
                 len(cov["missing_must"]), len(index.get("clips") or []))
        if not index.get("clips"):
            log.warning("素材库是空的 → 全部走回退画面（不会有影视切片）。"
                        "要真用切片：按需求清单剪素材 → scripts\\import_clip.py 绑槽位。")
        edl = edl_mod.build_edl(story, need_obj, index, cfg, llm)

    problems = edl_mod.validate_edl(edl, cfg, index)
    edl_path = edl_mod.save(edl, cfg)
    log.info("剪辑表：%s", edl_path)
    log.info("剪辑表内容：%s", edl_mod.describe(edl))
    if problems:
        for p in problems:
            log.error("剪辑表有问题：%s", p)
        log.error("剪辑表没通过校验 → 不出片（宁可不出，也不要出一版错位的片）")
        return 2

    # ---- 渲染
    outs: list[Path] = []
    if do_video:
        for orient in orients:
            out, cover = pipeline.render_orientation(story, cfg, orient, edl=edl)
            outs.append(out)
            if cover:
                log.info("封面：%s", cover)

    # ---- 自检 + 举证
    report_problems = _selfcheck(cfg, story, edl, outs) if outs else []
    src_md = _source_report(cfg, story, edl, index)
    log.info("素材出处清单：%s", src_md)
    log.info("═" * 70)
    if report_problems:
        for p in report_problems:
            log.error("自检不通过：%s", p)
        return 3
    if outs:
        log.info("阶段 2 完成，成片：")
        for p in outs:
            log.info("  %s", p)
    log.info("═" * 70)
    return 0


def main_agent(cfg: Config, args) -> int:
    stage = str(getattr(args, "stage", "status") or "status")
    if stage == "status":
        return status(cfg)
    if stage == "1":
        return stage1(cfg, args)
    if stage == "2":
        return stage2(cfg, args)
    log.error("不认识的阶段：%s（用 status / 1 / 2）", stage)
    return 1
