"""EDL（编辑决策文件）：把「槽位需求 + 素材库」翻译成逐镜头的剪辑计划。

它在整个流程里的位置（这是关键，别搞错顺序）：

    脚本 → 需求清单（needs.py，人工照它剪素材）→ 导入素材（clips）
    → **EDL（本模块）** → 渲染（shotvideo.py）→ 成片

为什么非要一个中间层，不让「需求清单」直接去渲染：

  · 需求清单是**给{人}看的工作单**（去剪什么），EDL 是**给{机器}执行的剪辑表**
    （第几秒用哪条素材的哪一段、什么手法、打什么字）。两者受众不同，不能合并。
  · 剪辑决策（时长怎么分、哪一段定格、放大到几倍、哪里打大字）需要**看素材实际情况**，
    只有等素材导进来以后才能定。所以 EDL 必须排在导入之后。
  · 有了 EDL，渲染就是纯粹的执行，可以反复重渲而不重做决策；出了问题能只看 EDL 定位。

「AI 导演」在哪一层：`direct_edl()` —— 一次 LLM 调用，为每个分镜排出镜头序列。
它只做**有素材可用**的分镜；没有素材的分镜走 `_fallback_shots()`（静态图/生成图 + 推拉），
不让流程卡住。

合规红线（`validate_edl` 会拦）：单镜头 ≤ `clips.max_seconds`、
成片里切片总时长占比 ≤ `clips.max_share`。这两条是发布兜底，不许放宽。
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from . import clips as clips_mod
from .config import Config
from .models import Story

log = logging.getLogger("hsg.edl")

EDL_VERSION = 1

# 手法：直铺 / 慢推 / 定格放大 / 静态图平移
TREATMENTS = ("plain", "slow_push", "freeze_zoom", "still_pan")
TREATMENT_ZH = {"plain": "直接铺", "slow_push": "慢推", "freeze_zoom": "定格放大",
                "still_pan": "静态图平移"}
# 素材来源：真的切片 / 复用同场素材 / 生成图 / 图库静态图
KINDS = ("clip", "reuse", "generate", "still", "comic")
KIND_ZH = {"clip": "切片", "reuse": "复用", "generate": "生成图", "still": "静态图",
           "comic": "四格漫画"}


@dataclass
class Shot:
    """一个镜头 = 一段时间轴上连续的画面。"""
    slot: str                     # 对应需求清单的槽位（s04_sh1），复用也有槽位
    clip_id: str = ""             # 素材库 id（kind=clip/reuse）
    src_in: float = 0.0           # 从素材的第几秒开始取
    dur: float = 0.0              # 这一段在成片里占多久
    treatment: str = "plain"      # TREATMENTS 之一
    callout: str = ""             # 画面上的大字（2-4 字）
    nametag: str = ""             # 人名条
    kind: str = "clip"            # KINDS 之一
    note: str = ""


@dataclass
class ScenePlan:
    """一个分镜的剪辑计划 = 若干镜头 + 这条旁白的语音。"""
    scene: int
    chapter: int
    dur: float                    # 画面总时长（旁白 + 尾垫），必须被 shots 铺满
    shots: list[Shot] = field(default_factory=list)
    heading: str = ""

    @property
    def shot_dur(self) -> float:
        return round(sum(s.dur for s in self.shots), 3)


def _slots_of_scene(needs: dict, scene: int) -> list[dict]:
    return [r for r in (needs.get("slots") or []) if int(r.get("scene") or 0) == scene]


def scene_candidates(index: dict, scene: int, people: list[str] | None = None,
                     era: str = "", allow_era_match: bool = False) -> list[dict]:
    """这个分镜能用的素材：默认**只认显式绑定了本场槽位的**。

    ⚠️ `allow_era_match` 默认 False，这是有意的（冒烟测试里发现的坑）：
    一开始这里在「没有绑定」时会退回「按人物/年代搜到的都算可用」，
    结果没剪素材的分镜也被自动塞了一条同年代的切片进去 ——
    **等于替用户做了创作和版权决策**：观众会看到一段跟旁白无关的画面，
    而且切片时长占比被顶穿合规红线（实测 97.7% > 45%）。

    「同年代有候选」只适合当**提示**（需求清单里那个「有候选可复用」），
    不适合直接进剪辑表。真要放开，把 `edl.allow_era_match` 设成 true 并自负后果。
    """
    bound: list[dict] = []
    seen: set[str] = set()
    for r in (index.get("clips") or []):
        slots = [str(s) for s in (r.get("slots") or [])]
        if any(s.startswith(f"s{scene:02d}_") for s in slots):
            bound.append(r)
            seen.add(str(r.get("id")))
    if bound or not allow_era_match:
        return bound
    return [c for c in clips_mod.search(index, people=people or None, era=era)
            if str(c.get("id")) not in seen]


def _fallback_shots(rows: list[dict], scene_dur: float, cfg: Config) -> list[Shot]:
    """没素材可用时的兜底：按需求清单的槽位铺满，画面走静态/生成。

    这里**不做剪辑决策**（没素材没什么可决策的），但仍要铺满时长，
    因为渲染层要求 shots 的总时长 = 分镜时长。
    """
    out: list[Shot] = []
    left = scene_dur
    for r in rows:
        if left <= 1e-6:
            break
        dur = min(float(r.get("dur") or 0) or left, left)
        if bool(cfg.comic.get("enabled", False)):
            # 四格漫画路线（用户 2026-09-17）：槽位就是一张四格，画面由它铺满
            kind, treat, note = "comic", "plain", "四格漫画（按槽位生成）"
        else:
            kind = "generate" if str(r.get("fallback")) == "generate" else "still"
            treat, note = "still_pan", "没有可用素材，按回退方案出画面"
        out.append(Shot(slot=str(r.get("slot")), dur=round(dur, 2), kind=kind,
                        treatment=treat, callout=str(r.get("callout") or ""), note=note))
        left = round(left - dur, 3)
    if left > 1e-6:      # 槽位时长加起来不够（需求清单是估算的），补到最后一个镜头上
        if out:
            out[-1].dur = round(out[-1].dur + left, 2)
        else:
            out.append(Shot(slot="s??_sh1", dur=round(scene_dur, 2), kind="still",
                            treatment="still_pan", note="无槽位信息，整段兜底"))
    return out


def _rule_shots(cands: list[dict], scene_dur: float, rows: list[dict], cfg: Config) -> list[Shot]:
    """规则兜底排镜头：素材按顺序轮着用，铺满时长；换了素材就换一档手法。

    为什么要有它：LLM 挂了/返回不可用时也得能出片。规则排法不好看但能用，
    而且它同时是 LLM 输出的**校验参照**（时长铺不满就退回这里）。
    """
    cap = float(cfg.clips.get("max_seconds", 10.0))
    out: list[Shot] = []
    left = round(scene_dur, 3)
    i = 0
    while left > 1e-6 and i < 64:
        c = cands[i % len(cands)]
        avail = max(0.2, float(c.get("dur") or cap))
        take = round(min(min(avail, cap), left), 2)
        shot_no = len(out) + 1
        # 素材比这一段短 → 用素材中段起头，避免每次都从同一个画面开始
        src_in = 0.0 if i < len(cands) else round(max(0.0, (avail - take) / 2), 2)
        out.append(Shot(
            slot=f"{_slot_prefix(rows, i)}", clip_id=str(c.get("id")), src_in=src_in,
            dur=take, kind="clip" if i < len(cands) else "reuse",
            treatment=["plain", "slow_push", "freeze_zoom"][shot_no % 3],
            callout=_callout_for(rows, i),
            note="规则排镜头（LLM 未参与）"))
        left = round(left - take, 3)
        i += 1
    if left > 1e-6 and out:
        out[-1].dur = round(out[-1].dur + left, 2)
    return out


def _slot_prefix(rows: list[dict], i: int) -> str:
    if not rows:
        return f"sh{i + 1}"
    return str(rows[min(i, len(rows) - 1)].get("slot") or f"sh{i + 1}")


def _callout_for(rows: list[dict], i: int) -> str:
    if not rows:
        return ""
    return str(rows[min(i, len(rows) - 1)].get("callout") or "")


_DIRECTOR_SYSTEM = """你是历史解说视频的剪辑指导。给你一个分镜的旁白、要找的镜头、以及**手头实际有哪些素材片段**，
你排出这个分镜的镜头序列（哪个片段、从第几秒取、取多久、什么手法、画面上打什么字）。

硬性约束：
1. 所有镜头时长加起来，必须正好等于「画面总时长」（可以差 0.1 秒以内）。
2. 每个镜头时长 ≤ 单段上限（秒数会给你）。
3. `src_in + dur` 不能超过该素材的时长（会给你每条素材的长度）。
4. clip_id 只能用给你的那些 id，不许编。
5. 手法只能从：plain（直铺）/ slow_push（慢推）/ freeze_zoom（定格放大）/ still_pan（静态图平移）。
   - 旁白说到某个**瞬间/细节**（一个动作、一句话、一个表情）→ freeze_zoom 把那一格定住放大，最有劲。
   - 交代环境、铺节奏 → plain 或 slow_push。
   - 同一段素材连着用两次要换手法，别让观众看出是同一段。
6. callout 是画面上打的**大字**（2-4 字，如 亡命东归 / 磨刀声），只在**值得强调的地方**给，
   一个分镜最多 1 个，没有就留空。不要每一镜都打。
7. nametag 只在第一次出现某个人物、且观众可能不认识时给（2-5 字人名），否则留空。

只输出 JSON：
{"scenes": [{"scene": 分镜号, "shots": [
  {"clip_id": "sg_01", "src_in": 0.0, "dur": 6.0, "treatment": "plain",
   "callout": "", "nametag": "曹操"}]}]}"""


def _scene_brief(scene_rows: list[dict], cands: list[dict], dur: float, cfg: Config) -> str:
    cap = float(cfg.clips.get("max_seconds", 10.0))
    lines = [f"分镜 {scene_rows[0].get('scene')}（第 {scene_rows[0].get('chapter')} 章 "
             f"{scene_rows[0].get('heading') or ''}）",
             f"  旁白：{scene_rows[0].get('narration') or '—'}",
             f"  要找的镜头：{scene_rows[0].get('need') or '—'}",
             f"  画面总时长：{dur:.1f} 秒　单段上限：{cap:.1f} 秒"]
    if scene_rows[0].get("callout"):
        lines.append(f"  需求清单里建议的标注词：{scene_rows[0]['callout']}")
    lines.append("  手头素材：")
    for c in cands:
        lines.append(f"    - id={c.get('id')}　时长 {float(c.get('dur') or 0):.1f}s　"
                     f"片源《{c.get('title')}》{c.get('year') or ''}　"
                     f"{c.get('desc') or ''}")
    return "\n".join(lines)


def direct_edl(story: Story, needs: dict, index: dict, cfg: Config, llm) -> dict[int, list[Shot]]:
    """AI 导演：为每个**有素材可用**的分镜排镜头序列。返回 {分镜号: [Shot]}。

    异常一律退回空字典（调用方用 `_rule_shots` 兜底），不能让剪辑决策失败卡住出片。
    """
    tail = float(cfg.video.get("tail_padding", 0.55))
    briefs: dict[int, tuple[list[dict], float]] = {}
    for s in story.all_scenes:
        if s.duration <= 0:
            continue
        rows = _slots_of_scene(needs, s.index)
        people: list[str] = []
        for r in rows:
            people.extend([str(x) for x in (r.get("people") or [])])
        cands = scene_candidates(index, s.index, people=people, era=story.period or "",
                                allow_era_match=bool(cfg.edl.get("allow_era_match", False)))
        if cands:
            briefs[s.index] = (cands, round(s.duration + tail, 2))
    if not briefs or llm is None:
        return {}
    payload = "\n\n".join(_scene_brief(_slots_of_scene(needs, k) or [{"scene": k}], c, d, cfg)
                          for k, (c, d) in sorted(briefs.items()))
    try:
        data = llm.chat_json(_DIRECTOR_SYSTEM,
                             f"本期：《{story.title}》　年代：{story.period}\n\n{payload}\n\n"
                             f'【输出】只输出 JSON：{{"scenes": [{{"scene": 1, "shots": [...]}}]}}')
    except Exception as exc:  # noqa: BLE001
        log.warning("剪辑导演（LLM）失败，改用规则排镜头：%s", exc)
        return {}

    out: dict[int, list[Shot]] = {}
    for item in (data or {}).get("scenes") or []:
        try:
            idx = int(item.get("scene"))
        except (TypeError, ValueError):
            continue
        if idx not in briefs:
            continue
        cands, want = briefs[idx]
        by_id = {str(c.get("id")): c for c in cands}
        shots: list[Shot] = []
        rows = _slots_of_scene(needs, idx)
        for i, sh in enumerate(item.get("shots") or []):
            cid = str(sh.get("clip_id") or "")
            if cid not in by_id:            # 编了不存在的 id → 整段决策作废
                log.warning("导演给了不存在的素材 id %r（分镜 %s），改用规则排镜头", cid, idx)
                shots = []
                break
            src_in = max(0.0, float(sh.get("src_in") or 0))
            dur = max(0.2, float(sh.get("dur") or 0))
            # 素材长度校验：导演经常让 7 秒的素材出 9~10 秒（提示词里写了它也不听）。
            # 这类镜头**渲染时必然出错**（ffmpeg 取不到那么长），所以在这里就退回规则排法 ——
            # 踩过：只靠后面的 validate_edl 拦，会把**整集**毙掉（宁可不出片），
            # 而其实只有这几个分镜需要退让。
            clip_dur = float(by_id[cid].get("dur") or 0)
            if clip_dur > 0 and src_in + dur > clip_dur + 0.05:
                log.warning("导演让 %s 出 %.1fs（从 %.1fs 起），但这条素材只有 %.1fs（分镜 %s）"
                            "→ 该分镜改用规则排镜头", cid, dur, src_in, clip_dur, idx)
                shots = []
                break
            treat = str(sh.get("treatment") or "plain")
            if treat not in TREATMENTS:
                treat = "plain"
            shots.append(Shot(
                slot=_slot_prefix(rows, i), clip_id=cid, src_in=round(src_in, 2),
                dur=round(dur, 2), treatment=treat,
                callout=str(sh.get("callout") or "")[:6],
                nametag=str(sh.get("nametag") or "")[:8],
                kind="clip" if i < len(rows) else "reuse",
                note="AI 导演排的"))
        total = round(sum(s.dur for s in shots), 2)
        # 时长对不上就退回规则排法 —— 宁可不好看，也不能出个时长错位的成片
        if shots and abs(total - want) <= max(0.25, want * 0.02):
            out[idx] = shots
        elif shots:
            log.warning("导演排的镜头总长 %.2fs ≠ 分镜时长 %.2fs（分镜 %s），改用规则排镜头",
                        total, want, idx)
    return out


def build_edl(story: Story, needs: dict, index: dict, cfg: Config, llm=None) -> dict:
    """产出整期 EDL。没有素材的分镜走回退，有素材的交给 AI 导演（失败则规则兜底）。"""
    tail = float(cfg.video.get("tail_padding", 0.55))
    directed = direct_edl(story, needs, index, cfg, llm)
    scenes: list[ScenePlan] = []
    for s in story.all_scenes:
        if s.duration <= 0:
            continue
        rows = _slots_of_scene(needs, s.index)
        ch = (story.chapters[s.chapter_index - 1]
              if 0 < s.chapter_index <= len(story.chapters) else None)
        dur = round(s.duration + tail, 2)
        people: list[str] = []
        for r in rows:
            people.extend([str(x) for x in (r.get("people") or [])])
        cands = scene_candidates(index, s.index, people=people, era=story.period or "",
                                allow_era_match=bool(cfg.edl.get("allow_era_match", False)))
        if s.index in directed:
            shots = directed[s.index]
        elif cands:
            shots = _rule_shots(cands, dur, rows, cfg)
        else:
            shots = _fallback_shots(rows, dur, cfg)
        scenes.append(ScenePlan(scene=s.index, chapter=s.chapter_index, dur=dur,
                                shots=shots, heading=ch.heading if ch else ""))
    return {
        "version": EDL_VERSION,
        "title": story.title,
        "topic": story.topic,
        "era": story.period,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "tail_padding": round(tail, 2),
        "scenes": [asdict(p) for p in scenes],
    }


def clip_share(edl: dict) -> float:
    """成片里「真切片」占的时长比例（合规用）。复用和兜底画面不算切片引用。"""
    total = sum(float(p.get("dur") or 0) for p in edl.get("scenes") or [])
    if total <= 0:
        return 0.0
    used = sum(float(s.get("dur") or 0)
               for p in edl.get("scenes") or []
               for s in p.get("shots") or [] if s.get("kind") == "clip")
    return round(used / total, 4)


def validate_edl(edl: dict, cfg: Config, index: dict | None = None) -> list[str]:
    """返回问题清单（空 = 通过）。这是出片前的闸门，别绕过。"""
    problems: list[str] = []
    cap = float(cfg.clips.get("max_seconds", 10.0))
    max_share = float(cfg.clips.get("max_share", 0.45))
    ids = {str(c.get("id")): c for c in ((index or {}).get("clips") or [])}
    for pl in edl.get("scenes") or []:
        idx = pl.get("scene")
        shots = pl.get("shots") or []
        if not shots:
            problems.append(f"分镜 {idx}：一个镜头都没有")
            continue
        total = round(sum(float(s.get("dur") or 0) for s in shots), 2)
        want = float(pl.get("dur") or 0)
        if abs(total - want) > max(0.25, want * 0.02):
            problems.append(f"分镜 {idx}：镜头总长 {total}s ≠ 画面时长 {want}s")
        for s in shots:
            d = float(s.get("dur") or 0)
            kind = str(s.get("kind"))
            if d <= 0:
                problems.append(f"{s.get('slot')}：时长是 0")
            elif kind in ("clip", "reuse") and d > cap + 0.02:
                # ⚠️ 单段上限是**引用影视切片**的合规红线，只管 clip/reuse。
                #    静态图/生成图（still/generate）不受它约束 —— 老流水线里
                #    一个 25 秒的分镜就是一张图加缓移，画面并不「引用」谁。
                #    （这条一开始写成了对所有镜头生效，回归测试当场把 12.55 秒的
                #    静态镜头报成红线，才发现判据放错了位置。）
                problems.append(f"{s.get('slot')}：切片单镜头 {d}s 超过上限 {cap}s（合规红线）")
            if str(s.get("treatment")) not in TREATMENTS:
                problems.append(f"{s.get('slot')}：手法 {s.get('treatment')!r} 不认识")
            cid = str(s.get("clip_id") or "")
            if kind in ("clip", "reuse"):
                if not cid:
                    problems.append(f"{s.get('slot')}：说是用素材但没给 clip_id")
                elif ids:
                    c = ids.get(cid)
                    if c is None:
                        problems.append(f"{s.get('slot')}：素材 {cid} 不在库里")
                    elif float(s.get("src_in") or 0) + d > float(c.get("dur") or 0) + 0.05:
                        problems.append(
                            f"{s.get('slot')}：要从 {cid} 的第 {s.get('src_in')}s 起取 {d}s，"
                            f"但它只有 {c.get('dur')}s")
    share = clip_share(edl)
    if share > max_share:
        problems.append(f"切片占比 {share:.1%} 超过上限 {max_share:.1%}（合规红线）")
    return problems


def save(edl: dict, cfg: Config) -> Path:
    out_dir = cfg.paths.get_path("data_dir") / "edl"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d")
    safe = "".join(c for c in (edl.get("title") or edl.get("topic") or "未命名")
                   if c not in '\\/:*?"<>|').strip()[:28] or "未命名"
    p = out_dir / f"{stamp}_{safe}_EDL.json"
    p.write_text(json.dumps(edl, ensure_ascii=False, indent=1), encoding="utf-8")
    return p


def load(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def describe(edl: dict) -> str:
    """给人看的一行摘要（日志/终端用）。"""
    n = sum(len(p.get("shots") or []) for p in edl.get("scenes") or [])
    total = sum(float(p.get("dur") or 0) for p in edl.get("scenes") or [])
    treats: dict[str, int] = {}
    for p in edl.get("scenes") or []:
        for s in p.get("shots") or []:
            k = str(s.get("treatment"))
            treats[k] = treats.get(k, 0) + 1
    bits = "　".join(f"{TREATMENT_ZH.get(k, k)}×{v}" for k, v in sorted(treats.items()))
    return (f"{len(edl.get('scenes') or [])} 场 / {n} 个镜头 / 画面总长 {total:.0f}s　"
            f"切片占比 {clip_share(edl):.1%}　{bits}")
