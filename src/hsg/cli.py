"""命令行入口。

    python -m hsg.cli                       # 随机挑个历史题材，跑全流程
    python -m hsg.cli -t "赤壁之战"           # 指定题材
    python -m hsg.cli --minutes 9            # 目标时长 9 分钟
    python -m hsg.cli -o portrait            # 只出竖屏
    python -m hsg.cli plan                   # 只写稿（不出语音、不出视频）
    python -m hsg.cli probe-tts              # 实测语速，校准配置
    python -m hsg.cli probe-images           # 检查图源可用性
    python -m hsg.cli voices                 # 列出 MiniMax 音色
    python -m hsg.cli smoke                  # 零 LLM 的画面/编码冒烟测试
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from . import topics
from .config import (ApiKeys, assert_text_provider, ensure_dirs, env_get,
                     get_channel, load_config, provider_banner)
from .verify import VerifyFailed
from .tts import TTSFailed
log = logging.getLogger("hsg")


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)-12s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )
    # 第三方库的 DEBUG 噪声压掉
    for noisy in ("httpx", "httpcore", "PIL"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _apply_overrides(cfg, args) -> None:
    """把命令行参数写回配置（Config 是递归就地转换的，所以这样写真的生效）。"""
    if getattr(args, "minutes", None):
        sec = int(float(args.minutes) * 60)
        cfg.story["target_total_seconds"] = sec
        # 允许区间跟着目标走，但不超过 5-10 分钟的原始要求
        cfg.story["min_total_seconds"] = max(120, int(sec * 0.75))
        cfg.story["max_total_seconds"] = max(300, int(sec * 1.25))
    if getattr(args, "chapters", None):
        cfg.story["chapters"] = int(args.chapters)
    if getattr(args, "seconds_per_chapter", None):
        cfg.story["seconds_per_chapter"] = int(args.seconds_per_chapter)
    if getattr(args, "seconds_per_scene", None):
        cfg.story["seconds_per_scene"] = int(args.seconds_per_scene)
    if getattr(args, "provider", None):
        cfg.llm["provider"] = str(args.provider)
    if getattr(args, "tts_provider", None):
        # minimax 余额不足/限流时可以切 edge（免费），验证内容链路不被卡住
        cfg.tts["provider"] = str(args.tts_provider)
    if getattr(args, "voice", None) or getattr(args, "speed", None):
        # 音色/语速写到**当前生效**的那个 provider 上，而不是写死 minimax
        # （否则 --tts-provider edge 时 --speed 会静默失效）
        active = str(cfg.tts.get("provider") or "minimax")
        sub = cfg.tts.setdefault(active, {})
        if getattr(args, "voice", None):
            sub["voice_id"] = str(args.voice)
        if getattr(args, "speed", None):
            sub["speed"] = float(args.speed)
    if getattr(args, "chars_per_second", None):
        # 换语速后必须同步校准：probe-tts --speed 1.1 实测出来的值填这里
        cfg.story["chars_per_second"] = float(args.chars_per_second)
    if getattr(args, "license_policy", None):
        cfg.images["license_policy"] = str(args.license_policy)
    if getattr(args, "no_bgm", False):
        cfg.bgm["enabled"] = False
    if getattr(args, "log_level", None):
        cfg.runtime["log_level"] = str(args.log_level)


def _orientations(cfg, arg: str) -> list[str]:
    if arg == "both":
        return list(cfg.video.orientations.keys())
    return [arg]


def _plan_stem(title: str) -> str:
    """`plan` 产物的文件名主干：**必须带标题**。

    只写「<日期>_plan」的话，同一天跑第二次 plan 会静默覆盖上一期的稿子和 metadata
    —— 试两次选题，第一期就没了（踩过，只能从备份恢复）。
    """
    from datetime import datetime as _dt

    from .pipeline import safe_filename as pipeline_safe

    return f"{_dt.now().strftime('%Y%m%d')}_plan_{pipeline_safe(title)}"


def _build_topic(title: str, type_name: str = "", desc: str = "", user=None,
                 series: str = "", ep: int = 0):
    """把「手填的标题 + 指定的类型/描述」拼成 Topic。

    类型是**受控词表**：写错了当场报错并列出全部可选值，不要静默归到「未分类」——
    静默归类的后果是「避开连着做同一路」失效，而且你要过很久才会发现。
    """
    from . import topics as topics_mod

    t = str(type_name or "").strip()
    known = topics_mod.all_types(user)
    if t and t not in known:
        log.error("没有这个故事类型：%s", t)
        log.error("  可选：%s", "、".join(known))
        log.error("  看全部类型与描述：run.bat topics")
        log.error("  要新开一个类型：python scripts\\add_topic.py --type-desc \"…\" --type \"%s\" …", t)
        raise SystemExit(2)
    return topics_mod.Topic(title=str(title or "").strip(), type=t,
                            desc=str(desc or "").strip(),
                            series=str(series or "").strip(), ep=int(ep or 0))


def _resolve_topic(cfg, args):
    """定题材：显式指定先查重；否则从池子里挑没做过的；池子挑空了让模型出个新题。

    返回的是 `topics.Topic`（**两级**：类型 + 标题 + 这一期讲什么）。
    手填 `-t` 的题没有类型和描述 → 由 pipeline 在模型上下文里补（判类型 + 生成描述）。

    这是「不要有重复的故事」的落点 —— 生成记录（data/history.json）是查重依据。
    """
    from . import history, topics
    from .llm import LLM

    # 用户自己制定的选题池（data/topics_user.json）—— 一条命令就能加，
    # 见 scripts/add_topic.py。它并进内置池：能挑到、能按类型过滤、查重照样生效。
    user = topics.load_user_pool(topics.user_pool_path(cfg))

    want_series = (str(getattr(args, "series_name", "") or "").strip()
                   or str(getattr(args, "series", "") or "").strip())
    if want_series and not args.topic:
        ep = topics.next_episode(user, want_series, lambda x: history.is_used(cfg, x))
        if ep is None:
            names = user.series_names()
            log.error("系列「%s」没有可做的下一集（做完了，或者这个系列还没建）", want_series)
            log.error("  已有系列：%s", "、".join(names) or "（还没有）")
            log.error("  看进度：run.bat series　　新建一集：python scripts\\add_topic.py "
                      "--series \"%s\" --series-desc \"…\" --ep N --type \"…\" --title \"…\" "
                      "--desc \"…\"", want_series)
            raise SystemExit(2)
        log.info("系列《%s》第 %d 集：%s", ep.series, ep.ep, ep.title)
        log.info("  故事类型：%s（%s）", ep.type or "未分类", topics.type_desc(ep.type, user) or "—")
        log.info("  这一期讲什么：%s", ep.desc or "（待生成）")
        return ep

    if args.topic:
        # 规则先判；规则判不出来的「换了个说法」交给模型裁定（只沾边时才真的调一次）
        with LLM(cfg, ApiKeys.from_env()) as judge:
            dup = history.find_duplicate(cfg, args.topic, llm=judge)
        if dup:
            detail = (f"这个题材已经做过了：{dup.get('title')}"
                      f"（{str(dup.get('generated_at'))[:10]}，"
                      f"{float(dup.get('speech_minutes') or 0):.1f} 分钟）")
            if not args.allow_duplicate:
                log.error("%s", detail)
                log.error("  · 想换一个：去掉 -t/--topic，选题会自动跳过做过的题材")
                log.error("  · 确实要重做：加 --allow-duplicate")
                raise SystemExit(2)
            log.warning("%s  —— 已指定 --allow-duplicate，继续", detail)
        built = _build_topic(args.topic, getattr(args, "topic_type", ""),
                             getattr(args, "topic_desc", ""), user)
        if built.type:
            log.info("本期类型（手动指定）：%s（%s）", built.type,
                     topics.type_desc(built.type, user) or "—")
        if built.desc:
            log.info("这一期讲什么（手动指定）：%s", built.desc)
        return built

    recs = history.load(cfg)
    mode = str(cfg.story.get("angle_mode") or "small")
    recent = topics.used_types(recs, user=user)   # 最近几期类型：优先避开，别连着做同一类
    # --type 与 --topic-type 在池子路径下是一个意思（都当类型过滤），
    # 两者都给就以 --type 为准，并提醒一声，免得以为被忽略了
    want_type = str(getattr(args, "type", "") or "") or str(getattr(args, "topic_type", "") or "")
    t = topics.pick(seed=getattr(args, "seed", None),
                    is_used=lambda x: history.is_used(cfg, x),
                    mode=mode,
                    type_filter=want_type,
                    recent_types=recent,
                    user=user)
    if want_type and t is None:
        log.error("「%s」这一类里的选题都做过了（或类型名写错）", want_type)
        log.error("  看有哪些类型：run.bat topics　（池子挑空 → 用手填：run.bat -t \"标题\"）")
        raise SystemExit(2)
    if t:
        log.info("自动选题：%s", t.title)
        log.info("  故事类型：%s（%s）", t.type or "未分类", t.type_desc or "—")
        log.info("  这一期讲什么：%s", t.desc or "（待生成）")
        log.info("  切入方式 %s；已生成 %d 期，本次已避开做过的"
                 "（类型避开最近 %d 期）",
                 "小切口" if mode == "small" else "事件式", len(recs), len(recent))
        if recent and t.type not in recent:
            log.info("  （最近做过：%s —— 这次换了一路）", "、".join(recent))
        return t

    log.warning("选题池里的 %d 个题材都做过了，让模型出一个新题",
                len(topics.pool_for(mode, user)))
    avoid = [r.get("topic") or "" for r in recs] + [r.get("title") or "" for r in recs]
    with LLM(cfg, ApiKeys.from_env()) as llm:
        for attempt in range(1, 4):
            t = topics.propose(cfg, llm, avoid, mode=mode)
            if not history.find_duplicate(cfg, t.title):
                log.info("模型出的新题：%s", t.title)
                log.info("  故事类型：%s　讲什么：%s", t.type or "未分类", t.desc or "—")
                return t
            log.warning("第 %d 次出的题材与已有记录相似，重出：%s", attempt, t.title)
            avoid.append(t.title)
    log.warning("模型三次都出到相似题材，就用最后一个：%s", t.title)
    return t


# ------------------------------------------------------------------ 子命令
def cmd_run(cfg, args) -> int:
    from .pipeline import run

    ensure_dirs(cfg)
    keys = ApiKeys.from_env()
    assert_text_provider(cfg)          # 文本模型锁：非 deepseek 直接报错
    topic = _resolve_topic(cfg, args)
    try:
        res = run(
            cfg, topic.title, keys=keys,
            topic_type=topic.type, topic_desc=topic.desc,
            series=topic.series, series_ep=topic.ep,
            do_images=not args.no_images,
            do_video=not args.no_video,
            orientations=_orientations(cfg, args.orientation),
            force=args.force,
            allow_duplicate=args.allow_duplicate,
        )
    except VerifyFailed as exc:
        log.error("出片中止 —— %s", exc)
        log.error("  这不是程序错误，是内容没通过校验：%s",
                  "把 verify.block_on_fail 改成 false 可以先出片再看清单" )
        return 3
    except TTSFailed as exc:
        log.error("出片中止 —— %s", exc)
        return 4
    log.info("用时报告：语音 %.2f 分钟，总耗时 %.1f 分钟",
             res["total_seconds"] / 60, res.get("elapsed_seconds", 0) / 60)
    if res.get("verify", {}).get("recheck_fail"):
        log.warning("注意：本期有 %d 条史实问题未解决，清单在稿件的「待人工核对」一节",
                    res["verify"]["recheck_fail"])
    return 0


def cmd_history(cfg, args) -> int:
    """看生成记录（选题去重就靠它）：已做过哪些故事、多长、配图命中多少。"""
    from . import history

    ensure_dirs(cfg)
    if args.backfill:
        history.backfill_from_metadata(cfg)
    recs = history.load(cfg)
    if not recs:
        log.info("还没有生成记录。跑一期会自动写进去（run.bat），"
                 "也可以用 `history --backfill` 把 data/output 下已有的 metadata 补录进来。")
        return 0
    log.info("累计 %d 期 · %s", len(recs), history.history_path(cfg))
    log.info("%-3s %-17s %-34s %9s %9s %5s %7s", "#", "生成时间", "标题", "成片", "配音", "分镜", "配图")
    total_speech = 0.0
    for i, r in enumerate(recs, 1):
        vs = r.get("video_seconds") or {}
        v = max(vs.values()) if vs else 0.0
        im = r.get("images") or {}
        sp = float(r.get("speech_minutes") or 0)
        total_speech += sp
        log.info("%-3d %-17s %-34s %9s %8.2f分 %5s %7s", i,
                 str(r.get("generated_at") or "")[:16].replace("T", " "),
                 (r.get("title") or "")[:32],
                 f"{v/60:.2f}分" if v else "-",
                 sp, r.get("scenes") or 0,
                 f"{im.get('found', '-')}/{im.get('total', '-')}")
    log.info("合计配音 %.2f 分钟（%.1f 小时）", total_speech, total_speech / 60)
    log.info("人看的版本：%s", history.md_path(cfg))
    return 0


def cmd_plan(cfg, args) -> int:
    """只跑「素材 → 大纲 → 写稿 → 校验」，不花 TTS 和渲染的时间。"""
    from .llm import LLM
    from .material import fetch_material
    from .outline import build_outline
    from .pipeline import print_timeline, timeline_rows, write_metadata, write_script
    from .pipeline import safe_filename as pipeline_safe
    from .verify import audit_facts, final_check, llm_audit, report, rule_check
    from .writer import write_all

    ensure_dirs(cfg)
    keys = ApiKeys.from_env()
    assert_text_provider(cfg)
    topic = _resolve_topic(cfg, args)
    material, mat_meta = fetch_material(topic.title, [], cfg,
                                        cfg.paths.get_path("material_dir"))

    with LLM(cfg, keys) as llm:
        # 两级补全（判类型 + 生成描述）；日志由 build_outline 统一打，别在这里重复
        filled = topics.fill_levels(topic, cfg, llm)
        story = build_outline(topic.title, cfg, llm, material,
                              topic_type=filled.type, topic_desc=filled.desc,
                              series=topic.series, series_ep=topic.ep)
        audit_facts(story, cfg, llm)          # 锚点核查（和正式流程一致）
        story = write_all(story, cfg, llm)
        report(rule_check(story, cfg))
        report(llm_audit(story, cfg, llm))
        # 复检（和正式流程一致）：改写/写稿之后再验一遍
        recheck = final_check(story, cfg, llm)
        if recheck["fails"]:
            report(recheck["fails"])
            story.notes = [f"分镜 {i['scene'] or '-'}：{i['detail']}" for i in recheck["fails"]]
            log.warning("复检仍有 %d 条必须处理的问题（已写进稿件的「待人工核对」清单）",
                        len(recheck["fails"]))
        else:
            log.info("复检通过：没有发现必须处理的问题（提示 %d 条）", len(recheck["warns"]))

    est = sum(s.chars for s in story.all_scenes) / max(0.1, float(cfg.story.chars_per_second))
    rows = timeline_rows(story)
    print_timeline(rows)
    out = cfg.paths.get_path("output_dir")
    from datetime import datetime

    stem = _plan_stem(story.title or topic.title)
    log.info("预估语音时长 %.2f 分钟（%.0f 秒）", est / 60, est)
    log.info("稿件：%s", write_script(story, cfg, out / f"{stem}_脚本.md", est))
    # ⚠️ 这里给的是 topic.title（字符串）：write_metadata 的 extra 会被 json.dumps，
    #    传 Topic 对象会直接 TypeError（踩过 —— 因为写文件在最后一步，报错时文件没写坏）
    log.info("元数据：%s", write_metadata(story, cfg, out / f"{stem}_metadata.json", rows,
                                         {"topic": topic.title, "material": mat_meta,
                                          "plan_only": True}))
    return 0


def cmd_probe_tts(cfg, args) -> int:
    """实测语速：合成一段范文，量出「字/秒」，用来校准 story.chars_per_second。

    缓存文件名带语速 —— 否则改了 --speed 之后量回来的还是上次那个速度的时长。
    """
    from .tts import TTS, probe_duration

    ensure_dirs(cfg)
    assert_text_provider(cfg)
    sample = (
        "东汉建安十三年，公元二百零八年冬天，曹操的水军把战船首尾相连，停在长江北岸。"
        "江面上起了东南风，黄盖的十艘蒙冲斗舰，满载薪草膏油，直冲曹营。"
        "这一夜之后，天下的格局就再也回不去了。"
        "周瑜站在楼船的高处，看着江面上的火光连成一片。他很清楚，曹操退回北方之后，"
        "长江以南的局势会重新洗牌，而孙权帐下的那些老臣，明天就要换一套说辞了。"
        "史书上把这一战写得很简略，但简略的背后，是无数个具体的、慌乱的、"
        "在夜色里不知道该往哪边划桨的人。他们中的大多数人，连名字都没有留下来。"
    )
    audio_dir = cfg.paths.get_path("audio_dir")
    speed = cfg.tts[str(cfg.tts.provider)].get("speed")
    voice = str(cfg.tts[str(cfg.tts.provider)].get("voice_id") or "")
    out = audio_dir / f"probe_{voice}_{speed}.mp3"
    with TTS(cfg) as t:
        path, dur = t.synthesize(sample, out)
    rate = len(sample) / max(0.01, dur)
    # 兜底音色必须挡在这里：probe 的目的是「给这个音色」校准字/秒，
    # 如果实际是 edge 念的，报出来的数就是**另一个音色**的语速，
    # 照它改 config 会让时长估算一直错下去（实测踩过：报 4.75 字/秒，
    # 看着很正常，其实配额音色一个字都还没合成）。
    if path.name.endswith(".edge.mp3"):
        log.warning("⚠️ 上面这段是 edge 兜底音色（%s）念的，**不是**配置里的「%s」。",
                    t.edge_voice, voice)
        log.warning("   所以 %.2f 字/秒 是那个免费音色的语速，**不能**用来校准 "
                    "story.chars_per_second —— 照它改会让估算一直偏。", rate)
        log.warning("   先确认 MiniMax 余额/限流（第一条 [hsg.tts] 日志里有原因），"
                    "恢复正常后再跑一次 probe-tts。")
        return 5
    log.info("范文 %d 字 → %.2f 秒，实测 %.2f 字/秒（音色 %s / 语速 %s）",
             len(sample), dur, rate, voice, speed)
    log.info("当前配置 story.chars_per_second = %s；建议改成 %.2f",
             cfg.story.get("chars_per_second"), rate)
    log.info("用法：run.py --speed %s --chars-per-second %.2f", speed, rate)
    log.info("（换算：每章 %s 秒 → 约 %d 字）",
             cfg.story.get("seconds_per_chapter"),
             int(float(cfg.story.get("seconds_per_chapter", 68)) * rate))
    return 0


def cmd_probe_images(cfg, args) -> int:
    """检查图源：谁可用、谁的 key 没配、谁被版权策略挡住了、实际能返回什么。"""
    from .images import PROVIDERS, active_providers

    active = active_providers(cfg)
    policy = str(cfg.images.get("license_policy") or "clean")
    log.info("版权策略：%s（clean=只用 CC0/公共领域/免费商用图库）", policy)
    log.info("%-11s %-9s %-10s %s", "图源", "本次参与", "需要 key", "说明")
    for name, spec in PROVIDERS.items():
        if spec["risky"] and policy == "clean":
            note = "版权未明，clean 策略下不参与"
        elif spec["key_env"] and not env_get(spec["key_env"]):
            note = f"未配置 {spec['key_env']}（自动跳过）"
        else:
            note = "可用"
        log.info("%-11s %-9s %-10s %s", name,
                 "是" if name in active else "否",
                 spec["key_env"] or "-", note)

    queries = [args.query] if args.query else \
        ["Ming dynasty painting city wall", "Chinese hanging scroll scholar",
         "Song dynasty landscape painting"]
    log.info("")
    for name in active:
        for q in queries:
            try:
                got = PROVIDERS[name]["fn"](q, cfg, limit=6)
            except Exception as exc:  # noqa: BLE001
                log.warning("[%s] %r 失败：%s", name, q, exc)
                continue
            log.info("[%s] %r → %d 条", name, q, len(got))
            for g in got[:2]:
                log.info("      %s｜%s｜%s", g.get("license"), (g.get("title") or "")[:34],
                         (g.get("url") or "")[:70])

    if not getattr(args, "download", False):
        log.info("")
        log.info("提示：候选条数不能说明图源可用 —— 有些源「搜得到但下不动」。"
                 "加 --download 实测下载速度。")
        return 0

    # ---- 实测下载速度（这才是图源能不能用的判据）
    import time

    from .images import download_image

    log.info("")
    log.info("实测下载速度（拉一张真图计时）")
    tmp = cfg.paths.get_path("image_dir") / "_speedtest"
    for name in active:
        for q in queries:
            cands = PROVIDERS[name]["fn"](q, cfg, limit=3)
            if not cands:
                continue
            cand = cands[0]
            dest = tmp / f"{name}_{abs(hash(cand['url'])) % 9999}.bin"
            t0 = time.monotonic()
            got = download_image(cand["url"], dest, cfg, referer=cand.get("page", ""))
            spent = time.monotonic() - t0
            if got:
                kb = dest.stat().st_size / 1024
                log.info("  %-10s ✓ %7.0f KB / %5.1f 秒 = %6.0f KB/s   %s",
                         name, kb, spent, kb / max(spent, 0.01), cand["url"][:52])
                dest.unlink(missing_ok=True)
            else:
                log.info("  %-10s ✗ 下不动或被体积/尺寸门槛挡掉（%.1f 秒）  %s",
                         name, spent, cand["url"][:52])
            break
    return 0


def cmd_voices(cfg, args) -> int:
    from .tts import MiniMaxTTS

    t = MiniMaxTTS(cfg)
    try:
        for v in t.voices():
            log.info("%-28s %-10s %s", v.get("voice_id"), v.get("gender"),
                     v.get("voice_name") or "")
    finally:
        t.close()
    return 0


def cmd_smoke(cfg, args) -> int:
    """零 LLM 冒烟测试：验证配图下载 + 画面合成 + ASS + ffmpeg 编码 + 拼接。"""
    import subprocess
    root = Path(__file__).resolve().parents[2]
    return subprocess.call([sys.executable, str(root / "scripts" / "smoke_video.py")])


def cmd_smoke_clips(cfg, args) -> int:
    """零 API 冒烟：影视切片 + EDL 剪辑链路（合成素材 → 排镜头 → 按镜头渲染）。"""
    import subprocess as _sp
    root = Path(__file__).resolve().parents[2]
    cmd = [sys.executable, str(root / "scripts" / "smoke_clips.py")]
    if getattr(args, "orientation", "both") not in (None, "both"):
        cmd += ["-o", str(args.orientation)]
    if getattr(args, "small", False):
        cmd += ["--small"]
    return _sp.call(cmd)


def cmd_test(cfg, args) -> int:
    """零成本回归测试：口播清洗 / 校验过滤 / 折行 / 字幕切分。不调任何 API。"""
    import subprocess
    root = Path(__file__).resolve().parents[2]
    return subprocess.call([sys.executable, str(root / "scripts" / "test_verify.py")])


# ------------------------------------------------------------------ 入口
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="hsg", description="历史小故事：AI 写稿 + TTS 配音 + 配图字幕 → 成片")
    p.add_argument("command", nargs="?", default="run",
                   choices=["run", "plan", "probe-tts", "probe-images", "voices",
                            "smoke", "smoke-clips", "test", "history", "agent", "topics", "series"],
                   help="默认 run")
    p.add_argument("series_name", nargs="?", default="",
                   help="series 命令的系列名（run.bat series \"古代十大权臣\"）")
    p.add_argument("--config", help="指定配置文件（默认项目根 config.yaml）")

    g = p.add_argument_group("内容")
    g.add_argument("-t", "--topic", help="故事主题；不填则从选题池随机挑（自动跳过做过的）")
    g.add_argument("--seed", type=int, help="随机选题的种子（复现同一题材）")
    g.add_argument("--type", help="按故事类型挑题（不指定 -t 时生效；如「行旅与驿传」）")
    g.add_argument("--topic-type", dest="topic_type",
                   help="直接指定本期的 L1 故事类型（配合 -t 手填选题时用；"
                        "不填就由模型判。可选值见 run.bat topics）")
    g.add_argument("--series", help="按系列取下一集（按集号顺序，跳过已做过的）；"
                                    "系列用 scripts\\add_topic.py --series 建，看进度用 run.bat series")
    g.add_argument("--topic-desc", dest="topic_desc",
                   help="直接指定本期的 L2 描述（「这一期到底讲什么」；不填就由模型生成）")
    g.add_argument("--allow-duplicate", action="store_true",
                   help="允许重做已生成过的题材（默认会拦下并提示）")
    g.add_argument("--minutes", type=float, help="目标总时长（分钟），默认 7")
    g.add_argument("--chapters", type=int, help="章节数，默认 6")
    g.add_argument("--seconds-per-chapter", type=int, help="每章目标秒数")
    g.add_argument("--seconds-per-scene", type=int, help="每个分镜目标秒数")
    # 文本模型锁：只允许 deepseek
    g.add_argument("--provider", choices=["deepseek"], default=None,
                   help="文本模型（已锁定 deepseek；MiniMax 只用于 TTS）")

    g2 = p.add_argument_group("语音 / 画面")
    g2.add_argument("--voice", help="MiniMax 音色 ID（如 audiobook_male_1）")
    g2.add_argument("--speed", type=float, help="语速，1.0 为标准（换语速后要用 probe-tts 重新校准字/秒）")
    g2.add_argument("--chars-per-second", type=float, dest="chars_per_second",
                    help="字/秒（覆盖 story.chars_per_second）；配合 --speed 使用")
    g2.add_argument("--no-bgm", action="store_true", help="不混背景音乐")
    g2.add_argument("--small", action="store_true",
                    help="小尺寸跑（内存紧的机器用：run.bat smoke-clips --small）")
    g2.add_argument("-o", "--orientation", default="both",
                    choices=["both", "portrait", "landscape"], help="输出横竖屏")
    g2.add_argument("--license-policy", dest="license_policy", choices=["clean", "mixed"],
                    help="配图版权策略：clean 只用 CC0/公共领域/免费商用图库（默认）；"
                         "mixed 允许关键词检索兜底（版权未明）")
    g2.add_argument("--tts-provider", dest="tts_provider", choices=["minimax", "edge"],
                    help="语音来源：minimax（默认，要余额）/ edge（免费兜底，音色见 tts.edge_voice）")
    g2.add_argument("--no-images", action="store_true", help="不下载配图（用渐变底图）")
    g2.add_argument("--no-video", action="store_true", help="只到语音为止，不渲染视频")
    g2.add_argument("--force", action="store_true", help="忽略缓存重跑")
    g2.add_argument("--query", help="probe-images 用的检索词")
    g2.add_argument("--download", action="store_true",
                    help="probe-images：实测图源下载速度（判据是下得动，不是搜得到）")
    g2.add_argument("--backfill", action="store_true",
                    help="history：把 data/output 下已有 metadata 补录进生成记录")
    g3 = p.add_argument_group("agent（生产线编排）")
    g3.add_argument("--stage", choices=["status", "1", "2"], default="status",
                    help="agent：status 看卡在哪（默认）/ 1 出脚本+素材需求清单 / 2 AI 剪辑+出片")
    g3.add_argument("--metadata", help="agent --stage 2：指定某一期的 metadata.json（默认取最新）")
    g2.add_argument("--log-level", default=None, help="DEBUG / INFO / WARNING")
    return p


def cmd_series(cfg, args) -> int:
    """看系列进度：哪几集已出、哪几集待做、下一集是哪一集。"""
    from . import history, topics as topics_mod

    user = topics_mod.load_user_pool(topics_mod.user_pool_path(cfg))
    want = (str(getattr(args, "series_name", "") or "").strip()
            or str(getattr(args, "series", "") or "").strip())
    if want and want not in user.series_names():
        log.error("没有这个系列：%s", want)
        log.error("  已有系列：%s", "、".join(user.series_names()) or "（还没有）")
        return 2
    print()
    print(topics_mod.series_progress(user, history.load(cfg), want))
    if not want:
        return 0
    nxt = topics_mod.next_episode(user, want, lambda x: history.is_used(cfg, x))
    if nxt:
        print(f"下一集：第 {nxt.ep} 集《{nxt.title}》")
        print(f"  出稿：run.bat agent --stage 1 --series \"{want}\"")
    else:
        print(f"《{want}》已经做完或用光了。")
    return 0


def cmd_topics(cfg, args) -> int:
    """打印两级选题池：类型（高层描述）→ 具体选题（标题 + 这一期讲什么）。

    为什么要有这条命令：选题是两级的，人得能先看见「有哪几路故事」再决定挑哪路。
    只靠随机抽题，你根本不知道池子里还有什么。
    """
    from . import history, topics as topics_mod

    mode = str(cfg.story.get("angle_mode") or "small")
    user = topics_mod.load_user_pool(topics_mod.user_pool_path(cfg))
    want = str(getattr(args, "type", "") or "")
    if want:
        if want not in topics_mod.all_types(user):
            log.error("没有这个类型：%s", want)
            log.error("  可选：%s", "、".join(topics_mod.all_types(user)))
            return 2
        items = [t for t in topics_mod.pool_for(mode, user) if t.type == want]
        print(f"\n■ {want}　{topics_mod.type_desc(want, user)}\n")
        for t in items:
            print(f"    · {t.title}")
            if t.desc:
                print(f"      {t.desc}")
        print()
    else:
        print()
        print(topics_mod.render_pool(mode, user))
        print("（只看某一类：run.bat topics --type \"类型名\"）")
        print("（自己制定选题：python scripts\\add_topic.py --help）")

    used = [str(r.get("topic_type") or "") for r in history.load(cfg)]
    used = [t for t in used if t]
    if used:
        print(f"最近做过的类型：{'、'.join(used[-6:])}")
        print("选题时会优先避开这些（避免连着做同一路）；指定类型用 --type。")
    print("指定类型挑题：run.bat --type \"行旅与驿传\"")
    return 0


def cmd_agent(cfg, args) -> int:
    """生产线编排：阶段闸门 + AI 剪辑决策 + 自检（见 hsg/agent.py）。"""
    from .agent import main_agent
    return main_agent(cfg, args)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = load_config(Path(args.config) if args.config else None)
    setup_logging(str(args.log_level or cfg.runtime.get("log_level", "INFO")))
    _apply_overrides(cfg, args)

    handlers = {
        "run": cmd_run, "plan": cmd_plan, "probe-tts": cmd_probe_tts,
        "probe-images": cmd_probe_images, "voices": cmd_voices,
        "smoke": cmd_smoke, "smoke-clips": cmd_smoke_clips,
        "test": cmd_test, "history": cmd_history,
        "agent": cmd_agent, "topics": cmd_topics, "series": cmd_series,
    }
    return handlers[args.command](cfg, args)


if __name__ == "__main__":
    raise SystemExit(main())
