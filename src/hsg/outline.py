"""阶段 A：选题 + 分章大纲。

切入方式由 config.yaml 的 story.angle_mode 决定：

  small（默认）小切口 ——整期只回答一个具体的日常问题，
                用史实把这个问题的答案讲成故事。
                例：「流放三千里：被流放的人一路上靠什么活下来？」
  event        事件式 ——讲一场战役/一次变法的来龙去脉（旧行为）。

为什么要跟「写稿」分成两次 LLM 调用：
  · 一次调用既要决定讲什么、又要写出全部稿件，输出太长容易被截断；
  · 分章后写稿可以并行（4 路并发），实测从 90 秒降到 20 秒级别；
  · 大纲先固定骨架，长度控制（章节数 / 每章秒数）才有地方下手。
"""

from __future__ import annotations

import logging

from .config import Config
from .llm import LLM
from .models import Chapter, Story

log = logging.getLogger("hsg.outline")

SYSTEM_HEAD = """你是资深历史纪录片撰稿人，擅长把史料讲成有画面感、有悬念的故事。

通用硬性要求：
1. 只讲有史料支撑的内容。人名、年份、地名、事件因果、数字（价格/里程/天数/人数）
   必须真实；细节不确定时用「史书记载不一」「据说」收口，绝不虚构具体数字与人名。
2. 叙事推进靠因果和悬念，不要写成知识点罗列或教科书条目。
3. 章节之间要有承接的钩子，听众要能顺着听下去。
4. 每一章都要给出配图检索词：
   · **中文检索词**（image_queries）：2-3 个，具体到实物/场景/地点，例如
     「明代 驿站 马匹 古画」「漕船 出土文物」「清代 枷锁 刑具 博物馆藏品」
     不要抽象词（「历史」「古代」），不要现代名词。
   · **英文检索词**（image_queries_en）：2-3 个，用于检索博物馆开放接口
     （克利夫兰/大都会/芝加哥艺术博物馆的藏品索引是英文的）。用英文写具体的
     器物与画种，例如 "Ming dynasty painting city wall"、"Chinese hanging scroll
     scholar"、"Song dynasty landscape handscroll"、"Chinese bronze vessel"。
     宁可写「时代 + 画种/器物」这种能命中的组合，不要写事件名。
5. 全部输出为简体中文（英文检索词除外）。"""

ANGLE_SMALL = """

【本期的形式：小切口提问 → 用史实回答】
整期只回答一个问题（angle_question），所有章节都在回答它。要求：

1. **切口要小、要具体**：围绕吃穿住行、钱、税、病、死、罪、路、时间、身份。
   不要写成「某场战役的经过」，也不要写成「某制度的演变史」。
   · **章节标题不要带具体数字**（「一天五十里」这种）—— 标题是画面上的大字，
     数字一旦未经核查就上墙，会变成整个节目最显眼的错误。标题写提问或场景即可
     （「枷锁一出，谁管饭？」「病倒路上，谁给抓药？」）。
2. **问题必须能被史料回答 —— 这是选角度的第一原则**：
   · 先想「哪些东西是有记载的」，再据此定问题。优先挑这几类角度：
     - 有档案账目的经济生活：漕运、盐政、物价、俸禄、税收 —— 常有具体记载
     - 有考古实物直接证明的日常：吃穿住行用具、卫生、照明、取暖、埋葬
       （墓葬、器物、简牍、敦煌文书都能对上）
     - 有会典/则例/地方志可查的制度程序：科举流程、驿传、巡按、赈灾程序
     - 有著名文本记载的事件与人物：正史、实录、著名笔记（如《万历野获编》）
   · 反过来，**如果这个角度只有「律例里没写」可答，就不要选它** ——
     一个 8 分钟的节目撑不起十几处「史料没有记载」。
     例：「清代递解犯人一天走几里」律例并无规定，硬问只能反复说没记载；
     改问「押解是谁在管、谁出钱、什么情况下可以雇车」，就有制度可讲。
   · 大纲里每一章都要给 facts（本章必须依托的可考史实），至少 1 条；
     facts 要写「通行制度描述」或「有明确记载的个案」，不要写成出处不明的精确数字。
     写不出来就说明这一章的角度立不住，换角度。
3. **答案要靠史料**：制度怎么规定（律令、会典、则例）、实际怎么执行、
   不同身份的人待遇差在哪。这些是节目最有价值的部分。
4. **用小人物/具体场景承载**：尽量落到一个具体身份的人、一件具体的器物、
   一段具体的路程上，再由他的处境带出制度与大势。不要通篇讲制度。
5. **不要跑题**：每一章都要扣住本期那个问题，不相关内容一律不写。
6. 开场可以用「今天怎样 → 那时候怎样」作对照钩子，但正文必须以史实为主。
7. 结尾收束：这个答案照见了一个时代怎样的运行方式（不要喊口号）。
8. **诚实本身就是内容**：「史料没有规定」「记载互相矛盾」「只有某地某时的记载」
   这类结论比一个编出来的数字有价值得多，要把它们写进大纲。"""

ANGLE_EVENT = """

【本期的形式：讲清一件历史大事的来龙去脉】
· 有明确的起因、冲突、转折、结局
· 用具体的人物动作与场景推进，不要停留在事件概述
· 事件的关键数字（兵力、钱粮、时间）要真实"""

SCHEMA_SMALL = """{
  "title": "本期标题（14-22 字，冒号前是小切口，冒号后是具体疑问或悬念）",
  "angle_question": "本期要回答的那个具体问题（一句问句，20-40 字；必须是有史料能回答的）",
  "hook": "开篇钩子（70-100 字，从一个具体场景或一个反差切入，把人留住，可直接朗读）",
  "period": "年代范围的口播表述，如「清朝道光年间，公元 19 世纪前半叶」",
  "period_start": 年份整数（公元前为负，如 -221）,
  "period_end": 年份整数,
  "chapters": [
    {
      "index": 1,
      "heading": "章节标题（6-14 字，画面大字用）。不要写具体数字（里程/价格/重量/天数都不要上大字标题）——数字要等核查过才能用，标题写提问或场景即可",
      "summary": "本章回答问题的哪一层（2-3 个要点，写稿用，不进画面）",
      "facts": [
        "本章必须依托的可考史实（每条一句话，尽量带出处，如「《大清律例·名例》规定…」「乾隆年间奏折里提到…」）；拿不准就写「史料没有明确规定」——不要为了好看编数字"
      ],
      "seconds": 整数（本章目标口播秒数）,
      "image_queries": ["中文检索词1", "中文检索词2"],
      "image_queries_en": ["english query for museum APIs", "another english query"]
    }
  ]
}"""

SCHEMA_EVENT = """{
  "title": "本期标题（12-22 字，有钩子，不剧透结局）",
  "hook": "开篇钩子（60-90 字，用悬念把听众留住，可直接朗读）",
  "period": "年代范围的口播表述，如「东汉建安十三年，公元 208 年」",
  "period_start": 年份整数（公元前为负，如 -221）,
  "period_end": 年份整数,
  "chapters": [
    {
      "index": 1,
      "heading": "章节标题（6-14 字，画面大字用）",
      "summary": "本章要讲的 2-3 个要点（写稿用，不进画面）",
      "seconds": 整数（本章目标口播秒数）,
      "image_queries": ["中文检索词1", "中文检索词2"],
      "image_queries_en": ["english query for museum APIs", "another english query"]
    }
  ]
}"""


def mode_of(cfg: Config) -> str:
    m = str(cfg.story.get("angle_mode") or "small").strip().lower()
    return "event" if m == "event" else "small"


def system_for(cfg: Config) -> str:
    return SYSTEM_HEAD + (ANGLE_SMALL if mode_of(cfg) == "small" else ANGLE_EVENT)


def schema_for(cfg: Config) -> str:
    return SCHEMA_SMALL if mode_of(cfg) == "small" else SCHEMA_EVENT


SMALL_STRUCTURE = """【结构建议】
· 第 1 章：从一个具体场景或一个反常识的对比切入，让人立刻想知道答案
· 中间章节：一层层回答 —— 制度怎么规定 → 实际怎么执行（往往和规定不一样）
  → 不同身份/贫富的人差别在哪 → 具体花了多少钱、走了多少路、多少天
· 末章：收束到一个更大的认识：这件小事其实在说一个时代怎么运转"""

EVENT_STRUCTURE = """【结构建议】
· 结构建议：开篇（背景与处境）→ 冲突升级 → 转折 → 高潮 → 结果与余韵
· 章节标题要像纪录片分集标题，不要「第一章 背景介绍」这种机械写法"""


def build_outline(topic: str, cfg: Config, llm: LLM, material: str = "") -> Story:
    st = cfg.story
    small = mode_of(cfg) == "small"
    n = int(st.chapters)
    per = int(st.seconds_per_chapter)
    total = int(st.target_total_seconds)
    mat = (
        f"\n【参考素材（Bing 检索摘要，可能有噪声，只用来核对人名与年份）】\n{material[:6000]}\n"
        if material.strip() else "\n"
    )

    if small:
        ask = f"""请为下面这个主题写一份适合讲 5-10 分钟的分章大纲。

【主题】{topic}
{mat}
【本篇要求】
· 先把它凝练成**一个具体的日常问题**（填进 angle_question），整期都在回答它
· 共 {n} 章，每章口播约 {per} 秒（按中文播报 {st.chars_per_second} 字/秒算，约 {int(per * float(st.chars_per_second))} 字）
· 合计口播约 {total} 秒（约 {total / 60:.1f} 分钟），允许 ±15%

{SMALL_STRUCTURE}"""
    else:
        ask = f"""请为下面这个主题写一份适合讲 5-10 分钟的分章大纲。

【主题】{topic}
{mat}
【结构要求】
· 共 {n} 章，每章口播约 {per} 秒（按中文播报 {st.chars_per_second} 字/秒算，约 {int(per * float(st.chars_per_second))} 字）
· 合计口播约 {total} 秒（约 {total / 60:.1f} 分钟），允许 ±15%
{EVENT_STRUCTURE}"""

    user = ask + f"""

【输出】只输出如下 JSON，不要任何解释文字：
{schema_for(cfg)}"""

    data = llm.chat_json(
        system_for(cfg),
        user,
        max_tokens=4000,
        temperature=float(cfg.llm.get("outline_temperature", 0.6)),
    )
    if not isinstance(data, dict):
        raise ValueError(f"大纲不是 JSON 对象：{type(data)}")

    chapters: list[Chapter] = []
    for i, c in enumerate(data.get("chapters") or [], start=1):
        if not isinstance(c, dict):
            continue
        heading = str(c.get("heading") or "").strip() or f"第{i}章"
        if not bool(st.get("chapter_headings", True)):
            heading = f"第{i}章"
        chapters.append(
            Chapter(
                index=i,
                heading=heading,
                summary=str(c.get("summary") or "").strip(),
                seconds=int(c.get("seconds") or per),
                facts=[str(f).strip() for f in (c.get("facts") or []) if str(f).strip()],
                image_queries=[str(q).strip() for q in (c.get("image_queries") or []) if str(q).strip()],
                image_queries_en=[str(q).strip() for q in (c.get("image_queries_en") or []) if str(q).strip()],
            )
        )
    if not chapters:
        raise ValueError("大纲里没有章节，模型输出异常")

    story = Story(
        topic=topic,
        title=str(data.get("title") or topic).strip(),
        angle_question=str(data.get("angle_question") or "").strip(),
        hook=str(data.get("hook") or "").strip(),
        period=str(data.get("period") or "").strip(),
        period_start=int(data.get("period_start") or 0),
        period_end=int(data.get("period_end") or 0),
        chapters=chapters,
        material=material,
    )
    log.info("大纲完成：《%s》%d 章，预计 %.1f 分钟",
             story.title, len(chapters), sum(c.seconds for c in chapters) / 60)
    if story.angle_question:
        log.info("本期要回答：%s", story.angle_question)
    for c in chapters:
        log.info("  %d. %s（%ds, 配图词 %d 个）", c.index, c.heading, c.seconds, len(c.image_queries))
    return story
