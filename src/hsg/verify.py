"""史实校验。

为什么必须做：历史叙事最容易出的错是「年份对不上」「人物张冠李戴」
「年代穿越」。提示词约束会漏，所以这里做两层：

  第一层（零成本、程序化）
    · 结构检查：空分镜、过短/过长分镜、重复句子、占位符残留、markdown 残留
    · 年份区间检查：文中年份是否落在故事声明的年代区间附近
    · 人名一致性：同一个名字前后写法是否一致
    · 敏感词检查：清洗后不应再有「百科/维基/百度」这类来源词

  第二层（花 token、LLM 审校）
    让模型以「史实校对员」身份，对照年代区间与素材，逐条挑出**具体**错误，
    只报能确证的问题；命中的分镜带着问题重写一次。
"""

from __future__ import annotations

import logging
import re

from .config import Config
from .llm import LLM
from .models import Story
from .textfmt import extract_names, extract_years, normalize_year

log = logging.getLogger("hsg.verify")

_FORBIDDEN = ("百科", "维基", "wiki", "百度", "知乎", "公众号", "百家号")
_PLACEHOLDER = ("（略）", "(略)", "XXX", "xxx", "待补充", "此处", "n/a", "N/A", "TODO")


def _norm(s: str) -> str:
    return re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9]", "", s or "")


def rule_check(story: Story, cfg: Config) -> list[dict]:
    """返回 issue 列表：[{scene, kind, detail}]。kind=fail 需要处理，warn 只提示。"""
    issues: list[dict] = []

    # ---- 结构
    for ch in story.chapters:
        if not ch.scenes:
            issues.append({"scene": 0, "kind": "fail", "detail": f"第 {ch.index} 章没有分镜"})
        for s in ch.scenes:
            if s.chars < 30:
                issues.append({"scene": s.index, "kind": "fail",
                               "detail": f"分镜过短（{s.chars} 字）：{s.text[:30]}"})
            elif s.chars > 320:
                issues.append({"scene": s.index, "kind": "warn",
                               "detail": f"分镜过长（{s.chars} 字），画面停留会偏久"})
            for bad in _FORBIDDEN:
                if bad in s.text:
                    issues.append({"scene": s.index, "kind": "fail",
                                   "detail": f"出现来源词「{bad}」：{s.text[:40]}"})
            for bad in _PLACEHOLDER:
                if bad in s.text:
                    issues.append({"scene": s.index, "kind": "fail",
                                   "detail": f"疑似占位符「{bad}」：{s.text[:40]}"})
            if re.search(r"\*{1,2}|^#{1,6}\s", s.text, re.M):
                issues.append({"scene": s.index, "kind": "fail",
                               "detail": f"markdown 标记残留：{s.text[:40]}"})

    # ---- 重复句子（模型自重复是真实故障）
    # 阈值 10 字：整句完全相同才算重复，低于这个长度会误伤常用短语
    seen: dict[str, int] = {}
    for s in story.all_scenes:
        for sent in re.split(r"[。！？；;]", s.text):
            key = _norm(sent)
            if len(key) < 10:
                continue
            if key in seen and seen[key] != s.index:
                issues.append({"scene": s.index, "kind": "fail",
                               "detail": f"与分镜 {seen[key]} 重复的句子：{sent[:30]}"})
            else:
                seen.setdefault(key, s.index)

    # ---- 年份区间
    if bool(cfg.verify.get("year_range_check", True)) and story.period_start:
        lo = min(story.period_start, story.period_end) - 200
        hi = max(story.period_start, story.period_end) + 60
        for s in story.all_scenes:
            for y in extract_years(s.text):
                if lo <= y <= hi:
                    continue
                issues.append({"scene": s.index, "kind": "warn",
                               "detail": f"年份 {normalize_year(y)} 超出所述年代"
                                         f"（{normalize_year(story.period_start)}~"
                                         f"{normalize_year(story.period_end)}）范围，"
                                         f"请确认是否笔误"})

    # ---- 人名一致性
    # ⚠️ 默认关闭。原因：中文没有词边界，用 n-gram 粗抽专名会把「曹操还白」
    #    「州的水军」这种跨词切分当成名字，在第一次实跑就产生了 16 条全是误报的
    #    提示（真名「曹操」出现 19 次，任何含它的长 n-gram 都会被判成"写法不统一"）。
    #    真正抓人名错误靠 llm_audit —— 它的提示词里已经专门要求检查
    #    「同一个人是否前后写法不一致」。这个开关留着只为做实验。
    if bool(cfg.verify.get("name_consistency_check", False)):
        text = story.text
        names = extract_names(text, limit=60)
        for n in names:
            if len(n) < 3:
                continue
            cnt = text.count(n)
            if cnt > 1:
                continue
            for prefix in (n[:2], n[1:3]):
                if len(prefix) < 2 or prefix == n:
                    continue
                if text.count(prefix) >= 3:
                    issues.append({"scene": 0, "kind": "warn",
                                   "detail": f"「{n}」只出现 1 次，而「{prefix}」出现 "
                                             f"{text.count(prefix)} 次（可能是跨词切分，需人工确认）"})
                    break

    return issues


_AUDIT_SYSTEM = """你是严谨的史实校对员，负责审校一档历史故事音频节目的口播稿。

你会拿到：故事年代区间、检索到的参考资料（可能有噪声）、以及分镜稿。
你的任务是找出**具体的史实错误**：年份错误、人物张冠李戴、官职/地名错位、
事件的先后顺序或因果关系错误、以及明显的年代穿越（讲了那个时代还不存在的东西）。

请特别检查这几类高频错误：
1. 人物张冠李戴、官职写错（如「左督/右督」写成「左都督/右都督」这类后世官称）
2. 地名错位或与前后文自相矛盾（同一件事的地点在两处写得不一样）
3. 进军方向/地理方位说反（顺流 vs 逆流、东下 vs 西上）
4. 事件时间线前后颠倒，或把后来的结果说成当场发生（如把「三分天下雏形」
   说成「三国鼎立已经形成」）
5. 同一个人前后写法不一致（本名/字/爵位混用导致指代混乱）
6. **具体数字是否有史料依据** —— 价格、里程、天数、人数、年限、损耗率这类数字
   是「小切口」题材的核心内容，也最容易被编成一个看起来合理的数。
   凡是拿不出出处、又给得很精确的数字（如「一天走八十里」「一年俸禄四十五两」），
   要重点核；确实无从查证的，宁可让稿子说「史书没有留下准确数字」。

要求：
1. 只报你能确证的问题。不确定的一律不报 —— 宁可漏报，不要误报。
2. 文风、节奏、口语化程度不属于校对范围，不要评论。
3. 参考资料有噪声（SEO 摘要、游戏站内容），不要因为「资料里没写」就判定错误。
4. 每条问题必须指明是第几个分镜、错在哪里、应该改成什么。"""

_AUDIT_SCHEMA = """{
  "issues": [
    {
      "scene_index": 分镜序号（整数）,
      "severity": "high" | "low",
      "wrong_text": "稿子里错的那几个字，照抄原文（说不出来就留空字符串）",
      "problem": "错在哪里（一句话）",
      "fix": "应该怎么改（给出正确的表述）"
    }
  ]
}"""


# 审校器自己判了"没问题"却仍把条目返回的标记。实测它至少用过两种说法：
#   「…此条不报。」「此条无误。」—— 调用方原来一律写进「待人工核对」清单，
#   于是某期 12 条里 6 条是这种噪声、另一轮 21 条里 18 条是，清单看着唬人。
# 三轮实测出现过三种说法：此条不报 / 此条无误 / 无需修改 —— 还会再变，所以宁宽勿窄。
_NOISE_MARKERS = ("此条不报", "此条无误", "此条无须报", "不必报", "此表述正确", "无误，此条",
                  "无需修改", "不需修改", "无须修改", "不需要修改")


def triage(items: list[dict]) -> list[dict]:
    """把审校结果里"它自己都说没问题"的条目丢掉，只留真要人看的。"""
    out: list[dict] = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        detail = str(it.get("detail") or "")
        if any(m in detail for m in _NOISE_MARKERS):
            continue
        out.append(it)
    return out


def llm_audit(story: Story, cfg: Config, llm: LLM) -> list[dict]:
    """LLM 史实审校。返回 issue 列表（可能为空）。

    这里有一道**可执行性过滤**：审校员必须指出「稿子里错的那几个字」
    （wrong_text）。实跑遇到过这种输出：
        「曹无伤的官职写错，应为「左司马」而非「左司马」——原文就是「左司马」」
    —— 说不出错在哪、改法又和原文一样。这种条目没法执行，还会带着
    一句无意义的「务必改正」去重写章节，所以直接丢掉。
    """
    if not bool(cfg.verify.get("llm_audit", True)):
        return []
    body = "\n".join(f"[{s.index}] {s.text}" for s in story.all_scenes)
    by_idx = {s.index: s for s in story.all_scenes}
    mat = story.material[:3000] if story.material.strip() else "（无，仅凭你的历史知识判断）"
    user = f"""【年代区间】{story.period}（约 {story.period_start} ~ {story.period_end} 年，公元前为负）
【本期主题】{story.topic}
【参考资料（可能有噪声）】
{mat}

【分镜稿】
{body}

【输出】只输出 JSON，没问题就返回空数组：
{_AUDIT_SCHEMA}"""

    try:
        data = llm.chat_json(_AUDIT_SYSTEM, user, max_tokens=3000, temperature=0.1)
    except Exception as exc:  # noqa: BLE001
        log.warning("史实审校调用失败：%s", exc)
        return []
    items = data.get("issues") if isinstance(data, dict) else data
    out: list[dict] = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        try:
            idx = int(it.get("scene_index") or 0)
        except (TypeError, ValueError):
            idx = 0
        sev = str(it.get("severity") or "low").lower()
        if sev not in ("high", "low"):
            sev = "low"
        wrong = str(it.get("wrong_text") or "").strip()
        fix = str(it.get("fix") or "").strip()
        # ---- 可执行性过滤
        if not wrong:
            log.info("审校条目无具体错误原文，跳过（不可执行）：%s", str(it.get("problem"))[:50])
            continue
        if fix and fix == wrong:
            log.info("审校条目的改法与原句相同，跳过：%s", wrong[:30])
            continue
        scene = by_idx.get(idx)
        if scene and wrong and wrong not in scene.text:
            log.info("审校条目指出的「%s」在分镜 %d 里找不到，跳过（疑似审校员幻觉）",
                     wrong[:20], idx)
            continue
        out.append({
            "scene": idx,
            "kind": "fail" if sev == "high" else "warn",
            "detail": f"「{wrong}」→ {it.get('problem')}｜建议：{fix}",
            "source": "llm",
        })
    return out


_FACT_SYSTEM = """你是史料考据校对员。有人给一档历史节目写了「史实锚点」，
每条都声称有出处（如《大清律例》《户部则例》《吉林通志》、乾隆年间奏折等）。

你要逐条判断这条锚点能不能站得住，特别是**引用出处是否成立**：

  ok      该说法与出处基本相符，或属于学界公认的通识（无出处也成立）
  unsure  你无法确证：出处可能没有这一条，或数字/条文记不准，或学界有争议
  wrong   明确错误：出处与内容不符、把不同时代的事混在一起、该文献根本不存在此规定

判断标准要分清两类，不要一律从严：

【可判 ok】制度层面的通行描述与学界共识（无需逐字条文佐证），例如
  · 「清代流犯由沿途州县按站递解，配所编入当地户籍/保甲」
  · 「漕运由官府组织，沿途有闸坝、浅铺、催趱等一套管理」
  · 「清代科举分乡试、会试、殿试三级」
  · 著名事件、著名人物的生平要点
  —— 这类你熟悉就判 ok，不要因为「背不出原文」而否掉。

【判 unsure】满足任一条：
  · 给了**具体数字**（每天多少里、口粮几升、枷重几斤、折银几钱、几成死亡率、
    多少名差役），而你无法确认该文献真有此数字
  · 对某条**具体条文/奏折/笔记**做逐字引用，你无法确认该文献确有这段话
  · 把泛称当正式规定（如把俗称的「流放三千里」当驿道里数）
  · 学界有争议，或你只有模糊印象

【判 wrong】明确错误：把不同时代的事混在一起；文献性质搞错
  （如把成书于道光年间的《刑案汇览》说成乾隆年间的笔记）；
  该文献根本不存在此规定且与通行认识相反。

只输出 JSON：{"facts": [{"index": 序号, "verdict": "ok|unsure|wrong", "note": "一句话理由"}]}"""

_FACT_USER = """请逐条核查下面的史实锚点。

{facts}

【输出】只输出 JSON（index 就是上面的序号）：
{{"facts": [{{"index": 1, "verdict": "ok", "note": "理由"}}]}}"""


def audit_facts(story: Story, cfg: Config, llm: LLM) -> dict:
    """写稿前的「锚点核查」：把编造的出处挡在成稿之前。

    小切口题材靠具体史实撑起来，模型会顺手编一个看起来合理的出处
    （实跑出现过「《户部则例》记载递解人犯每日给米一升」）。
    这一关在写稿之前逐条核查锚点：
      · ok     保留
      · unsure 降级标注（写稿时只能用模糊说法，不许引这个出处和数字）
      · wrong  直接删掉
    返回统计信息，供记录与日志使用。
    """
    if not bool(cfg.verify.get("fact_audit", True)):
        return {"total": 0, "ok": 0, "unsure": 0, "wrong": 0}

    items: list[tuple[int, int, str]] = []       # (chapter.index, 章内序号, 内容)
    for ch in story.chapters:
        for i, f in enumerate(ch.facts, start=1):
            items.append((ch.index, i, f))
    if not items:
        return {"total": 0, "ok": 0, "unsure": 0, "wrong": 0}

    lines = [f"[{n}] （第{ci}章）{txt}" for n, (ci, _i, txt) in enumerate(items, start=1)]
    try:
        data = llm.chat_json(_FACT_SYSTEM, _FACT_USER.format(facts="\n".join(lines)),
                             max_tokens=3000, temperature=0.0)
    except Exception as exc:  # noqa: BLE001
        log.warning("锚点核查调用失败（锚点全部按原样保留）：%s", exc)
        return {"total": len(items), "ok": len(items), "unsure": 0, "wrong": 0, "error": str(exc)}

    verdicts: dict[int, tuple[str, str]] = {}
    for it in (data.get("facts") if isinstance(data, dict) else data) or []:
        if not isinstance(it, dict):
            continue
        try:
            n = int(it.get("index") or 0)
        except (TypeError, ValueError):
            n = 0
        v = str(it.get("verdict") or "").strip().lower()
        if v not in ("ok", "unsure", "wrong"):
            v = "unsure" if v else "unsure"
        verdicts[n] = (v, str(it.get("note") or "").strip())

    stat = {"total": len(items), "ok": 0, "unsure": 0, "wrong": 0}
    for n, (ci, _i, txt) in enumerate(items, start=1):
        ch = next((c for c in story.chapters if c.index == ci), None)
        if ch is None:
            continue
        verdict, note = verdicts.get(n, ("unsure", "未返回结论，按存疑处理"))
        if verdict == "ok":
            stat["ok"] += 1
            continue
        if verdict == "wrong":
            stat["wrong"] += 1
            log.warning("锚点核查：删除（出处/内容不成立）第%d章「%s」——%s", ci, txt[:40], note)
            ch.facts = [f for f in ch.facts if f != txt]
            continue
        stat["unsure"] += 1
        log.info("锚点核查：存疑→降级 第%d章「%s」——%s", ci, txt[:40], note)
        # 不删除，但明确告诉写稿：不许引这个出处与数字，只能说「记载不一 / 没明确规定」
        ch.facts = [
            (f"【存疑，不要引用其出处与具体数字，只能写成「史料记载不一 / 没有明确规定」】{f}"
             if f == txt else f)
            for f in ch.facts
        ]
    log.info("锚点核查：共 %d 条 → 确认 %d、存疑降级 %d、删除 %d",
             stat["total"], stat["ok"], stat["unsure"], stat["wrong"])
    return stat


class VerifyFailed(RuntimeError):
    """复检后仍有必修的史实问题，且配置要求「有问题就不出片」。"""


def final_check(story: Story, cfg: Config, llm=None) -> dict:
    """写稿（含改写）之后的**最终校验**。

    为什么必须有这一步：审校发现问题 → 带纠正意见重写章节 → 但重写的结果
    从来没有被再验一遍。没有复检，就等于「修没修好全靠猜」。
    这一步在 TTS 之前跑：规则层 + 审校层各一遍，给出仍未解决的问题清单。

    返回 {"rule": [...], "llm": [...], "fails": [...], "warns": [...]}
    """
    rule_issues = rule_check(story, cfg)
    llm_issues = llm_audit(story, cfg, llm) if llm is not None else []
    fails = [i for i in rule_issues + llm_issues if i["kind"] == "fail"]
    warns = [i for i in rule_issues + llm_issues if i["kind"] == "warn"]
    return {"rule": rule_issues, "llm": llm_issues, "fails": fails, "warns": warns}


def report(issues: list[dict]) -> tuple[int, int]:
    """打印问题清单，返回 (fail 数, warn 数)。"""
    fails = [i for i in issues if i["kind"] == "fail"]
    warns = [i for i in issues if i["kind"] == "warn"]
    for i in fails:
        log.warning("✗ 分镜 %s：%s", i["scene"] or "-", i["detail"])
    for i in warns:
        log.info("! 分镜 %s：%s", i["scene"] or "-", i["detail"])
    log.info("校验结果：%d 个必须处理，%d 个提示", len(fails), len(warns))
    return len(fails), len(warns)
