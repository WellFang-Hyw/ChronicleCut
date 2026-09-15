"""素材需求清单：把脚本分镜翻译成「要去剪什么片段」的工作单。

为什么需要这一层（用户 2026-09-15 提出的「脚本先行」）：

    剪素材是人工的、最慢的一环。如果先剪后配，会剪一堆用不上的。
    正确顺序是：脚本 → **需求清单** → 按单剪片 → 导入 → 渲染。
    所以清单不是附属产物，它是工作单。

两个粒度必须分清（这是容易做错的地方）：

    · **槽位粒度 = 画面切点**：一个分镜 22 秒、单个片段上限 10 秒，
      就必须切成 3 段（s04_sh1 / s04_sh2 / s04_sh3），每段 7 秒左右。
      所以槽位是按「片段」切的，不是按分镜。
    · **素材粒度 = 一个片段**：人工剪好的一段 mp4，导入时绑到它要填的槽位。

「定格 + 放大 + 标注」不需要额外素材 —— 那是在已有片段上抽帧做的编辑决策，
所以清单里只作为 `callout`（建议标注词）提示，不单独占槽位。

优先级与回退（少一个就会卡住整期）：

    priority: must（关键位置，找不到也别用静态图将就）| nice
    fallback: generate（AI 生成）/ still（现有静态配图）/ reuse（复用已入库的同类片段）

合规：清单只描述「需要什么样的镜头」，不含任何片源；片源是导入时才记的。
"""
from __future__ import annotations

import json
import logging
import math
from datetime import datetime
from pathlib import Path

from . import clips as clips_mod
from .config import Config
from .models import Scene, Story

log = logging.getLogger("hsg.needs")

NEEDS_VERSION = 1


def slot_name(scene_index: int, shot: int = 1) -> str:
    """槽位名：s{分镜号:02d}_sh{镜头号}，例如 s04_sh2。"""
    return f"s{int(scene_index):02d}_sh{int(shot)}"


def scene_durations(story: Story, cfg: Config,
                    recorded: dict[int, float] | None = None) -> tuple[dict[int, float], dict[str, int]]:
    """每个分镜的画面时长，按可靠度取三级来源。

    ① `scene.duration`：音频文件在，ffprobe 探到的**真值**
    ② `recorded`：metadata 里当初记下的 `seconds`（成片就是按它渲染的，等价于真值）
    ③ 按字数估：**plan 产物**才会走到这里（脚本先行时还没配音）

    为什么要三级而不是只按字数估：第 7 期就是反例 —— 按当前配置的 4.6 字/秒估是 311 秒，
    而 metadata 记的实测是 414 秒（那期用的是慢一截的克隆音色），低估了 25%。
    清单给的是「要剪多长的片段」，估错了你就剪短了，所以能拿真值就拿真值。

    返回 (时长表, 各级来源的条数)。
    """
    rec = recorded or {}
    cps = float(cfg.story.get("chars_per_second", 4.6)) or 4.6
    durs: dict[int, float] = {}
    src = {"audio": 0, "metadata": 0, "estimated": 0}
    for s in story.all_scenes:
        if float(s.duration) > 0:
            durs[s.index] = float(s.duration)
            src["audio"] += 1
        elif float(rec.get(s.index) or 0) > 0:
            durs[s.index] = round(float(rec[s.index]), 2)
            src["metadata"] += 1
        else:
            durs[s.index] = round(len(s.text or "") / cps, 2)
            src["estimated"] += 1
    return durs, src


def plan_slots(duration: float, cfg: Config) -> list[float]:
    """把一个分镜的时长切成若干槽位，返回每段的秒数。

    每段不能超过 `clips.max_seconds`（单段合规上限），也不超过
    `needs.slot_seconds`（目标节奏）。均分后若低于 `needs.min_shot_seconds`
    就减少段数 —— 宁可少切一刀，也不要一堆 2 秒的碎片。
    """
    cap = min(float(cfg.clips.get("max_seconds", 10.0)),
              float(cfg.needs.get("slot_seconds", 8.5)))
    floor = float(cfg.needs.get("min_shot_seconds", 4.0))
    dur = max(0.0, float(duration))
    if dur <= 0:
        return []
    if dur <= cap:
        return [round(dur, 2)]
    n = int(math.ceil(dur / cap))
    while n > 1 and dur / n < floor:
        n -= 1
    each = round(dur / n, 2)
    out = [each] * n
    out[-1] = round(dur - each * (n - 1), 2)      # 最后一段吸收四舍五入的零头
    return out


def _priority(scene: Scene, cfg: Config) -> str:
    """判**分镜级**优先级：只有章节开场算 must。

    ⚠️ 别把「长分镜」也塞进优先级判据 —— 试过，站不住：
      plan 阶段按字数估出来的分镜普遍 ~31 秒，而阈值 30 秒正好落在这个噪声带里，
      结果 9 场戏全被标成「必须」，优先级等于没有（第 2 版踩的坑）。
      长分镜真正的问题是「找不到素材时不能用一张静态图硬撑」，那是**回退方案**的事，
      判断逻辑放在 `_fallback_of` 里，不要混进优先级。
    """
    return "must" if (bool(cfg.needs.get("must_if_chapter_start", True))
                      and scene.is_chapter_start) else "nice"


def _fallback_of(pri_scene: str, dur: float, cfg: Config) -> str:
    """找不到素材时怎么兜。

    关键位置（章节开场）和长分镜都走 AI 生成 —— 一张静态文物图撑 30 秒以上太难看了。
    """
    if pri_scene == "must":
        return "generate"
    if float(dur) >= float(cfg.needs.get("long_scene_seconds", 30)):
        return "generate"
    return "still"


def _slot_priority(pri_scene: str, shot: int, dur: float, cfg: Config) -> tuple[str, str]:
    """把分镜级优先级落到槽位上，返回 (槽位优先级, 回退方案)。

    约定：**素材粒度 = 分镜**，所以每个分镜只有第 1 段是「真要去剪的」，
    第 2 段起都标 reuse（用本场素材的不同段落/轻微推拉铺满）。

    为什么：一个 22 秒的分镜按 8.5 秒上限要切 3 段，如果三段都算独立需求，
    工作单会变成 58 条「必须」—— 等于没有优先级，也没人剪得完（这是第一版的问题）。
    只剪 1 段也能成片，多剪几段画面更丰富，这个取舍交给人决定。

    must 只给章节开场的第 1 段，这样「先剪这几条」是 3-6 条。
    """
    if shot > 1:
        return "nice", "reuse"
    pri = "must" if pri_scene == "must" else "nice"
    return pri, _fallback_of(pri_scene, dur, cfg)


def _rule_slots(story: Story, cfg: Config, durs: dict[int, float]) -> list[dict]:
    """规则兜底：不调 LLM 也能产出一份可执行的清单。"""
    rows: list[dict] = []
    for s in story.all_scenes:
        dur_scene = float(durs.get(s.index) or 0.0)
        if dur_scene <= 0:
            continue
        ch = (story.chapters[s.chapter_index - 1]
              if 0 < s.chapter_index <= len(story.chapters) else None)
        pri_scene = _priority(s, cfg)
        need = (s.image_query or "").strip() or (s.text or "").strip()[:34]
        for k, dur in enumerate(plan_slots(dur_scene, cfg), 1):
            pri, fb = _slot_priority(pri_scene, k, dur_scene, cfg)
            rows.append({
                "slot": slot_name(s.index, k),
                "scene": s.index,
                "chapter": s.chapter_index,
                "shot": k,
                "shots_in_scene": 0,          # 下面统一回填
                "dur": dur,
                "need": need,
                "people": [],
                "era": story.period or "",
                "topic": [story.topic] if story.topic else [],
                "priority": pri,
                "scene_priority": pri_scene,  # 分镜级（渲染时决定"这场戏要不要真素材"看这个）
                "fallback": fb,
                "callout": "",
                "heading": ch.heading if ch else "",
                "narration": (s.text or "")[:60],
            })
    counts: dict[int, int] = {}
    for r in rows:
        counts[r["scene"]] = counts.get(r["scene"], 0) + 1
    for r in rows:
        r["shots_in_scene"] = counts.get(r["scene"], 1)
    return rows


_NEED_SYSTEM = """你在给「历史解说视频」的素材准备做工作单：根据每段旁白，写出这段画面**需要什么影视片段**。

要求：
1. need：一句话描述要找的镜头，**具体到能照着找**，20-40 字，含人物 + 动作/场景 + 景别 + 光线氛围。
   好的例子：曹操独自立于军帐中，中近景，夜，烛光冷调；坏的例子：找一个合适的片段。
2. people：这段涉及的历史人物（1-3 个；没有就空数组）。
3. callout：如果这段适合在画面上打一个大字标注，给出 **2-4 字**的标注词
   （如 枭雄 / 釜底抽薪 / 名存实亡）；不适合就留空字符串。
4. 不要出现现代词、不要提"素材库""版权""AI"。

只输出 JSON：{"scenes": [{"index": 序号, "need": "...", "people": ["..."], "callout": "..."}]}"""


def _llm_fill(rows: list[dict], story: Story, cfg: Config, llm) -> dict[int, dict]:
    """一次便宜的 LLM 调用，为每个分镜补上 need / people / callout。异常静默退回。"""
    if llm is None or not rows:
        return {}
    # 按分镜去重（同一分镜的多个槽位共用同一份描述）
    seen: dict[int, str] = {}
    for r in rows:
        seen.setdefault(int(r["scene"]), "")
    by_scene = {r["scene"]: r for r in rows}
    listing = "\n".join(
        f"{i}. 旁白：{by_scene[i]['narration']}"
        f"　（本章：{by_scene[i]['heading'] or '—'}｜年代：{by_scene[i]['era'] or '—'}）"
        for i in sorted(seen))
    try:
        data = llm.chat_json(
            _NEED_SYSTEM,
            f"本期题材：{story.topic}　标题：{story.title}　年代：{story.period}\n\n"
            f"下面是各段旁白，逐条给出需要的镜头：\n\n{listing}\n\n"
            f'【输出】只输出 JSON（index 就是上面的序号）：\n'
            f'{{"scenes": [{{"index": 1, "need": "…", "people": ["…"], "callout": "…"}}]}}')
    except Exception as exc:  # noqa: BLE001
        log.warning("需求清单的 LLM 补全失败（用规则兜底）：%s", exc)
        return {}
    out: dict[int, dict] = {}
    for item in (data or {}).get("scenes") or []:
        try:
            idx = int(item.get("index"))
        except (TypeError, ValueError):
            continue
        if idx not in seen:
            continue
        need = str(item.get("need") or "").strip()
        people = item.get("people")
        if isinstance(people, str):
            people = [x.strip() for x in people.replace("，", ",").split(",") if x.strip()]
        callout = str(item.get("callout") or "").strip()
        out[idx] = {
            "need": need or by_scene[idx]["need"],
            "people": [str(x) for x in (people or []) if str(x).strip()][:3],
            "callout": callout if 1 <= len(callout) <= 6 else "",
        }
    return out


def build_needs(story: Story, cfg: Config, llm=None,
                recorded: dict[int, float] | None = None) -> dict:
    """产出需求清单（规则兜底 + LLM 补全）。

    `recorded`：metadata 里各分镜当初的实测秒数（可选），有就用来替代字数估算。
    """
    durs, src = scene_durations(story, cfg, recorded)
    rows = _rule_slots(story, cfg, durs)
    filled = _llm_fill(rows, story, cfg, llm) if llm is not None else {}
    for r in rows:
        r.update(filled.get(int(r["scene"]), {}))
        r["topic"] = [t for t in (r["topic"] or []) if t]
    return {
        "version": NEEDS_VERSION,
        "topic": story.topic,
        "title": story.title,
        "era": story.period,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "slot_seconds": float(cfg.needs.get("slot_seconds", 8.5)),
        "duration_source": src,
        "narration_seconds": round(sum(durs.values()), 1),
        "slots": rows,
    }


def coverage(needs: dict, index: dict) -> dict:
    """算素材覆盖度：每个槽位是「已绑定」「有候选」「可复用」还是「真缺」。

    匹配顺序：① 有素材显式绑了这个槽位；② 按人物/年代能检索到候选（可复用）。

    ⚠️ `fallback == "reuse"` 的槽位（同一分镜的第二段起）不算缺口 ——
    它们本来就该用本场第一段素材的不同段落填，不需要你单独去剪。
    把它们算进缺口的话，工作量会被虚报一倍（第一版就是这样：58 个"缺口"里
    其实只有二十来个真要剪）。
    """
    rows = needs.get("slots") or []
    bound, suggested, missing, reuse = [], [], [], []
    for r in rows:
        slot = str(r.get("slot"))
        if clips_mod.search(index, slot=slot):
            bound.append(slot)
            continue
        hits = clips_mod.search(index, people=r.get("people") or None,
                                topic=None, era=str(r.get("era") or ""))
        if not hits and (r.get("people")):
            hits = clips_mod.search(index, people=r.get("people"))
        if hits:
            suggested.append(slot)
        elif r.get("fallback") == "reuse":
            reuse.append(slot)
        else:
            missing.append(slot)
    must = [str(r["slot"]) for r in rows if r.get("priority") == "must"]
    return {
        "slots_total": len(rows),
        "bound": bound,
        "suggested": suggested,
        "missing": missing,
        "reuse": reuse,
        "missing_must": [s for s in missing if s in set(must)],
        "need_seconds": round(sum(float(r.get("dur") or 0) for r in rows), 1),
        "fresh_seconds": round(sum(float(r.get("dur") or 0) for r in rows
                                   if r.get("fallback") != "reuse"), 1),
        "have_seconds": round(sum(float(c.get("dur") or 0)
                                  for c in (index or {}).get("clips", [])), 1),
    }


def to_markdown(needs: dict, cfg: Config, index: dict | None = None) -> str:
    """产出给人看的工作单（你照着这份去剪素材）。"""
    rows = needs.get("slots") or []
    index = index or clips_mod.empty_index()
    cov = coverage(needs, index)
    out: list[str] = []
    out.append(f"# 素材需求清单 · {needs.get('title') or needs.get('topic')}")
    out.append("")
    out.append(f"- 年代：{needs.get('era') or '—'}　生成时间：{needs.get('generated_at')}")
    out.append(f"- 共 **{cov['slots_total']} 个槽位**，铺满画面需要素材总长 "
               f"**{cov['need_seconds']:.0f} 秒**（旁白 "
               f"{(needs.get('narration_seconds') or cov['need_seconds']):.0f} 秒）")
    out.append(f"- 其中**真正要去剪的约 {cov['fresh_seconds']:.0f} 秒**，"
               f"其余 {len(cov['reuse'])} 个槽位复用本场素材")
    out.append(f"- 素材库现有 {len(index.get('clips') or [])} 条（{cov['have_seconds']:.0f} 秒）；"
               f"已绑定 {len(cov['bound'])}，有候选 {len(cov['suggested'])}，"
               f"**还缺 {len(cov['missing'])}**（其中关键位置 {len(cov['missing_must'])}）")
    out.append("")
    out.append("## 怎么用这份清单")
    out.append("")
    out.append("1. 按下面的槽位去剪片段：**单段 6–10 秒、无音轨、无字幕**。")
    out.append("2. 同一个分镜的几段，可以来自**同一场戏的不同时刻**（同一段素材切几段就行）。")
    out.append("   表里标「↺ 复用本场素材」的槽位不用单独剪 —— 每场只剪 1 段也能成片")
    out.append("   （渲染时会取这段的不同段落、配轻微推拉铺满）；想画面更丰富就多剪几段绑同场其他槽位。")
    out.append("3. 导入时绑槽位（一个片段可以同时绑本场所有槽位）：")
    out.append("")
    out.append("   ```")
    out.append("   python scripts\\import_clip.py --file <你剪好的.mp4> --id sg_caocao_01 ^")
    out.append("       --title \"三国演义\" --year 1994 --people 曹操 --era 东汉末 --topic 三国 ^")
    out.append("       --desc \"军帐独坐\" --slots s04_sh1,s04_sh2,s04_sh3")
    out.append("   ```")
    out.append("")
    out.append("4. 剪完重新出一次清单，看覆盖度；缺的先不管 —— 渲染时会按「回退方案」自动降级。")
    out.append("")
    out.append("## 槽位明细")
    out.append("")

    by_scene: dict[int, list[dict]] = {}
    for r in rows:
        by_scene.setdefault(int(r.get("scene") or 0), []).append(r)
    for scene_idx in sorted(by_scene):
        group = by_scene[scene_idx]
        first = group[0]
        mark = {"must": "★这场戏必须有真素材", "nice": "·可选"}.get(
            str(first.get("scene_priority")), "·可选")
        out.append(f"### 分镜 {scene_idx}（第 {first.get('chapter')} 章 "
                   f"{first.get('heading') or ''}）　{mark}　"
                   f"{len(group)} 段 × {group[0].get('dur'):.1f}s")
        out.append("")
        out.append(f"> 旁白：{first.get('narration') or '—'}")
        out.append("")
        out.append(f"> 要找的镜头：**{first.get('need') or '（规则兜底没给描述，请人工补）'}**")
        if first.get("people"):
            out.append(f"> 人物：{'、'.join(first['people'])}")
        if first.get("callout"):
            out.append(f"> 建议标注词：**{first['callout']}**（渲染时会在画面上打大字）")
        out.append(f"> 找不到时的回退：{first.get('fallback')}")
        out.append("")
        out.append("| 槽位 | 时长 | 素材库里的候选 |")
        out.append("|---|---|---|")
        for r in group:
            slot = str(r.get("slot"))
            if slot in cov["bound"]:
                state = "✓ 已绑定"
            elif slot in cov["suggested"]:
                hits = clips_mod.search(index, people=r.get("people") or None,
                                       era=str(r.get("era") or ""))
                state = f"有 {len(hits)} 条候选可复用"
            elif r.get("fallback") == "reuse":
                state = "↺ 复用本场素材（不用单独剪）"
            else:
                state = "**缺**"
            out.append(f"| `{slot}` | {float(r.get('dur') or 0):.1f}s | {state} |")
        out.append("")
    return "\n".join(out) + "\n"


def save(needs: dict, cfg: Config, index: dict | None = None) -> tuple[Path, Path]:
    """写两个文件：给人看的 .md（工作单）+ 给机器用的 .json（后面装配用）。"""
    out_dir = cfg.paths.get_path("data_dir") / "needs"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d")
    safe = "".join(c for c in (needs.get("topic") or "未命名")
                   if c not in '\\/:*?"<>|').strip()[:28] or "未命名"
    md = out_dir / f"{stamp}_{safe}_素材需求.md"
    js = out_dir / f"{stamp}_{safe}_素材需求.json"
    md.write_text(to_markdown(needs, cfg, index), encoding="utf-8")
    js.write_text(json.dumps(needs, ensure_ascii=False, indent=1), encoding="utf-8")
    return md, js
