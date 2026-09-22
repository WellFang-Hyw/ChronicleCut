"""阶段 B：逐章写稿，并输出「分镜」粒度。

一个分镜（scene）= 一段口播 + 一张配图 + 一条字幕。
为什么按分镜写而不是整章一段：整章 70 秒只配一张图会显得很呆，
而且 TTS 整章合成后要把音频切开对齐画面切换点，切口容易切在字中间。
按分镜合成则天然的「一段文字 = 一段语音 = 一张图 = 一片字幕」，边界严丝合缝。
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor

from .config import Config
from .llm import LLM
from .models import Chapter, Scene, Story
from .textfmt import sanitize_narration, strip_stage_marks

log = logging.getLogger("hsg.writer")

SYSTEM_HEAD = """你是历史故事的讲述者，写的是「给人听」的口播稿，不是给人看的文章。

通用写作要求：
1. 史实优先。人名、年份、地名、事件因果必须真实；
   不确定的细节不要编具体数字，用「史书记载不一」「据说」收口。
2. 口语化、短句为主、有节奏。念出来要顺，不要书面语堆砌。
3. 不要用「首先」「其次」「本节」「接下来我给大家讲讲」这类过渡腔。
4. 不要出现任何网站名、百科名、「资料显示」等来源表述；
   要引用就用「据史料记载」「史书上说」。
5. 不要写 markdown 标记（不要 ** 不要 #），不要写括注说明，不要写舞台提示。
6. 全部简体中文。数字用阿拉伯数字（208 年、十万），日期不要补零（写「9月10日」不写「09月10日」）。
7. **层次与承接**（2026-09-17 用户指定）：每章第一句要接住上一章结尾留下的问题；
   章内先讲**具体场景**（谁、在哪、做什么、手里拿着什么），再讲清机制与因果，
   最后落回人的处境与代价。不要一上来就讲道理，也不要把结论提前抖出来。
8. **语义连贯**：同一事物前后用同一个词（别一会儿「辅政大臣」一会儿「托孤重臣」）；
   「他」「这件事」这类指代必须找得到先行词；不要为了排比重复同一句式。
   一段话一个意思，写完读一遍：念出来会不会绊住。
9. **复述古文要释义**（2026-09-22 用户指定）：引用史书原文时，先照引原句，
   紧接着用大白话说明其含义，不要只引不释 —— 听众听不懂就白引了。
   例：「史书上说『受遗诏辅政』，就是说皇帝临终前把身后事托付给了他」。
10. **不要用生僻词**（2026-09-22 用户指定）：避开读起来不易懂的文言难词、
    生僻字、冷僻典故。宁可改写成大白话，也不要让听众在某个词上绊住。
    「万机」写「所有政务」，「崩殂」写「去世」，「薨」写「故去」。
11. **易于生成语音**（2026-09-22 用户指定）：写完读一遍，确保 TTS 能顺畅通读 ——
    句子短、断句自然、无拗口词组、无连续四字以上的成语堆叠。"""

SYSTEM_ANGLE_SMALL = """

【本期是小切口提问型：用史实回答一个具体的日常问题】
1. **每段都要有具体信息**：一个价格、一段里程、几天的路程、一件器物、
   一个具体的动作或一句史料原话。空泛的抒情句、总结句不要写。
2. **始终扣住本期那个问题**：跑题到通史、扯到不相关的大事，一律不要。
3. **用具体的人承载**：尽量落到某个具体身份的人（一个被流放的官员、一个跑驿站的马夫、
   一个赶考的考生）身上，由他的处境把制度和数字带出来。
4. **数字只能来自大纲给的史实锚点（facts）**：大纲 facts 里写了的才能写；
   没写的、拿不准的，一律不要给精确数字，更不许说成「法定标准」「按《某某会典》规定」。
   编一个看上去合理的数字（「每天走五十里」「口粮折银五厘」「枷重十五斤」）是这类题材
   最容易犯、也最容易被审查出来的错，会使整段内容失去可信度。
5. **「没有记载」也是答案**：把「律例只规定了什么、没规定什么」「各地记载不一致」
   「只有某地某时留下过记录」讲清楚，比一个编出来的数字更可信、更有信息量，
   也是这类题材真正有意思的地方。该说的时候就说：
   「律例里并没有规定一天要走多少里，实际快慢全看路况、天气和押解的差役」。
6. **别用现代概念套古代**：不要写「社保」「公务员」「物流」这种词去替代当时的说法，
   要用当时的词汇（俸禄、差役、驿传、漕运），可以用「相当于今天……」帮助理解，但一句话带过。"""

SYSTEM_ANGLE_EVENT = """

【本期是事件型：讲清一件历史大事的来龙去脉】
· 用具体的人物动作与场景推进，不要停留在事件概述
· 关键数字（兵力、钱粮、时间）要真实"""


def system_for(cfg) -> str:
    from .outline import mode_of

    return SYSTEM_HEAD + (SYSTEM_ANGLE_SMALL if mode_of(cfg) == "small" else SYSTEM_ANGLE_EVENT)

_SCHEMA = """{
  "scenes": [
    {
      "text": "这一段的口播稿（目标 %d-%d 字，也就是约 %.0f 秒）",
      "image_query": "这一段的配图检索词（具体到实物/场景/遗址/古画，容易搜到图）",
      "caption": "画面上的小字图注（10-16 字，可为空字符串）"
    }
  ]
}"""


def _facts_block(chapter) -> str:
    """把大纲给的史实锚点列成块；没有就明确说「不许编数字」。"""
    facts = [f for f in (getattr(chapter, "facts", None) or []) if f]
    if facts:
        return "\n".join(f"· {f}" for f in facts)
    return "（本章没有给出史实锚点 —— 因此不要写任何具体数字，"\
           "该说「史书没有留下记载」就直说）"


def _scene_target(cfg: Config) -> tuple[int, int, int]:
    st = cfg.story
    cps = float(st.chars_per_second)
    sec = float(st.seconds_per_scene)
    mid = int(sec * cps)
    return int(mid * 0.8), int(mid * 1.3), int(sec)


def _scenes_from(data: dict, chapter: Chapter, story: Story, start_index: int) -> list[Scene]:
    scenes: list[Scene] = []
    for k, s in enumerate(data.get("scenes") or []):
        if not isinstance(s, dict):
            continue
        text = sanitize_narration(strip_stage_marks(str(s.get("text") or "")))
        if len(text) < 20:
            continue
        scenes.append(
            Scene(
                index=start_index + len(scenes),
                text=text,
                image_query=str(s.get("image_query") or "").strip()
                or (chapter.image_queries[0] if chapter.image_queries else chapter.heading),
                caption=sanitize_narration(str(s.get("caption") or ""))[:24],
                chapter_index=chapter.index,
                is_chapter_start=(k == 0),
            )
        )
    return scenes


def write_chapter(chapter: Chapter, story: Story, cfg: Config, llm: LLM, start_index: int) -> Chapter:
    lo, hi, sec = _scene_target(cfg)
    n_scenes = max(1, round(chapter.seconds / max(1.0, sec)))
    mat = f"\n【参考素材（检索摘要，可能有噪声，只用于核对人名与年份）】\n{story.material[:3000]}\n" if story.material.strip() else ""
    prev = ""
    if chapter.index > 1:
        prev_ch = story.chapters[chapter.index - 2]
        prev = f"\n【上一章讲过的内容（不要重复）】{prev_ch.summary}\n"

    user = f"""请为下面这一章写口播稿，切成 {n_scenes} 个分镜。

【本期标题】{story.title}
【本期要回答的问题】{story.angle_question or story.topic}
【主题】{story.topic}
【年代】{story.period}
【本章】第 {chapter.index} 章《{chapter.heading}》
【本章要点】{chapter.summary}
【本章必须依托的史实（只能用这些；没写进来的数字一律不要给具体值）】
{_facts_block(chapter)}
【本章目标时长】约 {chapter.seconds} 秒，合计约 {int(chapter.seconds * float(cfg.story.chars_per_second))} 字
{prev}{mat}
【分镜要求】
· 切成 {n_scenes} 个分镜，每段 {lo}-{hi} 字（约 {sec} 秒）
· 每段自成一个小画面：讲一个场景、一个动作或一个转折
· 每段至少带一个具体信息（器物 / 动作 / 制度规定 / 上面列出的史实）
· 只写本章内容，不要重复上一章，也不要提前把后面章节的内容讲完
· 每段给一个配图检索词（具体、可搜到实物图/古画/遗址）

【输出】只输出 JSON：
{_SCHEMA % (lo, hi, float(sec))}"""

    try:
        data = llm.chat_json(
            system_for(cfg),
            user,
            max_tokens=6000,
            temperature=float(cfg.llm.get("writer_temperature", 0.65)),
        )
    except Exception as exc:  # noqa: BLE001
        log.error("第 %d 章写稿失败：%s", chapter.index, exc)
        chapter.scenes = []
        return chapter

    chapter.scenes = _scenes_from(data if isinstance(data, dict) else {}, chapter, story, start_index)
    if not chapter.scenes:
        log.error("第 %d 章写稿返回空分镜", chapter.index)
    else:
        chars = sum(s.chars for s in chapter.scenes)
        log.info("第 %d 章《%s》%d 个分镜 / %d 字（目标 %d 秒，估 %.0f 秒）",
                 chapter.index, chapter.heading, len(chapter.scenes), chars,
                 chapter.seconds, chars / max(0.1, float(cfg.story.chars_per_second)))
    return chapter


def write_all(story: Story, cfg: Config, llm: LLM) -> Story:
    """并行写全部章节。"""
    workers = max(1, int(cfg.llm.get("concurrency", 4)))
    idx = 1
    starts: list[int] = []
    for ch in story.chapters:
        starts.append(idx)
        idx += max(1, round(ch.seconds / max(1.0, float(cfg.story.seconds_per_scene))))

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [
            pool.submit(write_chapter, ch, story, cfg, llm, starts[i])
            for i, ch in enumerate(story.chapters)
        ]
        for f in futures:
            f.result()

    # 章节返回顺序可能乱，重排并按顺序重编全局序号
    story.chapters.sort(key=lambda c: c.index)
    n = 1
    for ch in story.chapters:
        for s in ch.scenes:
            s.index = n
            n += 1
        for k, s in enumerate(ch.scenes):
            s.is_chapter_start = k == 0
    total = sum(s.chars for s in story.all_scenes)
    log.info("写稿完成：%d 章 / %d 个分镜 / %d 字（约 %.1f 分钟）",
             len(story.chapters), len(story.all_scenes), total,
             total / max(0.1, float(cfg.story.chars_per_second)) / 60)
    return story


# ------------------------------------------------------------------ 长度自适应
def rewrite_for_length(
    chapter: Chapter,
    story: Story,
    cfg: Config,
    llm: LLM,
    target_seconds: float,
    *,
    feedback: str = "",
) -> Chapter:
    """把某一章改写（扩写 / 精简）到指定时长。

    扩写比精简难 —— 模型扩写时容易灌水（重复、空话），所以提示词里
    明确要求「补细节、补场景、补因果」，而不是「多写点形容词」。
    """
    cur = chapter.duration or sum(s.chars for s in chapter.scenes) / max(
        0.1, float(cfg.story.chars_per_second)
    )
    expand = target_seconds > cur
    lo, hi, sec = _scene_target(cfg)
    n_scenes = max(1, round(target_seconds / max(1.0, sec)))
    target_chars = int(target_seconds * float(cfg.story.chars_per_second))
    direction = "扩写" if expand else "精简"
    body = "\n".join(s.text for s in chapter.scenes)

    user = f"""下面这一章的口播稿需要{direction}。

【本期要回答的问题】{story.angle_question or story.topic}
【本期主题】{story.topic}（{story.period}）
【本章】第 {chapter.index} 章《{chapter.heading}》
【本章要点】{chapter.summary}
【本章必须依托的史实（只能用这些；没写进来的数字一律不要给具体值）】
{_facts_block(chapter)}
【当前字数】{len(body)} 字（约 {cur:.0f} 秒）
【目标】约 {target_seconds:.0f} 秒，约 {target_chars} 字，切成 {n_scenes} 个分镜
【当前稿子】
{body}
{feedback}
【{direction}要求】
{"· 补的是细节与场景：具体的人物动作、对话、器物、天气、地理、因果链条；不要重复已有句子，不要加空泛抒情。" if expand else "· 砍的是重复与铺陈：合并同类句子，删掉与主线无关的枝节，保留所有关键史实与转折。"}
· 保持原有史实与人物不变，不要新增没有史料支撑的年份或人名。
· 仍然切成 {n_scenes} 个分镜，每段 {lo}-{hi} 字。

【输出】只输出 JSON：
{_SCHEMA % (lo, hi, float(sec))}"""

    data = llm.chat_json(
        system_for(cfg),
        user,
        max_tokens=6000,
        temperature=float(cfg.llm.get("writer_temperature", 0.65)),
    )
    new_scenes = _scenes_from(data if isinstance(data, dict) else {}, chapter, story, 0)
    if new_scenes:
        chapter.scenes = new_scenes
        log.info("第 %d 章已%s：%.0f 秒 → %d 字（目标 %.0f 秒）",
                 chapter.index, direction, cur, sum(s.chars for s in chapter.scenes),
                 target_seconds)
    else:
        log.warning("第 %d 章%s失败，保留原稿", chapter.index, direction)
    return chapter
