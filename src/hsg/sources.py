"""取景单：把「这段画面去哪部剧里剪」也交给模型 + 代码辅助。

分工（跟项目其它地方一致的三层）：

  · **模型**：给候选片源。它见过大量剧集资料，能说出「汉武帝托孤这场戏大概在哪部剧里」。
    但它对**确定性信息**（第几集、第几分几秒）是会编的 —— 本项目已经踩过
    「AI 编出不存在的素材 id」的坑。所以候选一律标「**未核实**」，
    提示词里明确禁止编集数与时间码，只允许用剧情阶段描述（「汉武帝晚年、大结局前后」）。
  · **代码**：① 只给**必须剪**的槽位出（省 token，也不淹没重点）；
    ② 生成**检索式** —— 这是实践中真正管用的一步：在 B 站/YouTube 搜一下，
    那一场戏就在结果里，比先查集数再拖进度条快得多；
    ③ 给每个槽位生成**回填栏 + 预填好槽位的 import_clip 命令**（槽位号是确定性的，
    由本地清单给出，不让模型碰）。
  · **边界**：**绝不自动把候选绑到槽位**。绑定永远走 `import_clip.py --slots`，
    因为「用谁的片子」是版权与创作决策，得人来拍板（护栏 5）。
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path

from .config import Config

log = logging.getLogger("hsg.sources")

SOURCES_VERSION = 1

# 模型爱编的两类东西：集数（第58集 / 58集）与时间码（12:30）
FABRICATED = re.compile(r"第?\s*\d+\s*集|\d{1,2}:\d{2}")

# 剪掉编造的集数/时间码之后用这句占位（留空会让人以为「压根没有定位信息」）
STAGE_ONLY = "（模型给的集数/时间码不可靠，已隐去；请按剧情阶段自己在正片里找）"

_SYSTEM = """你是影视资料编辑，帮纪录片/解说视频的剪辑师找「这场戏可能在哪些剧里」。

硬要求（违反即报废）：
1. **绝对不要给出集数和时间码**。你不知道就说不知道 —— 编一个「第 58 集 12:30」会让剪辑师
   白翻半小时。允许、也只允许用**剧情阶段**定位，例如「汉武帝晚年至临终的段落（大结局前后）」。
2. 候选 3 个，按推荐度排序，**先想「这场戏最经典的影视呈现是哪一版」**（往往就是那一两部），
   再考虑其它覆盖同时期的剧。优先大陆历史正剧与纪录片，其次港台/合拍剧；不要综艺和短视频二创。
3. `why` 必须落到**具体剧情**（「汉武帝临终前把周公负成王图交给霍光，是《汉武大帝》的收尾段落」），
   不许写「该剧有大量朝会场景」这类放在哪一条都成立的空话。
   不确定就直说「不确定，需核实」。
3b. **绝对不许编剧名**。实测你这类模型会编出「《霍光传奇》（2020）」这种不存在的剧，
   还标成 high 置信度 —— 剪辑师照着搜一无所获，比空手更糟。
   **每部剧必须同时给出主演或播出平台**（能核实的锚点）；**写不出主演的剧就不要写这条**。
3c. **如果这场戏你想不出任何影视剧拍过，就直接说「印象中没有拍过」**（no_footage=true），
   并建议替代画面（文物/画像石/纪录片空镜）。宁可说没有，也不要硬凑 —— 按错方向找是白费时间。
4. 每处再给 2-3 个**检索关键词**（搜素材用）：用观众/UP 主会用来命名那一场戏的词，
   例如「托孤」「周公负成王图」「霍光废帝」「高平陵之变」。**不要整句旁白**，
   不要人物名（人物名由代码拼）。这一步很重要：剪辑师是拿关键词去 B 站/YouTube 搜，
   不是去翻集数。
5. 只输出 JSON，不要解释。"""


def fallback_keywords(slot: dict) -> list[str]:
    """没有模型时的关键词兜底：用**章节标题**（作者写的），而不是截断旁白。

    踩过：一开始拿 `need` 前 10 字当关键词，搜出来是「汉武大帝 汉武帝病榻前将一」这种
    废词 —— 整句旁白不是检索词。章节标题是作者拟的小标题（「遗诏辅政」），能直接搜。
    """
    heading = str(slot.get("heading") or "").split("：")[0].strip()
    callout = str(slot.get("callout") or "").strip()
    out = [x for x in (callout, heading) if x]
    seen: list[str] = []
    for x in out:
        if x not in seen:
            seen.append(x)
    return seen[:2]


def queries_for(slot: dict, candidates: list[dict] | None = None,
                keywords: list[str] | None = None) -> list[str]:
    """生成检索式（确定性：完全由本地清单字段拼出来，不问模型）。

    为什么这是主力手段：找素材的实际动作是「在 B 站/YouTube 搜一下，看那一场戏在不在」，
    搜到了再拖进度条定时间码。先查集数再找片源反而是绕远路。
    """
    people = [str(p).strip() for p in (slot.get("people") or []) if str(p).strip()]
    kws = [k for k in (keywords or fallback_keywords(slot)) if k]
    out: list[str] = []
    for c in (candidates or [])[:2]:
        name = str(c.get("title") or "").strip()
        if name and kws:
            out.append(f"{name} {kws[0]}")
    if people and kws:
        out.append(f"{' '.join(people[:2])} {kws[0]}")
    for k in kws[:2]:
        out.append(f"{k} 影视片段")
    for c in (candidates or [])[:1]:
        name = str(c.get("title") or "").strip()
        if name and len(kws) > 1:
            out.append(f"{name} {kws[1]}")
    # 去重保序
    seen: list[str] = []
    for q in out:
        q = " ".join(q.split())
        if q and q not in seen:
            seen.append(q)
    return seen[:5]


def import_command(slot: dict, slots_of_scene: list[str]) -> str:
    """给这个槽位预填一条 import_clip 命令（槽位号来自本地清单，不让模型碰）。"""
    people = " ".join(str(p) for p in (slot.get("people") or [])[:3])
    era = str(slot.get("era") or "").split("，")[0].strip()
    return (
        'python scripts\\import_clip.py --file <你剪的.mp4> --id <id> '
        f'--title "<片源名>" --year <年份> --people "{people}" --era "{era}" '
        f'--slots {",".join(slots_of_scene)}'
    )


def _ask_llm(slots: list[dict], topic: str, era: str, llm) -> dict[str, dict]:
    """一次调用问完所有必须剪的槽位。异常静默退回（只出检索式也能用）。"""
    if llm is None or not slots:
        return {}
    # ⚠️ enumerate 从 **1** 开始：清单里的序号是给人（和模型）看的 1-based，
    # 回填时按 idx-1 取。踩过的大坑 —— 写成 enumerate(slots) 会让编号从 0 起，
    # 模型返回 index=1 描述的是第 2 条，于是**每一条候选片源都错位一格**：
    # 剪辑师拿着「第 4 场的画面」去搜「第 5 场的剧」，白跑一趟还找不到原因。
    listing = "\n".join(
        f'{i}. 画面：{s.get("need") or "—"}\n'
        f'   （章节：{s.get("heading") or "—"}'
        f'｜人物：{"、".join(s.get("people") or []) or "—"}｜年代：{s.get("era") or "—"}\n'
        f'    这段旁白：{str(s.get("narration") or "—")[:70]}）'
        for i, s in enumerate(slots, 1))
    try:
        data = llm.chat_json(
            _SYSTEM,
            f"本期题材：{topic}　年代：{era}\n\n"
            f"下面每一段是一处需要真实影视画面的镜头，为每一处找可能有这场戏的剧：\n\n"
            f"{listing}\n\n"
            f"【输出】只输出 JSON（index 是上面的序号）：\n"
            f'{{"shots": [{{"index": 1, '
            f'"no_footage": false, "keywords": ["检索关键词", "…"], '
            f'"candidates": [{{"title": "剧名", "year": "年份（拿不准就空）", '
            f'"cast": "主演（必须给，给不出就别写这条）或播出平台", '
            f'"locate": "剧情阶段定位（不要集数）", "why": "为什么可能有这场戏", '
            f'"confidence": "high/mid/low"}}]}}]}}')
    except Exception as exc:  # noqa: BLE001
        log.warning("取景单的候选片源没问出来（只出检索式，不影响剪素材）：%s", exc)
        return {}
    out: dict[str, dict] = {}
    for item in (data or {}).get("shots") or []:
        try:
            idx = int(item.get("index"))
        except (TypeError, ValueError):
            continue
        if not (1 <= idx <= len(slots)):
            continue
        cands: list[dict] = []
        for c in (item.get("candidates") or [])[:3]:
            if not isinstance(c, dict):
                continue
            title = str(c.get("title") or "").strip()
            if not title:
                continue
            locate = str(c.get("locate") or "").strip()
            # 兜底：提示词里禁了集数/时间码，但模型还是可能塞（实测它很爱写「第58集」）。
            # 这类信息它不可靠 —— 剪掉，只保留剧情阶段描述。
            if FABRICATED.search(locate):
                locate = STAGE_ONLY
            cands.append({
                "title": title[:40],
                "cast": str(c.get("cast") or "").strip()[:40],
                "year": str(c.get("year") or "").strip()[:8],
                "locate": locate[:60],
                "why": str(c.get("why") or "").strip()[:80],
                "confidence": str(c.get("confidence") or "").strip().lower()[:6] or "low",
            })
        kws = [str(k).strip()[:16] for k in (item.get("keywords") or [])
               if str(k).strip()][:3]
        no_footage = bool(item.get("no_footage"))
        if cands or kws or no_footage:
            out[slots[idx - 1]["slot"]] = {"candidates": cands, "keywords": kws,
                                           "no_footage": no_footage}
    return out


def _selfcheck(all_cands: dict[str, list[dict]], llm) -> dict[str, str]:
    """让模型对自己的候选做一次交叉核对：哪些剧你能确信真实存在？

    为什么必须加这一道：实测它会编出不存在的剧（甚至同一条里年份两次都不一样、
    主演也是拼出来的），而**编的剧名比空手更糟** —— 剪辑师照着搜一无所获，
    还会怀疑自己搜错了。自检通常能把这些挑出来（自己评自己比凭空生成谨慎得多）。
    """
    titles: list[str] = []
    for cands in all_cands.values():
        for c in cands:
            if c["title"] not in titles:
                titles.append(c["title"])
    if not titles:
        return {}
    try:
        data = llm.chat_json(
            "你是影视资料核对员。用户会给你一批**电视剧剧名**，你要判断哪些是真实存在的剧、"
            "哪些可能是编的。判据：剧名 + 年份 + 主演能不能对上你确信的记忆；"
            "对不上、或者你觉得这个名字是拼凑的，就判 unsure。"
            "只输出 JSON，不要解释。",
            "逐个判断这些剧名（sure=我确信真实存在，unsure=我不确定或觉得是编的）：\n"
            + "\n".join(f"- 《{t}》" for t in titles)
            + '\n\n【输出】{"verdicts": [{"title": "剧名", "verdict": "sure/unsure"}]}')
    except Exception as exc:  # noqa: BLE001
        log.warning("剧名自检没跑成（候选照旧给你，但请按「未核实」对待）：%s", exc)
        return {}
    out: dict[str, str] = {}
    for item in (data or {}).get("verdicts") or []:
        t = str(item.get("title") or "").strip().strip("《》")
        v = str(item.get("verdict") or "").strip().lower()
        if t and v in ("sure", "unsure"):
            out[t] = v
    return out


def build_sources(needs: dict, cfg: Config, llm=None) -> dict:
    """为**必须剪**的槽位做取景单（其余槽位不浪费 token，也不淹没重点）。"""
    slots = list(needs.get("slots") or [])
    must = [s for s in slots if str(s.get("priority")) == "must"]
    limit = int(cfg.needs.get("sources_max_slots", 12))
    if len(must) > limit:
        log.info("必须剪的有 %d 处，取景单只出优先级最靠前的 %d 处（needs.sources_max_slots）",
                 len(must), limit)
        must = must[:limit]

    picked = [{"slot": s.get("slot"), "need": s.get("need"), "people": s.get("people"),
               "era": s.get("era"), "callout": s.get("callout"), "dur": s.get("dur"),
               "scene": s.get("scene"), "heading": s.get("heading")} for s in must]
    cands = _ask_llm(picked, str(needs.get("topic") or ""), str(needs.get("era") or ""), llm)
    verdicts = _selfcheck({k: v.get("candidates") or [] for k, v in cands.items()}, llm) \
        if cands else {}
    if verdicts:
        log.info("剧名自检：%d 个剧名，%d 个判为「不确定/可能是编的」", len(verdicts),
                 sum(1 for v in verdicts.values() if v == "unsure"))

    by_scene: dict[int, list[str]] = {}
    for s in slots:
        by_scene.setdefault(int(s.get("scene") or 0), []).append(str(s.get("slot")))

    items: list[dict] = []
    for s in picked:
        slot = str(s["slot"])
        got = cands.get(slot) or {}
        got_c = got.get("candidates") or []
        for c in got_c:
            c["verdict"] = verdicts.get(c["title"], "")
        got_k = got.get("keywords") or []
        items.append({
            **s,
            "candidates": got_c,
            "keywords": got_k or fallback_keywords(s),
            "no_footage": bool(got.get("no_footage")),
            "queries": queries_for(s, got_c, got_k),
            "slots_of_scene": by_scene.get(int(s["scene"] or 0), [slot]),
            "import_cmd": import_command(s, by_scene.get(int(s["scene"] or 0), [slot])),
        })
    obj = {
        "version": SOURCES_VERSION,
        "topic": needs.get("topic"),
        "title": needs.get("title"),
        "era": needs.get("era"),
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "llm_used": bool(cands),
        "items": items,
    }
    log.info("取景单：%d 处必须剪的镜头，候选片源 %s",
             len(items), "已出（**未核实**）" if cands else "没问出来（只有检索式）")
    return obj


def to_markdown(src: dict) -> str:
    """取景单正文（插进素材需求 .md 里，让剪辑师只读一个文件）。"""
    items = src.get("items") or []
    out: list[str] = ["## 取景单（去哪儿找这些画面）", ""]
    if not items:
        out += ["（本期没有「必须剪」的槽位，或清单还没生成。）", ""]
        return "\n".join(out)
    out += [
        "> **候选片源是模型给的，未核实** —— 模型**连剧名都会编**（实测编出过不存在的剧、"
        "还标成高置信度），所以：**以你能不能搜到为准，不要以本表为准**。",
        "> 别按候选硬找：**先拿下面的检索式去 B 站/YouTube 搜**，那一场戏通常就在结果里，",
        "> 比先查集数再拖进度条快得多。搜不到再换下一个候选。",
        "",
    ]
    for i, it in enumerate(items, 1):
        out.append(f"### {i}. `{it.get('slot')}`　{float(it.get('dur') or 0):.1f}s　"
                   f"标注「{it.get('callout') or '—'}」")
        out.append("")
        out.append(f"- 要什么画面：{it.get('need') or '—'}")
        out.append(f"- 人物：{'、'.join(it.get('people') or []) or '—'}　年代：{it.get('era') or '—'}")
        if it.get("no_footage"):
            out.append("- ⚠ **模型认为这场戏没有影视剧拍过** → 别硬找，建议用文物/画像石/纪录片空镜，"
                       "或让 AI 生成图兜底（这处走回退画面）")
        cands = it.get("candidates") or []
        if cands:
            out.append("- 候选片源（**未核实**）：")
            for c in cands:
                bits = [f"《{c['title']}》"]
                if c.get("year"):
                    bits.append(f"({c['year']})")
                bits.append(f"主演/平台：{c['cast']}" if c.get("cast")
                            else "**⚠ 模型给不出主演 → 很可能是编的，最后再试**")
                if c.get("confidence"):
                    bits.append(f"[{c['confidence']}]")
                if c.get("verdict") == "unsure":
                    bits.append("**⚠ 自检：不确信存在（可能是编的），最后再试**")
                if c.get("locate"):
                    bits.append(f"剧情阶段：{c['locate']}")
                if c.get("why"):
                    bits.append(f"理由：{c['why']}")
                out.append(f"    - {'　'.join(bits)}")
        else:
            out.append("- 候选片源：模型没给（不影响剪辑 —— 直接用下面的检索式搜）")
        out.append("- 检索式（复制去搜）：")
        for q in it.get("queries") or []:
            out.append(f"    - `{q}`")
        out.append(f"- 找到了就在这里记下来：片源 ＝ ______　集数 ＝ ______　"
                   f"起止时间 ＝ ______（如 12:30–12:37）")
        out.append(f"- 导入命令（剪好之后照这条改）：")
        out.append("")
        out.append("  ```")
        out.append(f"  {it.get('import_cmd')}")
        out.append("  ```")
        out.append("")
    return "\n".join(out)


def save(src: dict, cfg: Config) -> Path:
    """另存一份 json（给人看的正文已经插进素材需求 .md 里了）。"""
    out_dir = cfg.paths.get_path("data_dir") / "needs"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d")
    safe = "".join(c for c in str(src.get("topic") or "未命名")
                   if c not in '\\/:*?"<>|').strip()[:28] or "未命名"
    p = out_dir / f"{stamp}_{safe}_取景单.json"
    p.write_text(json.dumps(src, ensure_ascii=False, indent=1), encoding="utf-8")
    return p
