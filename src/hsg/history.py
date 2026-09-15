"""生成记录：每次跑完追加一条，并据此给选题去重。

两个作用：
  1. 「做过什么」有据可查 —— data/history.json（机器读）+ data/生成记录.md（人看）
  2. 选题去重 —— 随机选题会跳过已经做过的题材；显式指定重复题材会被拦下

去重怎么判「同一个故事」：
  · 规范化后完全相同；或
  · 一方是另一方的子串（≥4 字）——「赤壁之战」vs「赤壁之战：一场大火改写了三国」
  · 或字符二元组 Jaccard 相似度 ≥ 阈值（默认 0.55）—— 措辞不同但讲的是同一件事
比较对象是每条记录的**题材**和**标题**，所以换个说法也躲不过。
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path

from .config import Config

log = logging.getLogger("hsg.history")

_SIM_THRESHOLD = 0.55


# ---------------------------------------------------------------- 基础
def history_path(cfg: Config) -> Path:
    return cfg.paths.get_path("data_dir") / "history.json"


def md_path(cfg: Config) -> Path:
    return cfg.paths.get_path("data_dir") / "生成记录.md"


def norm(s: str) -> str:
    """规范化：只留中文/字母/数字，去掉标点空格并小写。"""
    return re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9]", "", s or "").lower()


def _bigrams(s: str) -> set[str]:
    return {s[i : i + 2] for i in range(len(s) - 1)} if len(s) > 1 else set()


def similar(a: str, b: str, threshold: float = _SIM_THRESHOLD) -> bool:
    """两个题材/标题是否算「同一个故事」。

    三条判据（命中任一即算同一件事）：
      1. 规范化后完全相同
      2. 一方是另一方的子串（两边都 ≥3 字）——「鸿门宴」↔「鸿门宴上，项羽为什么没杀刘邦」
      3. 字符二元组 Jaccard 相似度 ≥ 阈值 —— 措辞不同但讲的是同一件事
    第 2 条的阈值定在 3 字：再短（如「清代」）就会误伤，
    而 3 字已经能覆盖「鸿门宴」「虎门销烟」这类常用短名。
    """
    a, b = norm(a), norm(b)
    if not a or not b:
        return False
    if a == b:
        return True
    if len(a) >= 3 and len(b) >= 3 and (a in b or b in a):
        return True
    ga, gb = _bigrams(a), _bigrams(b)
    if not ga or not gb:
        return False
    return len(ga & gb) / len(ga | gb) >= threshold


def load(cfg: Config) -> list[dict]:
    p = history_path(cfg)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        log.warning("生成记录读取失败（%s），当作空记录：%s", p.name, exc)
        return []
    return data if isinstance(data, list) else []


def save(cfg: Config, records: list[dict]) -> Path:
    p = history_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    write_markdown(cfg, records)
    return p


def record_keys(rec: dict) -> list[str]:
    """一条记录里所有可用于查重的文本。"""
    out = [rec.get("topic") or "", rec.get("title") or ""]
    return [k for k in out if k]


def used_keys(cfg: Config) -> set[str]:
    keys: set[str] = set()
    for rec in load(cfg):
        for k in record_keys(rec):
            n = norm(k)
            if n:
                keys.add(n)
    return keys


def _shared_bigrams(a: str, b: str) -> int:
    ga, gb = _bigrams(norm(a)), _bigrams(norm(b))
    return len(ga & gb)


# 虚词字表：两个虚词组成的片段（「为什么」）没有任何区分度，查重时不算数
_FUNC_CHARS = set(
    "的了是在有和就也而为什么怎会不这那其所之与或及等你我他她它们一个上下里到从对"
    "把被让使能要可还都又很最更只才已将并且但因如则者乎矣中后前时天年月日地方些"
)


def _common_runs(a: str, b: str, min_len: int = 2) -> list[str]:
    """两串的「公共片段」列表（贪心取最长，长度 ≥min_len，滤掉纯虚词片段）。

    为什么不用 Jaccard：中文里「项羽」这种二字专名和「为什么」这种虚词片段
    在二元组层面权重一样，算出来分不清。
    「楚汉之争：项羽为什么会输」对「鸿门宴上，项羽为什么没杀刘邦」：
    公共片段只有「项羽」一段 → 不同一件事；
    「鸿门一宴，项羽为何放走刘邦」对同一条：鸿门 / 项羽 / 刘邦 三段 → 同一件事。
    """
    if not a or not b:
        return []
    runs: list[str] = []
    i = 0
    while i < len(a):
        best = ""
        j = i + min_len
        while j <= len(a) and a[i:j] in b:
            best = a[i:j]
            j += 1
        if best:
            runs.append(best)
            i += len(best)
        else:
            i += 1
    return [r for r in runs if not all(c in _FUNC_CHARS for c in r)]


def related(a: str, b: str) -> bool:
    """宽松的「可能相关」预筛 —— 只做筛选，不下结论（结论交给模型）。

    判据：有 2 段以上实质性公共片段（如「鸿门」「项羽」「刘邦」）。
    这个预筛的作用是控制成本：只有沾边的候选才值得调一次模型裁定。
    """
    if not norm(a) or not norm(b):
        return False
    if similar(a, b):
        return True
    if len(norm(a)) < 4 or len(norm(b)) < 4:
        return False
    return len(_common_runs(norm(a), norm(b))) >= 2


JUDGE_SYSTEM = """你负责判断两个历史节目题材是不是「同一个故事」。

判定标准：讲的是不是同一个历史事件/同一段史实。仅仅涉及同一个朝代、
同一个人物不同的事迹、或同一场战争的不同阶段，都算**不同**的故事。

只输出 JSON：{"same": true 或 false, "reason": "一句话理由"}"""

JUDGE_USER = """题材 A：{a}
题材 B：{b}

这两个题材讲的是不是同一个历史事件？"""


def judge_same(cfg: Config, llm, a: str, b: str) -> bool:
    """让模型裁定两个题材是否同一件事（只在规则的模糊带里调用）。"""
    if llm is None:
        return False
    try:
        data = llm.chat_json(JUDGE_SYSTEM, JUDGE_USER.format(a=a, b=b),
                             max_tokens=400, temperature=0.0)
    except Exception as exc:  # noqa: BLE001
        log.warning("查重裁定调用失败（按不重复处理）：%s", exc)
        return False
    if isinstance(data, dict):
        val = data.get("same")
        if isinstance(val, bool):
            return val
        return str(val).strip().lower() in ("true", "yes", "y", "1", "是")
    return False


def find_duplicate(cfg: Config, topic: str, llm=None) -> dict | None:
    """topic 是否已经做过了？返回命中的那一条记录。

    两层判定：
      1. 规则层（免费）：完全相同 / 一方是另一方子串 / 二元组 Jaccard ≥ 0.55
      2. 模型裁定层（只在「沾边但规则判不出来」的模糊带里调一次）：
         同一件事换个说法，规则抓不住，交给 DeepSeek 判
    """
    if not topic:
        return None
    records = load(cfg)
    for rec in records:
        for k in record_keys(rec):
            if similar(topic, k):
                return rec

    if llm is not None:
        for rec in records:
            if not any(related(topic, k) for k in record_keys(rec)):
                continue
            ref = rec.get("topic") or rec.get("title") or ""
            if judge_same(cfg, llm, topic, ref):
                log.info("查重：模型判定「%s」与已做过的「%s」是同一个故事", topic, ref)
                return rec
    return None


def is_used(cfg: Config, candidate: str) -> bool:
    """给选题池用：这个候选题材是否做过了。"""
    if not candidate:
        return True
    return any(similar(candidate, k) for k in used_keys(cfg))


# ---------------------------------------------------------------- 追加
def append_record(cfg: Config, record: dict) -> tuple[Path, Path]:
    records = load(cfg)
    records.append(record)
    p = save(cfg, records)
    log.info("生成记录已更新：%s（第 %d 条）", p, len(records))
    return p, md_path(cfg)


def build_record(
    story,
    cfg: Config,
    *,
    total_seconds: float,
    video_seconds: dict | None = None,
    outputs: list[str] | None = None,
    script: str = "",
    metadata: str = "",
    material_meta: dict | None = None,
    verify_stats: dict | None = None,
    elapsed: float = 0.0,
    extra: dict | None = None,
) -> dict:
    scenes = story.all_scenes
    rec = {
        "run_id": datetime.now().strftime("%Y%m%d-%H%M%S"),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "topic": story.topic,
        "title": story.title,
        # 选题两级：查重看标题，类型用于「避开连着做同一类」
        "topic_type": story.topic_type,
        "topic_desc": story.topic_desc,
        "series": story.series,
        "series_ep": story.series_ep,
        "angle_question": story.angle_question,
        "angle_mode": str(cfg.story.get("angle_mode") or "small"),
        "period": story.period,
        "chapters": len(story.chapters),
        "scenes": len(scenes),
        "chars": sum(len(s.text or "") for s in scenes),
        "speech_seconds": round(total_seconds, 1),
        "speech_minutes": round(total_seconds / 60, 2),
        "video_seconds": video_seconds or {},
        "images": {
            "total": len(scenes),
            "found": sum(1 for s in scenes if s.image_path),
            "fallback": sum(1 for s in scenes if not s.image_path),
        },
        "verify": verify_stats or {},
        "material": material_meta or {},
        "outputs": outputs or [],
        "script": script,
        "metadata": metadata,
        "elapsed_seconds": round(elapsed, 1),
        "text_model": f"{cfg.llm.provider}/{cfg.llm[str(cfg.llm.provider)].get('model')}",
        "tts": f"{cfg.tts.provider}/{cfg.tts[str(cfg.tts.provider)].get('voice_id')}",
    }
    if extra:
        rec.update(extra)
    return rec


# ---------------------------------------------------------------- 补录
def _probe_outputs(outputs: list[str], cfg: Config | None = None) -> dict[str, float]:
    """补录时把成片时长读出来（文件已经不在了就跳过，不报错）。

    产物被挪到 archive/ 子目录时，按文件名在 output 目录下再找一次。
    """
    out: dict[str, float] = {}
    try:
        from .video import media_duration
    except Exception:  # noqa: BLE001
        return out
    out_dir = cfg.paths.get_path("output_dir") if cfg is not None else None
    for o in outputs:
        p = Path(str(o))
        if not p.exists() and out_dir is not None:
            hit = next((q for q in out_dir.rglob(p.name)), None)
            if hit is not None:
                p = hit
        if not p.exists():
            continue
        label = "portrait" if "竖屏" in p.name else ("landscape" if "横屏" in p.name else p.stem[:12])
        try:
            out[label] = round(media_duration(p), 2)
        except Exception:  # noqa: BLE001
            continue
    return out


def backfill_from_metadata(cfg: Config) -> int:
    """把 data/output 下已存在的 metadata.json 补进记录（历史产物较多时用）。

    以 metadata 里的生成时间为准，已存在的 run_id 不会重复添加。
    """
    out_dir = cfg.paths.get_path("output_dir")
    records = load(cfg)
    have = {r.get("run_id") for r in records} | {norm(r.get("title") or "") for r in records}
    added = 0
    files = sorted(out_dir.rglob("*_metadata.json"))
    for f in files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        # 只写过稿、没出语音/成片的试跑（plan）不算一期
        if data.get("plan_only"):
            continue
        title = str(data.get("title") or "")
        if not title or norm(title) in have:
            continue
        ts = str(data.get("generated_at") or "")
        rid = re.sub(r"[^0-9]", "", ts)[:14] or f"backfill-{added + 1}"
        if rid in have:
            continue
        scenes = [s for c in data.get("chapters") or [] for s in c.get("scenes") or []]
        timeline = data.get("timeline") or []
        rec = {
            "run_id": rid,
            "generated_at": ts,
            "topic": data.get("topic") or "",
            "topic_type": data.get("topic_type") or "",
            "series": data.get("series") or "",
            "series_ep": data.get("series_ep") or 0,
            "topic_desc": data.get("topic_desc") or "",
            "title": title,
            "period": data.get("period") or "",
            "chapters": len(data.get("chapters") or []),
            "scenes": len(scenes),
            "chars": sum(len(str(s.get("text") or "")) for s in scenes),
            "speech_seconds": round(float(data.get("total_seconds") or 0), 1),
            "speech_minutes": round(float(data.get("total_seconds") or 0) / 60, 2),
            "video_seconds": _probe_outputs(data.get("outputs") or [], cfg),
            "images": {"total": len(scenes),
                       "found": sum(1 for s in scenes if s.get("image")),
                       "fallback": sum(1 for s in scenes if not s.get("image"))},
            "verify": (data.get("verify") or {}),
            "material": (data.get("material") or {}),
            "outputs": list(data.get("outputs") or []),
            "script": "",
            "metadata": str(f),
            "elapsed_seconds": 0.0,
            "backfilled": True,
            "text_model": "",
            "tts": "",
        }
        records.append(rec)
        have.add(norm(title))
        have.add(rid)
        added += 1
        log.info("补录：%s（%s，%.2f 分钟）", title, timeline and "有分镜表" or "", rec["speech_minutes"])
    if added:
        records.sort(key=lambda r: str(r.get("generated_at") or ""))
        save(cfg, records)
        log.info("共补录 %d 条，记录总数 %d 条", added, len(records))
    else:
        log.info("没有需要补录的记录（已存在 %d 条）", len(records))
    return added


# ---------------------------------------------------------------- 人类可读版
def write_markdown(cfg: Config, records: list[dict]) -> Path:
    """生成 markdown 表格 —— 这是给人看的版本。"""
    lines = [
        "# 历史小故事 · 生成记录",
        "",
        f"累计 {len(records)} 期。机器可读的完整版在同目录 `history.json`（选题去重就靠它）。",
        "",
        "| # | 生成时间 | 标题 | 题材 | 时长 | 配音时长 | 字数 | 分镜 | 配图 | 校验 |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for i, r in enumerate(records, 1):
        im = r.get("images") or {}
        vf = r.get("verify") or {}
        vtxt = []
        if vf.get("rule_fail") is not None:
            vtxt.append(f"规则 {vf.get('rule_fail', 0)}/{vf.get('rule_warn', 0)}")
        if vf.get("audit_issues") is not None:
            vtxt.append(f"审校 {vf.get('audit_issues', 0)}")
        vs = r.get("video_seconds") or {}
        vsec = max(vs.values()) if vs else None
        lines.append(
            f"| {i} | {str(r.get('generated_at') or '')[:16].replace('T', ' ')} "
            f"| {r.get('title') or ''} | {r.get('topic') or ''} "
            f"| {(f'{vsec/60:.2f}' if vsec else '-')} 分 "
            f"| {float(r.get('speech_minutes') or 0):.2f} 分 "
            f"| {r.get('chars') or 0} | {r.get('scenes') or 0} "
            f"| {im.get('found', '-')}/{im.get('total', '-')} "
            f"| {' '.join(vtxt) or '-'} |"
        )
    lines += [
        "",
        "## 明细",
        "",
    ]
    for i, r in enumerate(records, 1):
        lines.append(f"### {i}. {r.get('title') or '(无标题)'}")
        lines.append("")
        lines.append(f"- 生成时间：{str(r.get('generated_at') or '')[:19].replace('T', ' ')}")
        lines.append(f"- 题材：{r.get('topic') or '-'}（年代：{r.get('period') or '-'}）")
        lines.append(f"- 结构：{r.get('chapters')} 章 / {r.get('scenes')} 个分镜 / {r.get('chars')} 字")
        lines.append(f"- 配音：{float(r.get('speech_minutes') or 0):.2f} 分钟（{r.get('speech_seconds')} 秒）")
        if r.get("video_seconds"):
            for k, v in (r["video_seconds"] or {}).items():
                lines.append(f"- 成片（{k}）：{float(v):.1f} 秒 = {float(v)/60:.2f} 分钟")
        for o in r.get("outputs") or []:
            lines.append(f"- 文件：{o}")
        if r.get("text_model"):
            lines.append(f"- 模型：文本 {r.get('text_model')}　语音 {r.get('tts')}")
        lines.append("")
    p = md_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(lines), encoding="utf-8")
    return p
