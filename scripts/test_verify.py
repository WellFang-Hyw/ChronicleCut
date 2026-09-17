"""零成本回归测试：不调任何 API，只验证纯函数与过滤逻辑。

    python scripts/test_verify.py

覆盖的都是实际踩过的坑，改代码后先跑这个再跑正式流程。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from hsg import images, textfmt, verify, video     # noqa: E402
from hsg.config import load_config               # noqa: E402
from hsg.models import Chapter, Scene, Story     # noqa: E402
from hsg.subtitles import _wrap, split_cues      # noqa: E402

PASS, FAIL = [], []


def check(name: str, got, want) -> None:
    if got == want:
        PASS.append(name)
        print(f"  ✓ {name}")
    else:
        FAIL.append(name)
        print(f"  ✗ {name}\n      得到：{got!r}\n      期望：{want!r}")


def check_true(name: str, cond: bool, detail: str = "") -> None:
    check(name, bool(cond), True) if cond else check(name, f"False {detail}", True)


# ---------------------------------------------------------------- 口播清洗
def test_sanitize() -> None:
    print("\n[口播清洗 textfmt.sanitize_narration]")
    check("日期不补零（09月10日→9月10日）",
          textfmt.sanitize_narration("他于09月10日抵达"), "他于9月10日抵达")
    check("个位日不补零（9月09日→9月9日）",
          textfmt.sanitize_narration("9月09日"), "9月9日")
    check("markdown 星号去掉",
          textfmt.sanitize_narration("**曹操**南下"), "曹操南下")
    check("markdown 井号标题去掉",
          textfmt.sanitize_narration("## 第一章\n曹操南下"), "曹操南下")
    check("章节标记不进播报",
          textfmt.sanitize_narration("第一章 曹操南下"), "曹操南下")
    check("来源词换成史料表述",
          textfmt.sanitize_narration("据维基百科记载，他生于沛国"),
          "据史料记载，他生于沛国")
    check("百科残留被抹掉",
          textfmt.sanitize_narration("百度百科说他"), "说他")
    check("年份区间说成「到」",
          textfmt.sanitize_narration("208-209年间"), "208年到209年间")
    check("公元前表述",
          textfmt.sanitize_narration("前221年统一"), "公元前221年统一")
    check("千分位去掉",
          textfmt.sanitize_narration("1,000人"), "1000人")
    check("【】标记去掉",
          textfmt.sanitize_narration("【停顿】他走了"), "他走了")
    check("全英文括注去掉",
          textfmt.sanitize_narration("鸿门宴（Hongmen Banquet）上"), "鸿门宴上")


def test_fix_line_punct() -> None:
    print("\n[折行标点 textfmt.fix_line_punct]")
    check("行首逗号挪回上一行",
          textfmt.fix_line_punct(["江面上起了东南风，黄盖的十艘蒙冲斗舰", "，满载薪草膏油"]),
          ["江面上起了东南风，黄盖的十艘蒙冲斗舰，", "满载薪草膏油"])
    check("行首句号同样处理",
          textfmt.fix_line_punct(["他停住了", "。然后转身"]), ["他停住了。", "然后转身"])
    check("正常两行不受影响",
          textfmt.fix_line_punct(["第一行", "第二行"]), ["第一行", "第二行"])

    wrapped = _wrap("江面上起了东南风，黄盖的十艘蒙冲斗舰，满载薪草膏油，直冲曹营。", 18, 2)
    check_true("字幕折行后不以标点开头",
               not wrapped.split(r"\N")[1].startswith("，"), wrapped)


def test_extract_years() -> None:
    print("\n[年份抽取 textfmt.extract_years]")
    check("公元前为负数", textfmt.extract_years("公元前221年"), [-221])
    check("公元后", textfmt.extract_years("208年"), [208])
    check("不把兵力当成年份", textfmt.extract_years("八十万人"), [])
    check("年份区间抽两个", textfmt.extract_years("208年到209年"), [208, 209])


# ---------------------------------------------------------------- 审校过滤
class _FakeLLM:
    """假装是 DeepSeek，返回构造好的审校结果。"""

    def __init__(self, payload):
        self.payload = payload

    def chat_json(self, *_a, **_kw):
        return self.payload


def _story_with(text: str) -> Story:
    s = Scene(index=1, text=text, chapter_index=1)
    return Story(topic="测试", title="测试", chapters=[Chapter(index=1, heading="一", scenes=[s])])


def test_audit_filter() -> None:
    print("\n[审校可执行性过滤 verify.llm_audit]")
    cfg = load_config()
    scene_text = "曹无伤是刘邦的左司马，他派人向项羽告密。"
    story = _story_with(scene_text)

    payload = {"issues": [
        # ① 实跑遇到的自相矛盾条目：改法与原句一样、说不出错在哪
        {"scene_index": 1, "severity": "high", "wrong_text": "左司马",
         "problem": "曹无伤的官职写错，应为「左司马」而非「左司马」", "fix": "左司马"},
        # ② 没给 wrong_text，不可执行
        {"scene_index": 1, "severity": "high", "wrong_text": "",
         "problem": "此处表述不够准确", "fix": "改得更准确"},
        # ③ 指出的原文在稿子里根本不存在（审校员幻觉）
        {"scene_index": 1, "severity": "high", "wrong_text": "右司马",
         "problem": "应为左司马", "fix": "左司马"},
        # ④ 真问题：应该在稿子里、且改法不同
        {"scene_index": 1, "severity": "high", "wrong_text": "告密",
         "problem": "用词不合史实，应为「使人告项羽」", "fix": "派人向项羽通风报信"},
    ]}
    out = verify.llm_audit(story, cfg, _FakeLLM(payload))
    check("4 条里只留下 1 条真问题", len(out), 1)
    check("留下的是可执行的那条", out[0]["detail"].startswith("「告密」"), True)
    check("级别保留为 fail", out[0]["kind"], "fail")


def test_rule_check_catches() -> None:
    print("\n[规则层校验 verify.rule_check]")
    cfg = load_config()
    story = _story_with("这条分镜里含 百度百科 这个词。" * 3)
    kinds = {i["kind"] for i in verify.rule_check(story, cfg)}
    check_true("抓到来源词残留", "fail" in kinds)

    short = _story_with("太短了")
    check_true("抓到过短分镜",
               any("过短" in i["detail"] for i in verify.rule_check(short, cfg)))

    dup_text = "黄盖放火烧了曹军的战船。" * 2
    st = Story(topic="t", chapters=[
        Chapter(index=1, heading="一", scenes=[
            Scene(index=1, text="黄盖放火烧了曹军的战船。", chapter_index=1),
            Scene(index=2, text=dup_text, chapter_index=1)])])
    check_true("抓到跨分镜重复句子",
               any("重复" in i["detail"] for i in verify.rule_check(st, cfg)))

    year_story = Story(topic="t", period_start=200, period_end=210, chapters=[
        Chapter(index=1, heading="一", scenes=[
            Scene(index=1, text="公元一千二百年，也就是1200年，他做了这件事。", chapter_index=1)])])
    check_true("抓到超出年代区间的年份",
               any("超出所述年代" in i["detail"] for i in verify.rule_check(year_story, cfg)))


# ---------------------------------------------------------------- 画面运动
def test_motion() -> None:
    print("\n[画面运动表达式 video.motion_expr]")
    cfg = load_config()
    exprs = [video.motion_expr(m, cfg, 20.0) for m in range(4)]
    check("四种方向互不相同", len(set(exprs)), 4)
    check_true("表达式里带时长分母", all("20.000" in x or "20.000" in y for x, y in exprs))
    check_true("x/y 表达式是可用的（含 iw 或 ih 或常数）",
               all("iw" in x or "ih" in x or x.isdigit() for x, _ in exprs))


# ---------------------------------------------------------------- 字幕切分
def test_cues() -> None:
    print("\n[字幕切分 subtitles.split_cues]")
    cues = split_cues("曹操南下。孙权犹豫了很久，最后还是决定抵抗。", 18)
    check_true("按句号/逗号切开", len(cues) >= 2)
    check_true("每条不超字数", all(len(c) <= 18 for c in cues))


def test_dedupe() -> None:
    print("\n[选题查重 history]")
    from hsg import history as H

    cases = [
        ("鸿门宴", "鸿门宴上，项羽为什么没杀刘邦", True),          # 短名 ↔ 池子原文
        ("鸿门一宴，项羽为何放走刘邦？", "鸿门一宴，项羽为何放走刘邦？", True),
        ("虎门销烟", "虎门销烟：禁烟背后的账本", True),
        ("虎门销烟：禁烟背后的账本", "虎门销烟：禁烟背后的账本", True),
        ("赤壁之战：一场大火改写了三国", "鸿门宴上，项羽为什么没杀刘邦", False),
        ("玄武门之变", "虎门销烟：禁烟背后的账本", False),
        ("楚汉之争：项羽为什么会输", "鸿门一宴，项羽为何放走刘邦？", False),
        ("清代", "清代白银外流与禁烟", False),                     # 2 字太短，不判重
    ]
    for a, b, want in cases:
        check(f"similar({a!r}, {b[:12]!r}…) = {want}", H.similar(a, b), want)

    check("规范化抹平标点空格", H.norm("虎门销烟：禁烟 背后的账本"), "虎门销烟禁烟背后的账本")

    # 换了个说法：规则判不出来（Jaccard 只有 0.19），但应该被预筛捞出来
    a = "鸿门一宴，项羽为何放走刘邦？"
    b = "鸿门宴上，项羽为什么没杀刘邦"
    f1 = "楚汉之争：项羽为什么会输"
    check("同类换说法：规则不判重", H.similar(a, b), False)
    check("同类换说法：预筛能捞出来（该交给模型判）", H.related(a, b), True)
    check("不同事件：预筛不放行", H.related(f1, b), False)


def test_dedupe_llm_verdict() -> None:
    """模型裁定层：规则判不出来的换说法，由 LLM 兜住。"""
    print("\n[查重·模型裁定层]")
    import tempfile
    from pathlib import Path as _P

    from hsg import history as H
    from hsg.config import load_config

    cfg = load_config()
    with tempfile.TemporaryDirectory() as td:
        cfg["paths"]["data_dir"] = td          # 记录写到临时目录，别碰真实记录
        H.save(cfg, [{
            "run_id": "t1", "generated_at": "2026-09-11T11:23:00",
            "topic": "鸿门宴上，项羽为什么没杀刘邦",
            "title": "鸿门一宴，项羽为何放走刘邦？",
            "speech_minutes": 6.78,
        }])

        class _Yes:
            def chat_json(self, *_a, **_k):
                return {"same": True, "reason": "都是鸿门宴"}

        class _No:
            def chat_json(self, *_a, **_k):
                return {"same": False, "reason": "不同事件"}

        # ① 同一个故事换个说法：模型说 same → 判重复
        dup = H.find_duplicate(cfg, "鸿门宴：项羽为什么放走了刘邦", llm=_Yes())
        check("换说法 + 模型判 same → 命中记录", bool(dup), True)
        # ② 模型说不是 → 放行
        check("模型判 not same → 不算重复",
              H.find_duplicate(cfg, "鸿门宴：项羽为什么放走了刘邦", llm=_No()), None)
        # ③ 不沾边的题材根本不该调模型（_No 会返回 False，但仍然应为 None）
        check("不沾边的题材直接放行",
              H.find_duplicate(cfg, "玄奘西行：偷渡出关的取经人", llm=_Yes()), None)
        _P(td).exists()


def test_history_roundtrip() -> None:
    """生成记录：写入 → 读回 → 立刻参与查重 → markdown 版生成。"""
    print("\n[生成记录 · 写入/读取/查重闭环]")
    import tempfile
    from pathlib import Path as _P

    from hsg import history as H
    from hsg.config import load_config
    from hsg.models import Chapter, Scene, Story

    cfg = load_config()
    with tempfile.TemporaryDirectory() as td:
        cfg["paths"]["data_dir"] = td          # 别碰真实记录
        s = Scene(index=1, text="测试口播稿一段", image_query="测试检索词", chapter_index=1)
        s.duration = 20.0
        s.image_path = _P(td) / "scene_001.jpg"
        story = Story(topic="测试题材：某历史事件", title="测试标题",
                      period="公元 200 年",
                      chapters=[Chapter(index=1, heading="第一章", scenes=[s])])
        rec = H.build_record(
            story, cfg, total_seconds=20.0,
            video_seconds={"portrait": 24.0, "landscape": 24.5},
            outputs=["a.mp4"], script="s.md", metadata="m.json",
            material_meta={"items": 8},
            verify_stats={"rule_fail": 0, "rule_warn": 0, "audit_issues": 1},
        )
        H.append_record(cfg, rec)
        loaded = H.load(cfg)
        check("记录写入并能读回 1 条", len(loaded), 1)
        check("题材保存正确", loaded[0]["topic"], "测试题材：某历史事件")
        check("配图统计正确", loaded[0]["images"], {"total": 1, "found": 1, "fallback": 0})
        check("成片时长被记录", loaded[0]["video_seconds"]["portrait"], 24.0)
        check("配音分钟数被记录", loaded[0]["speech_minutes"], 0.33)
        # 写稿模型可能是配置里的任意一个（2026-09-17 起 user 可切 MiniMax），
        # 所以跟配置比，别写死 —— 写死的用例一改配置就挂，且看不出原因。
        check_true("模型分工被记录", f"{cfg.llm.provider}/" in loaded[0]["text_model"],
                   str(loaded[0]["text_model"]))
        md = H.md_path(cfg)
        check_true("人看的 markdown 版也生成了",
                   md.exists() and "测试标题" in md.read_text(encoding="utf-8"))
        check_true("新记录立刻参与查重（短名命中）",
                   H.find_duplicate(cfg, "某历史事件") is not None)
        # 追加第二条不应覆盖第一条
        H.append_record(cfg, dict(loaded[0], topic="另一个题材：别的事", title="另一期"))
        check("追加不覆盖已有记录", len(H.load(cfg)), 2)


def test_angle_mode() -> None:
    """切入方式（小切口 / 事件式）在选题池与各处提示词里都要生效。"""
    print("\n[切入方式 angle_mode]")
    from hsg import outline, topics, writer
    from hsg.config import load_config

    cfg = load_config()
    cfg.story["angle_mode"] = "small"
    check("small 模式：大纲提示词含小切口要求", "小切口提问" in outline.system_for(cfg), True)
    check("small 模式：写稿提示词要求每段带具体信息",
          "每段都要有具体信息" in writer.system_for(cfg), True)
    check("small 模式：大纲 schema 有 angle_question", "angle_question" in outline.schema_for(cfg), True)
    check("small 模式：结构建议含「一层层回答」", "一层层回答" in outline.SMALL_STRUCTURE, True)
    nxt = topics.pick(seed=5, mode="small")
    check_true("small 模式选题来自小切口池",
               nxt is not None and nxt.title in topics.TOPICS_ANGLE, f"→ {nxt}")
    check_true("小切口池与事件池不重叠",
               not (set(topics.TOPICS_ANGLE) & set(topics.TOPICS_EVENT)))

    cfg.story["angle_mode"] = "event"
    check("event 模式：切回事件式提示词", "来龙去脉" in outline.system_for(cfg), True)
    check("event 模式：schema 里没有 angle_question", "angle_question" in outline.schema_for(cfg), False)
    nxt = topics.pick(seed=5, mode="event")
    check_true("event 模式选题来自事件池",
               nxt is not None and nxt.title in topics.TOPICS_EVENT, f"→ {nxt}")
    check("缺省（未配置）按 small 处理", outline.mode_of(load_config()), "small")

    # 小切口池的主题都应该是「切口：问题」的问句形式
    bad = [t for t in topics.TOPICS_ANGLE if "：" not in t and "?" not in t and "？" not in t]
    check_true("小切口题材都带冒号或问号（提问式）", not bad)


def test_topic_levels() -> None:
    """两级选题：类型（L1 高层描述）+ 标题与描述（L2），以及按类型挑题。"""
    print("\n[两级选题 topics.STORY_TYPES / Topic / pick]")
    from hsg import topics

    # ---- L1：类型表本身
    check_true("类型表有内容", len(topics.STORY_TYPES) >= 8,
               f"{len(topics.STORY_TYPES)} 类")
    check_true("每类都有一句高层描述（不能只有名字）",
               all(len(v) >= 20 for v in topics.STORY_TYPES.values()),
               f"最短 {min(len(v) for v in topics.STORY_TYPES.values())} 字")
    check("类型描述能查出来", bool(topics.type_desc("行旅与驿传")), True)
    check("不认识的类型 → 空描述", topics.type_desc("不存在"), "")

    # ---- L2：池子条目必须两级齐全（位置参数顺序是 类型/标题/描述）
    bad_title = [t for t in topics.ITEMS_SMALL if not t.title]
    bad_type = [t for t in topics.ITEMS_SMALL
                if t.type not in topics.STORY_TYPES]
    bad_desc = [t for t in topics.ITEMS_SMALL if len(t.desc) < 15]
    check_true("池子条目的类型都在类型表里（不是自由字符串）", not bad_type,
               f"→ {[t.type for t in bad_type][:3]}")
    check_true("池子条目都写了 L2 描述（不是标题的复述）", not bad_desc,
               f"→ {[t.title for t in bad_desc][:3]}")
    check_true("池子条目的标题不为空（字段顺序没写反）", not bad_title,
               f"→ {[str(t)[:12] for t in bad_title][:3]}")
    check_true("描述不是标题的复制",
               all(t.desc.strip() != t.title.strip() for t in topics.ITEMS_SMALL))
    check_true("每个类型下至少有一条选题（类型表不留空壳）",
               not (set(topics.STORY_TYPES) & {t.type for t in topics.ITEMS_SMALL}
                    ^ {t.type for t in topics.ITEMS_SMALL}),
               f"小切口池用到的类型 {sorted({t.type for t in topics.ITEMS_SMALL})}")
    check_true("池子里没有重复标题",
               len({t.title for t in topics.ITEMS_SMALL + topics.ITEMS_EVENT})
               == len(topics.ITEMS_SMALL) + len(topics.ITEMS_EVENT))

    # ---- 字段顺序（踩过的坑：类型被塞进标题，池子打出来是空的）
    one = topics.ITEMS_SMALL[0]
    check_true("位置参数 = (类型, 标题, 描述)",
               one.type in topics.STORY_TYPES and "：" in one.title, f"→ {one.as_dict()}")

    # ---- 按类型挑题
    got = topics.pick(seed=1, type_filter="行旅与驿传")
    check("按类型挑题", got.type, "行旅与驿传")
    check_true("指定的类型不存在时返回 None（不静默给别的类型）",
               topics.pick(seed=1, type_filter="不存在的类型") is None)
    allt = {topics.pick(seed=i).type for i in range(30)}
    check_true("随机挑题会覆盖到多个类型", len(allt) >= 4, f"→ {sorted(allt)}")

    # ---- 避开最近做过的类型（这是类型成为一等概念之后才有的能力）
    recent = ["行旅与驿传", "生计与物价"]
    fresh = topics.pick(seed=3, recent_types=recent)
    check_true("优先挑近期没做过的类型", fresh.type not in recent, f"→ {fresh.type}")
    covered = {topics.pick(seed=i, recent_types=recent).type for i in range(12)}
    check_true("近期类型被避开（不是碰巧）", not (covered & set(recent)),
               f"→ {sorted(covered)}")

    # ---- used_types：从生成记录里取最近类型
    recs = [{"topic_type": "行旅与驿传"}, {"topic_type": ""}, {"topic_type": "刑狱与流放"}]
    check("used_types 取最近几期的类型（按顺序、跳过空值）",
          topics.used_types(recs), ["行旅与驿传", "刑狱与流放"])
    check("used_types 只看最近 N 期",
          topics.used_types(recs, last=1), ["刑狱与流放"])
    check("used_types：记录里没有类型字段时返回空", topics.used_types([{}]), [])
    # 老记录（这个字段是后加的）要能按标题回查池子补出类型，
    # 否则「避开最近类型」在重跑所有期之前一直空转
    old_rec = [{"topic": "驿站那匹马：一封加急军报从边关到京城要跑几天？"}]
    check("used_types：老记录按标题回查池子补类型",
          topics.used_types(old_rec), ["行旅与驿传"])
    guessed = topics.used_types([{"topic": "县衙大牢里到底关着谁"}])
    check_true("used_types：池子里没有的标题用关键词规则兜底",
               guessed == ["刑狱与流放"], f"→ {guessed}")

    # ---- 出题/判类型/补描述（打桩，不花钱）
    class _FakeLLM:
        def __init__(self, payload):
            self.payload = payload
            self.calls = 0

        def chat_json(self, system, user, **kw):
            self.calls += 1
            if isinstance(self.payload, Exception):
                raise self.payload
            return self.payload

    cfg = load_config()
    t = topics.propose(cfg, _FakeLLM({"type": "钱粮与税役", "title": "盐引那本账：商人怎么和官府结算？",
                                      "desc": "从一张盐引的流转看专卖制度怎么运行。"}),
                       avoid=[], mode="small")
    check("出题：两级都拿到了", (t.type, bool(t.desc)), ("钱粮与税役", True))
    t2 = topics.propose(cfg, _FakeLLM({"type": "瞎编的类型", "title": "某个新题材的两个例子细节"}),
                        avoid=[], mode="small")
    check_true("出题：类型不在表里时不接受（回退到关键词规则或未分类）",
               t2.type in topics.STORY_TYPES or t2.type == topics.UNKNOWN_TYPE,
               f"→ {t2.type}")
    check("判类型：模型给的表内类型就被采纳",
          topics.classify("驿站那匹马：一封加急军报要跑几天？", cfg,
                          _FakeLLM({"type": "行旅与驿传"})), "行旅与驿传")
    check("判类型：模型挂了退回关键词规则",
          topics.classify("县衙大牢里吃什么", cfg, _FakeLLM(RuntimeError("boom"))), "刑狱与流放")
    check("判类型：认不出来的归到未分类",
          topics.classify("完全无关的一句话", cfg, _FakeLLM({"type": "没这个"})),
          topics.UNKNOWN_TYPE)
    filled = topics.fill_levels(topics.Topic(title="驿站那匹马：一封加急军报要跑几天？"),
                                cfg, _FakeLLM({"desc": "一封加急公文在路上要经过什么。"}))
    check("补两级：类型与描述都补上了",
          (filled.type, bool(filled.desc)), ("行旅与驿传", True))
    keep = topics.fill_levels(topics.ITEMS_SMALL[0], cfg, _FakeLLM(RuntimeError("boom")))
    check("补两级：已有描述的原样保留（不重复花钱）",
          keep.desc, topics.ITEMS_SMALL[0].desc)
    no_llm = topics.fill_levels(topics.Topic(title="随便一个标题够长"), cfg, None)
    check_true("补两级：没有 LLM 时不报错，类型落到兜底名",
               no_llm.type in (topics.UNKNOWN_TYPE, "") and no_llm.desc == "",
               f"→ type={no_llm.type!r}")

    # ---- 手填两级（-t + --topic-type + --topic-desc）
    from hsg import cli

    a = cli.build_parser().parse_args(["-t", "驿站那匹马：一封加急军报要跑几天？",
                                       "--topic-type", "行旅与驿传",
                                       "--topic-desc", "一封加急公文在路上要经过什么。"])
    check("三个参数都能解析出来",
          (a.topic_type, bool(a.topic_desc)), ("行旅与驿传", True))
    built = cli._build_topic(a.topic, a.topic_type, a.topic_desc)
    check("手填的两级进 Topic（并带上类型的高层描述）",
          (built.type, built.type_desc != "", built.desc != ""), ("行旅与驿传", True, True))
    only_title = cli._build_topic("某个手填的标题")
    check("只给标题时类型/描述留空（后面由模型补）",
          (only_title.type, only_title.desc), ("", ""))
    try:
        cli._build_topic("标题", "不存在的类型")
        check("类型写错要被拦下（不能静默归到未分类）", False, "居然没报错")
    except SystemExit as exc:
        check("类型写错要被拦下（退出码 2）", exc.code, 2)
    # plan 产物文件名必须带标题，否则同一天跑第二次会覆盖第一次
    st = cli._plan_stem("曹操杀吕伯奢：那句「宁我负人」到底谁写的？")
    check_true("plan 文件名带日期与标题", st.startswith("20") and "_plan_" in st
               and "曹操杀吕伯奢" in st, f"→ {st}")
    check_true("同一天两个不同选题的文件名不同（不会互相覆盖）",
               cli._plan_stem("甲题：一个例子") != cli._plan_stem("乙题：另一个例子"))
    check_true("文件名里的非法字符被清掉",
               not any(c in cli._plan_stem('a/b:c*d?e"f') for c in '/\\:*?"<>|'))

    no_type_desc = cli._build_topic("标题", type_name="", desc="只有描述")
    check("只给描述也可以（类型交给模型判）", (no_type_desc.type, no_type_desc.desc),
          ("", "只有描述"))
    # 手填的两级不能被模型覆盖（也不该白花一次调用）
    fake2 = _FakeLLM({"type": "疾病与丧葬", "desc": "模型瞎写的描述"})
    kept = topics.fill_levels(built, cfg, fake2)
    check("手填的两级原样保留，不调模型", (kept.type, kept.desc, fake2.calls),
          ("行旅与驿传", "一封加急公文在路上要经过什么。", 0))

    # ---- 用户自己制定的选题池（scripts/add_topic.py 写的就是它）
    import tempfile as _tf
    with _tf.TemporaryDirectory() as td:
        up = Path(td) / "user.json"
        check("文件不存在 → 空池（不报错）", topics.load_user_pool(up).is_empty, True)
        (Path(td) / "bad.json").write_text("{ 这不是 json", encoding="utf-8")
        check("坏文件 → 空池（不能让手写的文件搞崩出片）",
              topics.load_user_pool(Path(td) / "bad.json").is_empty, True)
        pool = topics.UserPool(types={"近代交通": "铁路轮船电报如何改变距离感。"},
                               topics=[topics.Topic(type="近代交通", title="京张铁路：一条路修了几年？",
                                                    desc="从勘测到通车的账。", mode="small")],
                               path=str(up))
        topics.save_user_pool(pool)
        back = topics.load_user_pool(up)
        check("存盘再读回（自定义类型 + 条目）",
              (len(back.topics), back.types), (1, {"近代交通": "铁路轮船电报如何改变距离感。"}))
        check("自定义类型能查到高层描述",
              topics.type_desc("近代交通", back), "铁路轮船电报如何改变距离感。")
        check_true("用户类型并进 all_types", "近代交通" in topics.all_types(back))
        check("自定义类型的条目进池子（自己写的排前面）",
              topics.pool_for("small", back)[0].title, "京张铁路：一条路修了几年？")
        check("按自定义类型过滤能挑到",
              topics.pick(seed=1, type_filter="近代交通", user=back).title,
              "京张铁路：一条路修了几年？")
        check_true("池子总数 = 内置 + 自己写的",
                   len(topics.pool_for("small", back)) == len(topics.TOPICS_ANGLE) + 1,
                   f"→ {len(topics.pool_for('small', back))}")
        check("按标题回查能带上类型（查重要用）",
              topics.topic_of("京张铁路：一条路修了几年？", back).type, "近代交通")
        check_true("自己写的条目也进「最近做过的类型」回查",
                   "近代交通" in topics.used_types([{"topic": "京张铁路：一条路修了几年？"}], user=back))
        check_true("池子输出里标出自己写了几条",
                   "自己写的 1 条" in topics.render_pool("small", back))
        # 手填类型时，用户自定义类型也是合法的
        from hsg import cli as _cli
        check("--topic-type 接受自定义类型",
              _cli._build_topic("标题", "近代交通", "", back).type, "近代交通")
        try:
            _cli._build_topic("标题", "编的类型", "", back)
            check("自定义类型表里没有的仍然拦下", False, "居然没报错")
        except SystemExit as exc:
            check("自定义类型表里没有的仍然拦下", exc.code, 2)

    # ---- 选题池的可读输出（run.bat topics）
    txt = topics.render_pool("small")
    check_true("选题池输出含类型与高层描述", "■ 行旅与驿传" in txt and "人与物的长途移动" in txt)
    check_true("选题池输出含 L2 标题与描述", "驿站那匹马" in txt and "换马接力" in txt)


def test_fact_audit() -> None:
    """写稿前的锚点核查：ok 保留 / unsure 降级标注 / wrong 删除。"""
    print("\n[写稿前锚点核查 verify.audit_facts]")
    from hsg import verify
    from hsg.config import load_config
    from hsg.models import Chapter, Story

    cfg = load_config()
    ch = Chapter(index=1, heading="一",
                 facts=["清代流犯由沿途州县按站递解", "递解人犯日行五十里",
                        "乾隆年间《刑案汇览》记载…"])
    story = Story(topic="测试", chapters=[ch])

    class _LLM:
        def chat_json(self, *_a, **_k):
            return {"facts": [
                {"index": 1, "verdict": "ok", "note": "制度通行描述"},
                {"index": 2, "verdict": "unsure", "note": "具体数字无据"},
                {"index": 3, "verdict": "wrong", "note": "《刑案汇览》成书于道光年间"},
            ]}

    stat = verify.audit_facts(story, cfg, _LLM())
    check("统计正确（1 确认 / 1 存疑 / 1 删除）",
          (stat["ok"], stat["unsure"], stat["wrong"]), (1, 1, 1))
    check("wrong 的锚点被删除", len(ch.facts), 2)
    check_true("ok 的锚点原样保留", "清代流犯由沿途州县按站递解" in ch.facts)
    check_true("存疑的锚点被降级标注，仍保留内容",
               any("存疑" in f and "日行五十里" in f for f in ch.facts))
    check_true("被删的锚点确实不在了", all("刑案汇览" not in f for f in ch.facts))

    # 开关关掉时不应该调模型
    class _Boom:
        def chat_json(self, *_a, **_k):
            raise AssertionError("关掉 fact_audit 后不该调用模型")

    cfg.verify["fact_audit"] = False
    stat2 = verify.audit_facts(story, cfg, _Boom())
    check("关闭 fact_audit 后跳过（total=0）", stat2["total"], 0)

    # 模型调用失败时不能丢锚点（宁可保留，也不能让大纲变空）
    cfg.verify["fact_audit"] = True

    class _Fail:
        def chat_json(self, *_a, **_k):
            raise RuntimeError("网络挂了")

    ch2 = Chapter(index=1, heading="一", facts=["锚点A", "锚点B"])
    st2 = Story(topic="t", chapters=[ch2])
    stat3 = verify.audit_facts(st2, cfg, _Fail())
    check("核查失败时锚点原样保留", len(ch2.facts), 2)
    check_true("核查失败被记录", "error" in stat3)


# ---------------------------------------------------------------- 版权安全配图
def test_license_policy() -> None:
    print("\n[配图版权策略 images.active_providers]")
    cfg = load_config()
    cfg.images["license_policy"] = "clean"
    clean = images.active_providers(cfg)
    check_true("clean 策略下排除版权未明的 bing", "bing" not in clean)
    check_true("clean 策略下保留无需 key 的博物馆图源（cleveland）", "cleveland" in clean)
    check_true("没配 key 的图源自动跳过（pexels / unsplash）",
               "pexels" not in clean and "unsplash" not in clean)
    check_true("缺 key 也不至于没图源", len(clean) >= 1, str(clean))
    cfg.images["license_policy"] = "mixed"
    mixed = images.active_providers(cfg)
    check_true("mixed 策略下 bing 自动参与兜底（不必写进 providers）", "bing" in mixed)
    check_true("mixed 时 bing 排最后（干净图源优先用）", mixed[-1] == "bing")


def test_culture_filter() -> None:
    print("\n[文化圈过滤 images._culture_ok]")
    cfg = load_config()
    check_true("中国画家作品通过（克利夫兰字段）",
               images._culture_ok("Mi Youren (Chinese, 1072–1151) 山水", cfg))
    check_true("大都会 culture=China 通过",
               images._culture_ok("China Asian Art Ink and color on paper", cfg))
    check_true("西方作品被挡掉（芝加哥实测返回过《大碗岛的星期天》）",
               not images._culture_ok(
                   "Georges Seurat (French, 1859-1891) A Sunday on La Grande Jatte", cfg))
    check_true("波斯手抄本被挡掉（大都会实测返回过）",
               not images._culture_ok("Iran (probably Tabriz) Islamic Art manuscript", cfg))
    off = load_config()
    off.images["culture_filter"] = False
    check_true("关掉开关后不过滤", images._culture_ok("Georges Seurat (French)", off))


def test_era_match() -> None:
    print("\n[年代匹配 images.parse_years / era_tier]")
    check("解析单一年份", images.parse_years("1130"), (1130, 1130))
    check("解析年份区间", images.parse_years("1598–1652"), (1598, 1652))
    check("解析朝代区间文本", images.parse_years("Ming dynasty, 1368-1644"), (1368, 1644))
    check("解析世纪", images.parse_years("2nd century"), (101, 200))
    check("解析不出来给 None", images.parse_years("晚明"), None)
    period = (1600, 1644)                      # 明末
    check("同时代作品最贴合（陈洪绶 1598–1652）",
          images.era_tier((1598, 1652), period), 0)
    check("近世作品算尚可（元代 1300–1400）", images.era_tier((1300, 1400), period), 1)
    check("宋代山水算不贴（1130，差 470 年）", images.era_tier((1130, 1130), period), 2)
    check("没有年代信息算未知", images.era_tier(None, period), 2)


# ---------------------------------------------------------------- 复检闸门
def test_final_check_gate() -> None:
    print("\n[复检闸门 verify.final_check / VerifyFailed]")
    cfg = load_config()
    bad = _story_with("这条分镜里含 百度百科 这个词。" * 3)
    res = verify.final_check(bad, cfg, None)   # 不传 llm → 只跑规则层，零成本
    check_true("复检抓到未解决的问题", len(res["fails"]) > 0)
    check_true("复检同时返回 fails / warns", "fails" in res and "warns" in res)
    check_true("VerifyFailed 是可捕获的运行时错误",
               issubclass(verify.VerifyFailed, RuntimeError))
    good = _story_with("曹操率军南下，沿途州县望风而降，荆州的官员们纷纷开城投降，"
                       "这件事在史料里有明确记载，也是整场战争胜负的转折点。")
    check_true("干净稿子复检无必修问题", len(verify.final_check(good, cfg, None)["fails"]) == 0)
    short = _story_with("太短了")
    check_true("过短分镜在复检里会算必修问题",
               len(verify.final_check(short, cfg, None)["fails"]) > 0)


def test_license_plumbing() -> None:
    print("\n[版权与待核清单落盘 pipeline.write_script / write_metadata]")
    import json

    from hsg import pipeline

    cfg = load_config()
    text = "曹操率军南下，沿途州县望风而降，这件事在史料里有明确记载。"
    story = _story_with(text)
    st = story.all_scenes[0]
    st.duration = 20.0
    st.image_path = Path("scene_001.jpg")
    st.image_license = "CC0 公共领域"
    st.image_credit = "CC0 公共领域｜克利夫兰艺术博物馆 · 佚名（1130）"
    story.notes = ["分镜 1：把后来的制度说成城破当天发生"]

    out = ROOT / "data/tmp/test_script.md"
    pipeline.write_script(story, cfg, out, 20.0)
    txt = out.read_text(encoding="utf-8")
    check_true("稿件里有「待人工核对」清单", "待人工核对" in txt)
    check_true("未解决项内容写进清单", "把后来的制度说成城破当天发生" in txt)
    check_true("配图小节标注了版权", "CC0 公共领域" in txt)
    check_true("配图小节标题已含版权字样", "配图出处与版权" in txt)

    story2 = _story_with(text)
    story2.all_scenes[0].duration = 20.0
    story2.all_scenes[0].image_path = Path("scene_001.jpg")
    out2 = ROOT / "data/tmp/test_script2.md"
    pipeline.write_script(story2, cfg, out2, 20.0)
    check_true("版权未明会标警示",
               "版权未明" in out2.read_text(encoding="utf-8"))

    meta = ROOT / "data/tmp/test_meta.json"
    pipeline.write_metadata(story, cfg, meta, 20.0, {"recheck_fail": 1})
    data = json.loads(meta.read_text(encoding="utf-8"))
    check("metadata 记录版权策略", data.get("license_policy"), "clean")
    check("metadata 记录未解决清单", data.get("unresolved"),
          ["分镜 1：把后来的制度说成城破当天发生"])
    check("metadata 里每个分镜带版权字段",
          data["chapters"][0]["scenes"][0].get("image_license"), "CC0 公共领域")


def test_client_and_tts_guards() -> None:
    print("\n[客户端重开 / TTS 全灭闸门]")
    from hsg import tts as tts_mod
    from hsg.config import ApiKeys
    from hsg.llm import LLM

    cfg = load_config()
    llm = LLM(cfg, ApiKeys.from_env())
    llm.close()
    first = llm._client
    check_true("close() 之后客户端处于关闭态", bool(getattr(first, "is_closed", False)))
    second = llm._ensure_client()
    check_true("再调一次会自动重开（不是同一个对象）", second is not first)
    check_true("重开的客户端是可用的", not second.is_closed)
    llm.close()

    check_true("TTSFailed 是可捕获的运行时错误",
               issubclass(tts_mod.TTSFailed, RuntimeError))
    check_true("edge 兜底音色能读到（tts.edge.voice_id）",
               str((cfg.tts.get("edge") or {}).get("voice_id") or "") != "")


def test_renumber_and_feedback() -> None:
    """改写之后的两个坑：分镜序号不重编、反馈串到别章。"""
    print("\n[pipeline.renumber_scenes / chapter_feedback]")
    from hsg import pipeline

    cfg = load_config()

    # ---- ① 改写出来的分镜是「每章从 0 起」编号的，重编之后必须是全局连续
    def _ch(idx, heading, texts):
        s = [Scene(index=k, text=t, chapter_index=idx, is_chapter_start=(k == 0))
             for k, t in enumerate(texts)]
        return Chapter(index=idx, heading=heading, scenes=s)

    story = Story(topic="t", title="t", chapters=[
        _ch(1, "一", ["第一章分镜甲，这里写满三十个字以避开过短判定。"]),
        _ch(2, "二", ["第二章分镜甲，这里写满三十个字以避开过短判定。",
                     "第二章分镜乙，这里写满三十个字以避开过短判定。"]),
    ])
    story.chapters[1].scenes[0].index = 0        # 模拟 rewrite_for_length 的产物
    story.chapters[1].scenes[1].index = 1
    pipeline.renumber_scenes(story)
    check("重编后分镜号全局连续", [s.index for s in story.all_scenes], [1, 2, 3])
    check("重编后每章首镜被标记",
          [s.is_chapter_start for s in story.all_scenes], [True, True, False])
    check("重编后不再是 0 号分镜", 0 in [s.index for s in story.all_scenes], False)

    # ---- ② 反馈只能含本章的问题
    by_idx = {s.index: s for s in story.all_scenes}
    issues = [
        {"scene": 1, "kind": "fail", "detail": "第一章的问题"},
        {"scene": 3, "kind": "fail", "detail": "第二章的问题"},
        {"scene": 2, "kind": "warn", "detail": "只是提示，不该进改写反馈"},
        {"scene": 0, "kind": "fail", "detail": "章节级结构问题（无分镜号）"},
    ]
    fb1 = pipeline.chapter_feedback(issues, by_idx, 1)
    fb2 = pipeline.chapter_feedback(issues, by_idx, 2)
    check_true("第一章反馈含本章问题", "第一章的问题" in fb1)
    check_true("第一章反馈不含第二章问题", "第二章的问题" not in fb1)
    check_true("第二章反馈含本章问题", "第二章的问题" in fb2)
    check_true("第二章反馈不含第一章问题", "第一章的问题" not in fb2)
    check_true("warn 不进改写反馈", "只是提示" not in fb1 + fb2)
    check_true("章节级问题（scene=0）不丢", "章节级结构问题" in fb1 and "章节级结构问题" in fb2)


def test_tts_speed_plumbing() -> None:
    """语速必须真的传到合成分支 —— edge 曾经静默按常速念。"""
    print("\n[tts 语速传导 TTS.__init__ / synthesize]")
    from hsg import tts as tts_mod

    cfg = load_config()

    # ---- minimax 分支读 minimax.speed
    cfg.tts["provider"] = "minimax"
    cfg.tts["minimax"]["speed"] = 1.05
    check("minimax 语速从配置读出", tts_mod.TTS(cfg).speed, 1.05)

    # ---- edge 分支读 edge.speed（修复前 TTS 根本不读它）
    cfg.tts["provider"] = "edge"
    cfg.tts["edge"]["speed"] = 1.1
    check("edge 语速从配置读出", tts_mod.TTS(cfg).speed, 1.1)

    # ---- 真的传给了 edge_synth（用桩函数截获，零成本）
    seen: dict = {}
    orig = tts_mod.edge_synth
    probe = ROOT / "data/tmp/tts_speed_probe.mp3"
    probe.unlink(missing_ok=True)      # ★ 必须清掉：残留文件会让 synthesize 走缓存短路，
                                       #   桩函数根本不会被调用（第二次跑就会假失败）

    def fake(text, out_path, voice="", speed=None):
        seen["speed"] = speed
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        Path(out_path).write_bytes(b"\x00" * 8192)      # 模拟产出一个可读的文件
        return Path(out_path)

    tts_mod.edge_synth = fake
    try:
        t = tts_mod.TTS(cfg)
        t.synthesize("这是一段用来验证语速传导的测试文本。", probe)
    finally:
        tts_mod.edge_synth = orig
        probe.unlink(missing_ok=True)
    check("edge 合成拿到配置语速（不是 None）", seen.get("speed"), 1.1)
    check_true("语速与缓存 key 的取值口径一致",
               t.speed == cfg.tts["edge"].get("speed"))


def test_cover() -> None:
    """封面：折行断点、尺寸、以及「真的画上去了字」（不是一张空图）。"""
    print("\n[封面 media.cover_title_layout / build_cover]")
    from PIL import Image, ImageStat

    from hsg import media

    check("封面标题在冒号处断行（全角）",
          media.cover_title_layout("一顿饭多少钱：古代打工人的三餐到底怎么吃？"),
          "一顿饭多少钱：\n古代打工人的三餐到底怎么吃？")
    check("半角冒号同样处理（顺带吃掉冒号后的空格）",
          media.cover_title_layout("赤壁之战: 一场大火改写了三国"),
          "赤壁之战:\n一场大火改写了三国")
    check("没有冒号的标题不动它",
          media.cover_title_layout("鸿门一宴，项羽为何放走刘邦？"),
          "鸿门一宴，项羽为何放走刘邦？")
    check("空标题不炸", media.cover_title_layout(""), "")

    # 副标题断行：视觉复核抓到「县官」被拆成「县/官」，这里把它钉住
    f = media._font(40)
    sub_text = ("在清朝道光年间，一个普通佃农、一个衙门差役、一个县官，"
                "各自一天花多少钱、吃什么东西？")
    lines = media._wrap_by_clause(sub_text, f, 1180, max_lines=4)
    check_true("按句断行不丢字（拼回去等于原文）",
               "".join(lines) == sub_text, f"→ {lines}")
    check_true("「县官」不会被拆到两行", any("县官" in ln for ln in lines), f"→ {lines}")
    check_true("没有一行以逗号/顿号开头",
               not any(ln and ln[0] in "，、。；" for ln in lines), f"→ {lines}")

    # ---- 标题折行：整段优先，绝不把词拆开 ----
    # 回归来源：第 7 期封面把「怎样」折成了「怎/样」（逐像素差分定位到 y 440–937）。
    # 根因：冒号后那段内部没有标点，_wrap_by_clause 当场退回硬折。
    _tw, _th = 1080, 1920
    _usable = _tw - 2 * int(_tw * 0.08)
    _base_px = int(min(_tw * 0.105, _th * 0.055))
    fit_f, fit_lines = media.cover_title_fit(
        media.cover_title_layout("夜里的城：宵禁之后出门会怎样？"),
        _base_px, _usable, int(_th * 0.40))
    check("标题折成 2 行（冒号处断一次，后半段整段占一行）", fit_lines,
          ["夜里的城：", "宵禁之后出门会怎样？"])
    check_true("「怎样」没被拆到两行", any("怎样" in ln for ln in fit_lines),
               f"→ {fit_lines}")
    check_true("标题不丢字（拼回去等于原文）",
               "".join(fit_lines) == "夜里的城：宵禁之后出门会怎样？", f"→ {fit_lines}")
    check_true("每一行都在可用宽度内",
               all(fit_f.getlength(ln) <= _usable for ln in fit_lines), f"→ {fit_lines}")
    # 更长的一期也要能不拆词（第 6 期标题：后半段 14 字）
    _, fit2 = media.cover_title_fit(
        media.cover_title_layout("一顿饭多少钱：古代打工人的三餐到底怎么吃？"),
        _base_px, _usable, int(_th * 0.40))
    check_true("第 6 期的长标题同样不拆词（「到底」完整）",
               any("到底" in ln for ln in fit2) and len(fit2) <= 3, f"→ {fit2}")
    # 超长标题：缩到下限还放不下，允许按标点断/硬折，但不能炸、不能超 3 行
    _, fit3 = media.cover_title_fit(
        media.cover_title_layout(
            "一个写得特别特别长的标题：后面这一段故意堆到字号缩到下限也放不下好触发兜底"),
        _base_px, _usable, int(_th * 0.40))
    check_true("超长标题不炸且行数 ≤3", 1 <= len(fit3) <= 3, f"→ {fit3}")

    cfg = load_config()
    tmp = ROOT / "data/tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    # 造一张纯色底图当配图，避免依赖已有的 scene_XXX.jpg
    src = tmp / "cover_src.jpg"
    Image.new("RGB", (1200, 1600), (60, 70, 90)).save(src, "JPEG", quality=88)

    out = tmp / "cover_test.jpg"
    p = media.build_cover(out, (1080, 1920), cfg, title="一顿饭多少钱：古代打工人的三餐到底怎么吃？",
                          kicker=cfg.video.channel_name,
                          subtitle="在清朝道光年间，一个普通佃农、一个衙门差役分别吃什么？",
                          foot=str(cfg.video.get("intro_slogan") or ""), image_path=src)
    with Image.open(p) as im:
        check("封面尺寸与竖屏一致", im.size, (1080, 1920))
        # 文字是白的/黄的 —— 画面里必须有足够多的亮像素，否则说明字没画上去
        bright = sum(1 for px in im.convert("L").getdata() if px > 200)
        stat = ImageStat.Stat(im.convert("L"))
        # ---- 底部标语必须躲开竖屏平台约 15% 的遮挡区 ----
        # 回归来源：上一版留白 0.115，实测字块落在距底 11.6%–13.2%，**仍在遮挡区内**
        # （用「重建纯背景层 + 逐像素差分」量出来的：字块 y 1667–1698 / 1920）。
        # 这里扫金色像素把它钉住；只扫下半幅，避开顶部的金色栏目名。
        # ⚠️ 必须写在 with 里面：PIL 出了 with 就关掉图像，getpixel 取不到了。
        def _goldish(px) -> bool:
            r, g, b = px[0], px[1], px[2]
            return r > 200 and g > 170 and b < 140

        gold_ys = [y for y in range(int(1920 * 0.5), 1920, 3)
                   for x in range(0, 1080, 3) if _goldish(im.getpixel((x, y)))]
        check_true("封面底部标语画上去了", bool(gold_ys), f"金像素={len(gold_ys)}")
        if gold_ys:
            check_true("标语底边在距底 15% 以上（躲开竖屏遮挡区）",
                       max(gold_ys) <= int(1920 * 0.85),
                       f"max_y={max(gold_ys)} → 距底 {100 * (1920 - max(gold_ys)) / 1920:.1f}%")
            check_true("标语没有飘到画面中部", min(gold_ys) > int(1920 * 0.7),
                       f"min_y={min(gold_ys)}")
    check_true("封面画上了文字（亮像素数量合理）", 3000 < bright < 200000,
               f"bright={bright}")
    check_true("封面不是纯色空图（有明暗层次）", stat.stddev[0] > 20,
               f"stddev={stat.stddev[0]:.1f}")
    out.unlink(missing_ok=True)
    src.unlink(missing_ok=True)


def test_clip_index() -> None:
    """素材库索引：登记、去重、多维检索、合规配额。"""
    print("\n[素材库 clips.add_clip / search / check_quota]")
    from hsg import clips

    idx = clips.empty_index()
    check("空索引结构", sorted(idx.keys()), ["clips", "version"])
    rec = clips.add_clip(idx, {"id": "sg_caocao_01", "file": "norm/sg_caocao_01.mp4",
                               "title": "三国演义", "year": 1994, "people": "曹操",
                               "era": "东汉末", "topic": "三国", "dur": 8.5,
                               "desc": "横槊赋诗前的特写"})
    check_true("登记一条（字符串标签切成列表）",
               rec is not None and rec.get("people") == ["曹操"], f"→ {rec}")
    check("id 重复被拦下",
          clips.add_clip(idx, {"id": "sg_caocao_01", "file": "x.mp4"}), None)
    check("缺 file 被拦下", clips.add_clip(idx, {"id": "sg_x"}), None)
    clips.add_clip(idx, {"id": "sg_liubei_01", "file": "norm/sg_liubei_01.mp4",
                         "title": "三国演义", "people": ["刘备"], "era": "东汉末",
                         "topic": ["三国"], "dur": 7.0})
    check("按人物检索", [c["id"] for c in clips.search(idx, people=["曹操"])],
          ["sg_caocao_01"])
    check("多人是任一命中", len(clips.search(idx, people=["曹操", "刘备"])), 2)
    check("按年代检索", len(clips.search(idx, era="东汉末")), 2)
    check("年代不匹配为空", clips.search(idx, era="唐代"), [])
    check("维度之间是 AND", len(clips.search(idx, people=["曹操"], era="唐代")), 0)
    check("按题材检索", len(clips.search(idx, topic=["三国"])), 2)
    check("按描述文本检索", [c["id"] for c in clips.search(idx, text="横槊")],
          ["sg_caocao_01"])
    check("空条件返回全部", len(clips.search(idx)), 2)
    check("按槽位检索（还没绑槽位时为空）", clips.search(idx, slot="s04_sh1"), [])
    check("删掉一条", clips.remove_clip(idx, "sg_caocao_01") and len(idx["clips"]), 1)

    # ---- 合规配额 ----
    cfg = load_config()
    check("配额内无告警",
          clips.check_quota(cfg, 100.0, [{"id": "a", "dur": 5.0}, {"id": "b", "dur": 5.0}]), [])
    bad = clips.check_quota(cfg, 100.0, [{"id": "long", "dur": 25.0}])
    check_true("单段超限被拦下", any("单段上限" in x for x in bad), f"→ {bad}")
    heavy = clips.check_quota(cfg, 30.0, [{"id": "a", "dur": 8.0}, {"id": "b", "dur": 8.0}])
    check_true("占比超限被拦下", any("占比" in x for x in heavy), f"→ {heavy}")
    check("占比算得对（16/30 = 53%）",
          round(clips.share_of(30.0, [{"id": "a", "dur": 8.0}, {"id": "b", "dur": 8.0}]), 2), 0.53)

    # ---- 配置里的路径键必须**真的被读**（否则就是假开关，audit_config 会标出来）----
    alt = load_config()
    check_true("clips.dir 生效", clips.clip_dir(alt).as_posix().endswith("data/clips"),
               f"→ {clips.clip_dir(alt)}")
    check_true("clips.index 生效",
               clips.index_path(alt).as_posix().endswith("data/clips/index.json"),
               f"→ {clips.index_path(alt)}")
    alt.clips["index"] = "data/tmp/alt_index.json"
    check_true("改配置真的会换路径（不是硬编码）",
               clips.index_path(alt).as_posix().endswith("data/tmp/alt_index.json"),
               f"→ {clips.index_path(alt)}")


def test_clip_normalize() -> None:
    """素材规范化：统一规格、**剥掉音轨**、截到时长上限（真跑 ffmpeg，零 API）。"""
    print("\n[素材库 clips.normalize_clip / probe]")
    from hsg import clips, video

    tmp = ROOT / "data/tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    src, dest = tmp / "clip_src_test.mp4", tmp / "clip_norm_test.mp4"
    broken = tmp / "clip_broken_test.mp4"
    # 造一条 14 秒、带音轨、1280x720@25 的"人工剪好的素材"
    # -preset ultrafast 是为了**测试能在内存紧张的机器上跑**（这台机器只剩 1.4GB
    # 可用时，x264 用默认 medium 编码 1080p 连 7MB 都分配不到，直接 malloc failed）。
    # 断言的是分辨率/帧率/去音轨/截断时长，跟编码 preset 无关，所以换掉不影响验证力。
    video.run_ffmpeg(["-f", "lavfi", "-i", "testsrc=size=1280x720:rate=25:duration=14",
                      "-f", "lavfi", "-i", "sine=frequency=440:duration=14",
                      "-c:v", "libx264", "-preset", "ultrafast",
                      "-pix_fmt", "yuv420p", "-c:a", "aac",
                      "-shortest", str(src)], cwd=tmp, desc="test_clip_src")
    before = clips.probe(src)
    check_true("测试素材本身带音轨（否则这条测试没意义）",
               bool(before.get("has_audio")), f"→ {before}")
    cfg = load_config()
    clips.normalize_clip(src, dest, cfg.clips.get("spec") or {},
                         float(cfg.clips.get("max_seconds", 10)), preset="ultrafast")
    after = clips.probe(dest)
    check("规范化后分辨率统一为 1920x1080", [after["width"], after["height"]], [1920, 1080])
    check("规范化后帧率统一为 30", str(after["fps"]).split("/")[0], "30")
    check_true("音轨已被剥掉", not after.get("has_audio"), f"→ {after}")
    check_true("时长被截到 10 秒上限内（输入 14 秒）",
               9.5 < after["duration"] <= 10.1, f"→ {after['duration']:.2f}s")

    # 损坏素材必须抛错，不能静默产出残废文件
    broken.write_bytes(b"not a video at all")
    try:
        clips.normalize_clip(broken, tmp / "clip_should_not_exist.mp4", {}, 10)
        check_true("损坏素材应当抛错", False, "→ 没有抛错")
    except video.FFmpegError:
        check_true("损坏素材抛 FFmpegError", True)
    for f in (src, dest, broken, tmp / "clip_should_not_exist.mp4"):
        f.unlink(missing_ok=True)


def test_needs() -> None:
    """素材需求清单：槽位切分、三级时长来源、优先级、覆盖度、工作单。"""
    print("\n[需求清单 needs.plan_slots / build_needs / coverage]")
    import json as _json
    import tempfile

    from hsg import clips, needs

    cfg = load_config()

    # ---- 槽位命名
    check("槽位名", needs.slot_name(4, 2), "s04_sh2")
    check("槽位名补零到两位（>9 也不乱）", needs.slot_name(12, 1), "s12_sh1")

    # ---- 切分：上限取 min(clips.max_seconds, needs.slot_seconds)
    check("短分镜不切", needs.plan_slots(6.0, cfg), [6.0])
    check("0 秒不切（没时长的分镜不进清单）", needs.plan_slots(0.0, cfg), [])
    s22 = needs.plan_slots(22.0, cfg)
    check("22 秒切 3 段", len(s22), 3)
    check_true("每段不超过目标节奏 8.5s", all(x <= 8.5 + 1e-9 for x in s22), f"→ {s22}")
    check_true("每段不超过合规单段上限 10s", all(x <= float(cfg.clips.max_seconds) for x in s22),
               f"→ {s22}")
    check_true("切完总长不变（不会凭空长出/少了秒数）",
               abs(sum(s22) - 22.0) < 1e-6, f"→ {sum(s22)}")
    s30 = needs.plan_slots(30.0, cfg)
    check_true("30 秒切 4 段且每段 ≥ 下限",
               len(s30) == 4 and all(x >= float(cfg.needs.min_shot_seconds) for x in s30),
               f"→ {s30}")
    check_true("极短分镜退化成 1 段而不是 0 段",
               len(needs.plan_slots(0.5, cfg)) == 1, f"→ {needs.plan_slots(0.5, cfg)}")

    # ---- 三级时长来源：实测 > metadata 记录 > 按字数估
    story = Story(
        topic="三国", title="曹操为什么杀吕伯奢", period="东汉末",
        chapters=[
            Chapter(index=1, heading="逃亡路上", scenes=[
                Scene(index=1, text="字" * 47, chapter_index=1, is_chapter_start=True,
                      image_query="曹操 逃亡"),
                Scene(index=2, text="字" * 100, chapter_index=1)]),
            Chapter(index=2, heading="那一句话", scenes=[
                Scene(index=3, text="字" * 100, chapter_index=2, is_chapter_start=True),
                # 31.9 秒的长分镜，但**不是**章节开场 → 不该进「必须」
                Scene(index=4, text="字" * 150, chapter_index=2)]),
        ])
    cps = float(cfg.story.chars_per_second)
    durs, src = needs.scene_durations(story, cfg, recorded={})
    check("没有实测也没有记录 → 按字数估", durs[1], round(47 / cps, 2))
    check("估算计数", src["estimated"], 4)
    cfg_alt = load_config()
    cfg_alt.story["chars_per_second"] = 5.0
    d5, _ = needs.scene_durations(story, cfg_alt, recorded={})
    check("换算值真的读配置（改了配置结果跟着变，不是写死的）", d5[1], round(47 / 5.0, 2))
    durs, src = needs.scene_durations(story, cfg, recorded={1: 12.5})
    check("metadata 的实测记录优先于估算", durs[1], 12.5)
    check("记录计数", src["metadata"], 1)
    story.chapters[0].scenes[0].duration = 9.9
    durs, src = needs.scene_durations(story, cfg, recorded={1: 12.5})
    check("音频文件探到的真值优先于记录", durs[1], 9.9)
    check("实测计数", src["audio"], 1)
    story.chapters[0].scenes[0].duration = 0.0

    # ---- 清单本体
    nn = needs.build_needs(story, cfg, None, recorded={1: 20.0})
    rows = nn["slots"]
    check("清单版本号", nn["version"], needs.NEEDS_VERSION)
    check("分镜 1（20.0s）切 3 个槽位", len([r for r in rows if r["scene"] == 1]), 3)
    check("槽位时长之和 = 分镜时长",
          round(sum(r["dur"] for r in rows if r["scene"] == 1), 2), 20.0)
    check("每个分镜的段数被回填", sorted({r["shots_in_scene"] for r in rows}), [3, 4])
    check("must 只落在每场的第 1 段",
          sorted({r["shot"] for r in rows if r["priority"] == "must"}), [1])
    check("章节开场是 must，其余是 nice",
          [r["priority"] for r in rows if r["scene"] == 1], ["must", "nice", "nice"])
    check("第 2 段起一律是复用（素材粒度=分镜）",
          sorted({r["fallback"] for r in rows if r["shot"] > 1}), ["reuse"])
    check("非关键场次的第 1 段回退到静态配图",
          [r["fallback"] for r in rows if r["scene"] == 2 and r["shot"] == 1], ["still"])
    # ★ 优先级与回退必须解耦：长分镜只改回退，不改优先级
    check("长分镜不进「必须」（否则每一场都是必须，优先级失效）",
          [r["priority"] for r in rows if r["scene"] == 4 and r["shot"] == 1], ["nice"])
    check("长分镜的第 1 段回退到 AI 生成（静态图撑不住）",
          [r["fallback"] for r in rows if r["scene"] == 4 and r["shot"] == 1], ["generate"])
    check("章节开场仍然是 must（优先级只认这一条）",
          sorted({r["scene"] for r in rows if r["priority"] == "must"}), [1, 3])
    check("关键位置的回退是 AI 生成",
          [r["fallback"] for r in rows if r["priority"] == "must"], ["generate", "generate"])
    check("规则兜底用配图的检索词当需求描述",
          [r["need"] for r in rows if r["scene"] == 1 and r["shot"] == 1], ["曹操 逃亡"])
    check("题材/年代带上（供导入时打标签用）",
          (rows[0]["topic"], rows[0]["era"]), (["三国"], "东汉末"))

    # ---- 覆盖度
    empty = needs.coverage(nn, clips.empty_index())
    check("空素材库：缺口 = 总槽位 − 复用槽位",
          len(empty["missing"]) + len(empty["reuse"]), len(rows))
    check("空素材库：真缺的槽位（每场 1 段 × 4 场）", len(empty["missing"]), 4)
    check("空素材库：复用槽位不算缺口", len(empty["reuse"]), 9)
    check("空素材库：关键位置的缺口", len(empty["missing_must"]), 2)
    check_true("真要剪的秒数低于总秒数", empty["fresh_seconds"] < empty["need_seconds"],
               f"→ {empty['fresh_seconds']} vs {empty['need_seconds']}")

    idx = clips.empty_index()
    clips.add_clip(idx, {"id": "sg_01", "file": "a.mp4", "title": "三国演义",
                         "people": ["曹操"], "era": "东汉末", "dur": 8.0,
                         "slots": ["s01_sh1"]})
    cov = needs.coverage(nn, idx)
    check("绑了槽位 → 已绑定", cov["bound"], ["s01_sh1"])
    check("同年代的其他槽位 → 有候选可复用", len(cov["suggested"]), len(rows) - 1)
    check("有候选就不算缺口", cov["missing"], [])
    check("素材库现有秒数被算出来", cov["have_seconds"], 8.0)

    # ---- LLM 补全（打桩，不花钱）
    class _FakeLLM:
        def __init__(self, payload=None, boom=False):
            self.payload, self.boom = payload, boom
            self.calls = 0

        def chat_json(self, system, user):
            self.calls += 1
            if self.boom:
                raise RuntimeError("模拟 LLM 挂了")
            return self.payload

    fake = _FakeLLM({"scenes": [{"index": 1, "need": "曹操策马夜行，中景，月光",
                                 "people": "曹操,陈宫", "callout": "亡命"}]})
    nn2 = needs.build_needs(story, cfg, fake, recorded={1: 20.0})
    r1 = [r for r in nn2["slots"] if r["scene"] == 1][0]
    check("LLM 补的镜头描述", r1["need"], "曹操策马夜行，中景，月光")
    check("人物字符串被切成列表", r1["people"], ["曹操", "陈宫"])
    check("标注词", r1["callout"], "亡命")
    check("一次调用覆盖全部分镜（不是每个分镜一次）", fake.calls, 1)
    check("没被覆盖到的分镜保留规则值",
          [r["need"] for r in nn2["slots"] if r["scene"] == 2][0], "字" * 34)

    boom = _FakeLLM(boom=True)
    nn3 = needs.build_needs(story, cfg, boom, recorded={1: 20.0})
    check("LLM 挂了要退回规则值，不能阻断出清单",
          [r["need"] for r in nn3["slots"] if r["scene"] == 1][0], "曹操 逃亡")
    nn4 = needs.build_needs(
        story, cfg, _FakeLLM({"scenes": [{"index": 1, "callout": "一二三四五六七"}]}),
        recorded={1: 20.0})
    check("过长/过短的标注词被丢掉（画面打不下）",
          [r["callout"] for r in nn4["slots"] if r["scene"] == 1][0], "")

    # ---- 工作单
    md = needs.to_markdown(nn2, cfg, clips.empty_index())
    check_true("工作单里有槽位名（导入时要照着抄）", "`s01_sh1`" in md, "")
    check_true("工作单里有导入命令", "import_clip.py" in md, "")
    check_true("工作单说明了哪些槽位不用单独剪", "复用本场素材" in md, "")
    check_true("工作单里带上了建议标注词", "亡命" in md, "")
    check_true("工作单里说清了单段规格（无音轨）", "无音轨" in md, "")
    check_true("工作单按分镜分组", "### 分镜 1" in md, "")

    with tempfile.TemporaryDirectory() as td:
        cfg2 = load_config()
        cfg2.paths["data_dir"] = td
        md_p, js_p = needs.save(nn2, cfg2, clips.empty_index())
        check_true("save 同时写 md（给人）和 json（给机器）",
                   md_p.exists() and js_p.exists(), f"→ {md_p}")
        back = _json.loads(js_p.read_text(encoding="utf-8"))
        check("json 能原样读回（渲染装配要用）", len(back["slots"]), len(nn2["slots"]))
        check_true("文件名带题材", "三国" in md_p.name, f"→ {md_p.name}")
        bad = dict(nn2)
        bad["topic"] = 'a/b:c*d?e"f'
        md_bad, _ = needs.save(bad, cfg2, clips.empty_index())
        check_true("文件名里的非法字符被清掉（Windows 会炸）",
                   not any(c in md_bad.name for c in '/\\:*?"<>|'), f"→ {md_bad.name}")


def test_edl() -> None:
    """剪辑表：素材绑定、排镜头、合规校验、切片占比。"""
    print("\n[剪辑表 edl.build_edl / validate_edl]")
    import tempfile

    from hsg import clips, edl

    cfg = load_config()
    cap = float(cfg.clips.max_seconds)

    # ---- 候选素材：默认只认显式绑定的（不许自动拿同年代素材顶替）
    idx = clips.empty_index()
    clips.add_clip(idx, {"id": "sg_a", "file": "norm/a.mp4", "title": "三国演义",
                         "people": ["曹操"], "era": "东汉末", "dur": 8.0,
                         "slots": ["s02_sh1", "s02_sh2"]})
    clips.add_clip(idx, {"id": "sg_b", "file": "norm/b.mp4", "title": "赤壁",
                         "people": ["曹操"], "era": "东汉末", "dur": 6.0})
    got = edl.scene_candidates(idx, 2, people=["曹操"], era="东汉末")
    check("只认绑定了本场槽位的素材", [c["id"] for c in got], ["sg_a"])
    check("没绑槽位的场次 → 一条候选都没有",
          edl.scene_candidates(idx, 3, people=["曹操"], era="东汉末"), [])
    loose = edl.scene_candidates(idx, 3, people=["曹操"], era="东汉末", allow_era_match=True)
    check("放开 allow_era_match 才会按年代兜（提示用，不该进剪辑表）",
          sorted(c["id"] for c in loose), ["sg_a", "sg_b"])

    # ---- 排镜头（规则兜底）
    rows = [{"slot": "s02_sh1", "dur": 5.0}, {"slot": "s02_sh2", "dur": 5.0}]
    cands = edl.scene_candidates(idx, 2, people=["曹操"], era="东汉末")
    shots = edl._rule_shots(cands, 13.0, rows, cfg)
    check("规则排镜头：总时长铺满分镜",
          round(sum(s.dur for s in shots), 2), 13.0)
    check_true("每个镜头都不超过单段上限",
               all(s.dur <= cap + 1e-9 for s in shots), f"→ {[s.dur for s in shots]}")
    check_true("取用区间不超出素材长度",
               all(s.src_in + s.dur <= 8.0 + 0.05 for s in shots),
               f"→ {[(s.src_in, s.dur) for s in shots]}")
    check("素材用第二次起标 reuse（同场不同段落）",
          [s.kind for s in shots], ["clip", "reuse"])
    check_true("第二次取用的是素材中段，不从同一格开始",
               shots[0].src_in == 0.0 and shots[1].src_in > 0, f"→ {shots[1].src_in}")

    # ---- 回退镜头
    fb = edl._fallback_shots([{"slot": "s03_sh1", "dur": 9.0, "fallback": "still"},
                              {"slot": "s03_sh2", "dur": 8.0, "fallback": "generate"}],
                             17.5, cfg)
    check("回退镜头也铺满时长", round(sum(s.dur for s in fb), 2), 17.5)
    check("回退类型跟着需求清单走（still / generate）",
          [s.kind for s in fb], ["still", "generate"])
    check("回退镜头一律静态图平移（没素材没什么可决策的）",
          {s.treatment for s in fb}, {"still_pan"})
    fb2 = edl._fallback_shots([{"slot": "s03_sh1", "dur": 4.0, "fallback": "still"}], 9.0, cfg)
    check("槽位时长不够时补到最后一个镜头（估算误差兜底）",
          round(sum(s.dur for s in fb2), 2), 9.0)

    # ---- 整期装配
    from hsg.models import Chapter, Scene, Story
    # ⚠️ 全局配置现在是 clips.enabled=false（用户要求镜头全部用生成图）。
    # 这个用例测的就是**切片链路**，必须自己把开关打开 —— 测试不该依赖全局配置，
    # 否则哪天改了默认值，这里会以"看不出原因"的方式挂掉。
    cfg.clips["enabled"] = True
    cfg.comic["enabled"] = False
    story = Story(topic="三国", title="曹操杀吕伯奢", period="东汉末", chapters=[
        Chapter(index=1, heading="逃亡路上", scenes=[
            Scene(index=1, text="字" * 40, chapter_index=1, is_chapter_start=True),
            Scene(index=2, text="字" * 40, chapter_index=1)]),
        Chapter(index=2, heading="磨刀声", scenes=[
            Scene(index=3, text="字" * 40, chapter_index=2, is_chapter_start=True)]),
    ])
    for s in story.all_scenes:
        s.duration = 12.0
    index = clips.empty_index()
    clips.add_clip(index, {"id": "c1", "file": "norm/c1.mp4", "title": "片甲",
                           "people": ["曹操"], "era": "东汉末", "dur": 9.0,
                           "slots": ["s01_sh1", "s01_sh2"]})
    needs = {"version": 1, "slots": [
        {"slot": "s01_sh1", "scene": 1, "chapter": 1, "shot": 1, "dur": 6.0,
         "people": ["曹操"], "era": "东汉末", "fallback": "generate", "callout": "亡命"},
        {"slot": "s02_sh1", "scene": 2, "chapter": 1, "shot": 1, "dur": 6.5,
         "people": [], "era": "东汉末", "fallback": "still"},
        {"slot": "s03_sh1", "scene": 3, "chapter": 2, "shot": 1, "dur": 6.5,
         "people": [], "era": "东汉末", "fallback": "generate"},
    ]}
    e = edl.build_edl(story, needs, index, cfg, None)
    check_true("整期 EDL：每场都有镜头",
               all(p["shots"] for p in e["scenes"]), f"{len(e['scenes'])} 场")
    check("整期 EDL：每场镜头总长 = 画面时长（旁白 + 尾垫）",
          [round(sum(s["dur"] for s in p["shots"]), 2) for p in e["scenes"]],
          [round(12.0 + float(cfg.video.tail_padding), 2)] * 3)
    kinds = {p["scene"]: {s["kind"] for s in p["shots"]} for p in e["scenes"]}
    check("绑了素材的场次用切片", kinds[1], {"clip", "reuse"})
    check("没绑素材的场次走回退（不碰别的素材）", kinds[2], {"still"})
    check("回退类型跟着需求清单", kinds[3], {"generate"})
    check("校验通过", edl.validate_edl(e, cfg, index), [])
    check_true("回退画面（静态图/生成图）不受切片单段 10 秒红线约束",
               all(any(float(s["dur"]) > float(cfg.clips.max_seconds)
                       for s in p["shots"] if s["kind"] in ("still", "generate"))
                   for p in e["scenes"] if p["scene"] in (2, 3)),
               "第 2/3 场各有一个 12.55s 的静态镜头，不该被报成红线")
    check_true("切片占比算得出来", 0 < edl.clip_share(e) < 1,
               f"{edl.clip_share(e):.1%}")

    # ---- 校验器要能拦住每一类问题（这些是出片前的闸门）
    bad = {"version": 1, "scenes": [{"scene": 1, "dur": 20.0, "shots": [
        {"slot": "s01_sh1", "kind": "clip", "clip_id": "c1", "src_in": 0.0,
         "dur": 12.0, "treatment": "plain"}]}]}
    p1 = edl.validate_edl(bad, cfg, index)
    check_true("拦下单镜头超上限", any("超过上限" in x for x in p1), f"{p1}")
    bad["scenes"][0]["shots"][0].update({"dur": 6.0, "treatment": "zoom"})
    p2 = edl.validate_edl(bad, cfg, index)
    check_true("拦下不认识的手法", any("不认识" in x for x in p2), f"{p2}")
    bad["scenes"][0]["shots"][0].update({"treatment": "plain", "clip_id": "不存在"})
    p3 = edl.validate_edl(bad, cfg, index)
    check_true("拦下不存在的素材 id", any("不在库里" in x for x in p3), f"{p3}")
    bad["scenes"][0]["shots"][0].update({"clip_id": "c1", "src_in": 7.0})
    p4 = edl.validate_edl(bad, cfg, index)
    check_true("拦下「取用区间超出素材长度」", any("只有" in x for x in p4), f"{p4}")
    bad3 = {"version": 1, "scenes": [{"scene": 1, "dur": 18.0, "shots": [
        {"slot": "s01_sh1", "kind": "clip", "clip_id": "c1", "src_in": 0.0,
         "dur": 6.0, "treatment": "plain"}]}]}
    p5 = edl.validate_edl(bad3, cfg, index)
    check_true("拦下镜头总长对不上画面时长（铺不满）",
               any("≠ 画面时长" in x for x in p5), f"{p5}")
    bad2 = {"version": 1, "scenes": [{"scene": 1, "dur": 20.0, "shots": [
        {"slot": "s01_sh1", "kind": "clip", "clip_id": "c1", "src_in": 0.0,
         "dur": 9.5, "treatment": "plain"},
        {"slot": "s01_sh2", "kind": "still", "dur": 10.5, "treatment": "still_pan"}]}]}
    p6 = edl.validate_edl(bad2, cfg, index)
    check_true("拦下切片占比超合规红线", any("切片占比" in x for x in p6), f"{p6}")
    check_true("describe 能出摘要", "场" in edl.describe(e), edl.describe(e))

    # ---- 存盘 / 读回
    with tempfile.TemporaryDirectory() as td:
        cfg2 = load_config()
        cfg2.paths["data_dir"] = td
        p = edl.save(e, cfg2)
        check_true("EDL 存盘", p.exists() and p.suffix == ".json", f"→ {p.name}")
        back = edl.load(p)
        check("读回一致", len(back["scenes"]), len(e["scenes"]))


def test_edl_overlong_shot() -> None:
    """导演让素材"出得比它本身还长"→ 该分镜退回规则排法（不是毙掉整集）。"""
    print("\n[edl 素材长度校验：超长镜头退回规则排法]")
    from hsg import edl as E
    from hsg.config import load_config
    from hsg.models import Chapter, Scene, Story

    cfg = load_config()
    cap = float(cfg.clips.get("max_seconds", 10.0))
    idx = {"clips": [{"id": "hg1", "dur": 7.0, "slots": ["s1_sh1", "s1_sh2", "s1_sh3"],
                      "title": "汉武大帝", "file": "norm/hg1.mp4", "width": 1920,
                      "height": 1080, "fps": "30/1"}]}
    sc = Scene(index=1, text="旁白", chapter_index=1, image_query="q")
    sc.duration = 20.0
    story = Story(topic="t", title="t", chapters=[Chapter(index=1, heading="一", scenes=[sc])])
    needs = {"topic": "t", "slots": [
        {"slot": f"s1_sh{i}", "scene": 1, "chapter": 1, "dur": 6.7, "priority": "must",
         "scene_priority": "must", "need": "x", "callout": "", "people": []}
        for i in (1, 2, 3)]}

    class _Over:
        def chat_json(self, *_a, **_k):
            return {"scenes": [{"scene": 1, "shots": [
                {"clip_id": "hg1", "src_in": 0, "dur": 9.0, "treatment": "plain",
                 "callout": "", "nametag": ""}]}]}

    edl = E.build_edl(story, needs, idx, cfg, _Over())
    shots = edl["scenes"][0]["shots"]
    # 只看真取素材的镜头（clip/reuse）；still 是回退静态图，不受素材长度约束
    bad = [s for s in shots if s.get("kind") in ("clip", "reuse")
           and float(s.get("src_in") or 0) + float(s["dur"]) > 7.0 + 0.05]
    check_true("没有「取超过素材长度」的镜头（渲染必崩的那种）", not bad, f"→ {bad[:2]}")
    check_true("整集仍然出得来（不是直接毙掉）", bool(shots), f"→ {len(shots)} 个镜头")
    check_true("每条镜头都不超过素材长度",
               all(float(s["dur"]) <= 7.0 + 0.05 for s in shots if s.get("kind") != "still"),
               f"→ {[(s['dur'], s.get('kind')) for s in shots]}")
    check_true("总长仍然铺满分镜时长",
               abs(sum(float(s["dur"]) for s in shots) - (20.0 + 0.55)) < 0.3,
               f"→ {sum(float(s['dur']) for s in shots)}")
    chk = E.validate_edl(edl, cfg, idx)
    check_true("校验通过（不再报「取不出这么长」）", not chk, f"→ {chk[:2]}")


def test_frames() -> None:
    """抽帧后的「定格放大」与画面上的标注位置（按像素验收，不靠眼睛）。"""
    print("\n[定格放大 / 标注元素 frames + media.draw_callout]")
    import tempfile

    from PIL import Image as _Image, ImageDraw as _Draw

    from hsg import frames, media

    cfg = load_config()
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        # ---- 造一张有规律条纹的图：竖条纹宽 20px
        src = td / "src.png"
        im = _Image.new("RGB", (320, 180), (20, 20, 20))
        d = _Draw.Draw(im)
        for i in range(16):
            if i % 2 == 0:
                d.rectangle([i * 20, 0, i * 20 + 19, 179], fill=(240, 240, 240))
        im.save(src)

        def stripe_width(img_path: Path) -> float:
            """中横排上「亮条」的平均宽度（像素）—— 放大倍数直接反映在这里。

            不用「数边缘」：JPEG 在硬边附近会振铃，边缘计数会被虚边带偏
            （实测 zoom=2 时数出 21 条边缘，比原图的 15 还多，方向都反了）。
            """
            with _Image.open(img_path) as x:
                g = x.convert("L")
                w = g.width
                px = g.crop((0, 90, w, 91)).load()
            widths, run = [], 0
            for i in range(w):
                if px[i, 0] > 128:
                    run += 1
                elif run:
                    widths.append(run)
                    run = 0
            if run:
                widths.append(run)
            return round(sum(widths) / len(widths), 1) if widths else 0.0

        w0 = stripe_width(src)
        check("原图亮条宽 20px（测试素材本身要对）", w0, 20.0)
        w2 = stripe_width(frames.zoom_frame(src, td / "z2.png", zoom=2.0))
        check_true("放大 2 倍 → 亮条约 40px（真的放大了）", 32 < w2 < 48, f"→ {w2}px")
        w9 = stripe_width(frames.zoom_frame(src, td / "z9.png", zoom=99.0))
        check_true("放大倍数被夹在 3.0（99 → 约 60px）", w9 > 50, f"→ {w9}px")
        check("放大 1.0 倍等于原样（不裁不缩）",
              stripe_width(frames.zoom_frame(src, td / "z1.png", zoom=1.0)), w0)
        far = frames.zoom_frame(src, td / "zf.png", zoom=1.0, focus=(0.98, 0.5))
        check_true("focus 可以指定放大位置（画面右侧）", far.exists())

        # ---- 标注元素：位置必须落在各自的带里，谁也不许侵入谁
        size = (int(cfg.video.orientations.landscape.width),
                int(cfg.video.orientations.landscape.height))
        fg_plain = td / "fg_plain.png"
        _, fg_plain = media.build_layers(td / "bg1.jpg", fg_plain, size, cfg, image_path=None,
                                         kicker="历史小故事 · 第1章 示例", title="示例章节")
        check("没给大字时左上角是空的（不许凭空多出字）",
              frames.ink_band(fg_plain, 0.075, 0.19, 0.0, 0.70, min_luma=170), 0)
        fg_mark = td / "fg_mark.png"
        _, fg_mark = media.build_layers(td / "bg2.jpg", fg_mark, size, cfg, image_path=None,
                                        kicker="历史小故事 · 第1章 示例", title="示例章节",
                                        callout="亡命东归", nametag="曹操")
        # 关键词位置：用户 2026-09-16 要求从画面正中挪到**左上角**
        check_true("大字落在左上角（纵向 0.075-0.19 / 左 70% 宽）",
                   frames.ink_band(fg_mark, 0.075, 0.19, 0.0, 0.70, min_luma=170) > 500,
                   f"→ {frames.ink_band(fg_mark, 0.075, 0.19, 0.0, 0.70, min_luma=170)} 像素")
        check_true("大字不再压在画面正中（旧位置必须空出来）",
                   frames.ink_band(fg_mark, 0.40, 0.52, min_luma=170) < 80,
                   f"→ {frames.ink_band(fg_mark, 0.40, 0.52, min_luma=170)} 像素")
        check_true("人名条落在左下带（0.57-0.67）",
                   frames.ink_band(fg_mark, 0.57, 0.67) > 200,
                   f"→ {frames.ink_band(fg_mark, 0.57, 0.67)} 像素")
        check("大字没侵入字幕/图注区（0.72 以下）",
              frames.ink_band(fg_mark, 0.74, 1.0) - frames.ink_band(fg_plain, 0.74, 1.0), 0)
        check_true("人名条没侵入字幕区",
                   frames.ink_band(fg_mark, 0.72, 1.0) == frames.ink_band(fg_plain, 0.72, 1.0),
                   "人名条与无语版本在字幕区应完全一致")
        check_true("章节标题还在顶部带（没被挤走）",
                   frames.ink_band(fg_mark, 0.085, 0.255) > 500,
                   f"→ {frames.ink_band(fg_mark, 0.085, 0.255)} 像素")

        # ---- 超长标注词：字号自适应，不溢出画布
        fg_long = td / "fg_long.png"
        _, fg_long = media.build_layers(td / "bg3.jpg", fg_long, size, cfg, image_path=None,
                                        callout="一二三四五六七八")
        with _Image.open(fg_long) as x:
            alpha = x.convert("RGBA").getchannel("A")
            bbox = alpha.getbbox()
        check_true("超长大字不溢出画布", bbox and bbox[2] <= size[0] and bbox[0] >= 0,
                   f"bbox={bbox}")

        # ---- 抽帧：素材不存在时必须报错，不能静默产出 0 字节
        try:
            frames.grab_frame(td / "不存在.mp4", 0.0, td / "f.jpg")
            check("抽帧失败要抛错", False, "居然没抛错")
        except Exception as exc:  # noqa: BLE001
            check_true("抽帧失败要抛错（不是静默出 0 字节）",
                       "0 字节" in str(exc) or "ffmpeg" in str(exc).lower(),
                       f"→ {type(exc).__name__}: {str(exc)[:60]}")


def test_silence_and_audio_track() -> None:
    """静音轨的容器/编码器要跟扩展名走（否则 AAC 塞 mp3 直接失败）。"""
    print("\n[静音轨 video.make_silence]")
    import tempfile

    from hsg import video as v

    cfg = load_config()
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        for name in ("a.mp3", "b.m4a"):
            p = v.make_silence(td / name, 1.5, cfg)
            info = v.probe_streams(p)
            a = info.get("audio") or {}
            check_true(f"{name} 生成成功且时长对", abs(float(info.get("duration") or 0) - 1.5) < 0.2,
                       f"{info.get('duration')}s codec={a.get('codec')}")
        check("扩展名决定编码器：.mp3 → mp3 / .m4a → aac",
              ((v.probe_streams(td / "a.mp3").get("audio") or {}).get("codec"),
               (v.probe_streams(td / "b.m4a").get("audio") or {}).get("codec")),
              ("mp3", "aac"))


def test_playlist_bgm() -> None:
    """按章节交替 BGM：区间换算、降级路径、有声区间判定。

    这一套是 2026-09-15 加的（用户要求 3/7/8 三首按章节交替）。
    两个坑都在这几条断言里：电平差 21 dB 的曲子交替会有一段变静音；
    Voxscape 开头 42 秒是无声铺垫，从 0 偏移切片那一章就是静音。
    """
    print("\n[BGM 交替 pipeline.chapter_spans / video.build_playlist_bed]")
    from hsg import pipeline, video

    # ---- 区间换算（纯函数）----
    # 片头 40s + 第1章 30+30 + 第2章 35+35 = 170s；BGM 从 40.4s 起
    tl = [(40.0, 0), (30.0, 1), (30.0, 1), (35.0, 2), (35.0, 2)]
    spans = pipeline.chapter_spans(tl, 40.4, 170.0)
    check("片头并进第 1 章、整体减掉 BGM 起点",
          [[round(s, 1), round(e, 1)] for s, e in spans], [[0.0, 59.6], [59.6, 129.6]])
    # 没有片头（start_at=0）
    spans2 = pipeline.chapter_spans([(30.0, 1), (30.0, 1), (35.0, 2), (35.0, 2)], 0.0, 130.0)
    check("没有片头时直接从 0 开始",
          [[round(s, 1), round(e, 1)] for s, e in spans2], [[0.0, 60.0], [60.0, 130.0]])
    # 片尾并入最后一章（音乐铺到片尾结束）
    # 片头 40s + 第1章 30s + 第2章 30s + 片尾 8s = 108s，BGM 从 40.4s 起
    tl3 = [(40.0, 0), (30.0, 1), (30.0, 2), (8.0, 0)]
    spans3 = pipeline.chapter_spans(tl3, 40.4, 108.0)
    check("片尾并入最后一章（音乐铺到成片结束）",
          [[round(s, 1), round(e, 1)] for s, e in spans3], [[0.0, 29.6], [29.6, 67.6]])
    check_true("最后一段的右端 = 成片总长 − BGM 起点（片尾没被漏掉）",
               abs(spans3[-1][1] - (108.0 - 40.4)) < 0.01, f"→ {spans3[-1][1]:.1f}")
    check("只有一章时也只有一段",
          len(pipeline.chapter_spans([(5.0, 1), (5.0, 1)], 0.0, 10.0)), 1)
    check("空时间轴不炸", pipeline.chapter_spans([], 0.0, 10.0), [])

    # ---- 降级路径（拼不起来就交回单曲模式）----
    cfg = load_config()
    tmpd = ROOT / "data/tmp"
    check("曲子不足 2 首 → None",
          video.build_playlist_bed(tmpd, [tmpd / "nope_a.mp3"], [(0, 10), (10, 20)], 20, cfg), None)
    check("章节不足 2 段 → None",
          video.build_playlist_bed(tmpd, [tmpd / "nope_a.mp3", tmpd / "nope_b.mp3"],
                                   [(0, 10)], 20, cfg), None)
    check("文件都不存在 → None（退回单曲）",
          video.build_playlist_bed(tmpd, [tmpd / "nope_a.mp3", tmpd / "nope_b.mp3"],
                                   [(0, 10), (10, 20)], 20, cfg), None)

    # ---- 配置与素材 ----
    pc = cfg.bgm.get("playlist") or {}
    check_true("配置里启用了交替", bool(pc.get("enabled")))
    files = [ROOT / str(f) for f in (pc.get("files") or [])]
    check_true("交替曲目 3 首且文件都在",
               len(files) == 3 and all(f.exists() for f in files),
               f"→ {[f.name for f in files]}")

    # ---- 有声区间（真实文件，零 API）----
    vx = ROOT / "data/bgm/playlist/03_voxscape.mp3"
    if vx.exists():
        lo, hi = video.safe_window(vx)
        check_true("Voxscape 开头一大段是无声铺垫（有声区间不从 0 开始）",
                   lo > 30.0, f"lo={lo:.1f}s")
        check_true("有声区间右端不超过曲子长度",
                   hi <= video.media_duration(vx) + 0.1, f"hi={hi:.1f}s")
        check_true("有声区间长度够铺一章（>60s）", hi - lo > 60.0, f"{hi - lo:.1f}s")
    else:
        print("  （跳过 Voxscape 有声区间测试：文件不在）")


def test_scene_kinds() -> None:
    """分镜画面类型判定 + 按类型分派风格后缀。

    这是第 7 期配图跑偏的修复：器物向后缀写死「整幅画面只有器物本身」，
    把叙事分镜也拉成了静物小品（要找巡夜兵丁给了灯笼、要找衙门审案给了西式法槌）。
    """
    print("\n[分镜类型 images.classify_scene_kinds / build_generate_prompt(kind)]")
    from hsg import images

    # ---- 规则兜底 ----
    check("「巡夜兵丁」→ scene", images._guess_kind_zh("明代京城巡夜兵丁古画"), "scene")
    check("「坊市布局图」→ scene", images._guess_kind_zh("唐代长安城坊市布局图 坊墙坊门遗址"),
          "scene")
    check("「衙门审案场景」→ scene", images._guess_kind_zh("清代衙门审案场景古画"), "scene")
    check("「街市夜市商铺」→ scene", images._guess_kind_zh("清明上河图 北宋东京街市 夜市 商铺"),
          "scene")
    check("「铜钱实物」→ object", images._guess_kind_zh("清代铜钱 串钱 道光通宝 实物"), "object")
    check("「古籍书影刑具实物」→ object",
          images._guess_kind_zh("唐律疏议古籍书影 笞杖刑具实物"), "object")
    check("空词保守判 object", images._guess_kind_zh(""), "object")

    cfg = load_config()
    rows = [(1, "明代京城巡夜兵丁古画"), (2, "清代铜钱 串钱 实物")]

    class _Fake:
        def __init__(self, payload):
            self.payload = payload

        def chat_json(self, *a, **k):
            return self.payload

    class _Boom:
        def chat_json(self, *a, **k):
            raise RuntimeError("boom")

    check("LLM 结论覆盖规则值",
          images.classify_scene_kinds(
              rows, cfg, _Fake({"kinds": [{"index": 1, "kind": "object"},
                                          {"index": 2, "kind": "scene"}]})),
          {1: "object", 2: "scene"})
    check("越界与非法值被丢弃、退回规则值",
          images.classify_scene_kinds(
              rows, cfg, _Fake({"kinds": [{"index": 9, "kind": "scene"},
                                          {"index": 1, "kind": "说不清"}]})),
          {1: "scene", 2: "object"})
    check("没有 llm 时用规则值",
          images.classify_scene_kinds(rows, cfg, None), {1: "scene", 2: "object"})
    check("LLM 异常时不炸、退回规则值",
          images.classify_scene_kinds(rows, cfg, _Boom()), {1: "scene", 2: "object"})
    check("空输入返回空", images.classify_scene_kinds([], cfg, None), {})
    _cfg_off = load_config()
    _cfg_off.images["classify_scene_kinds"] = False
    check("开关关闭时返回空（全部按器物向）",
          images.classify_scene_kinds(rows, _cfg_off, None), {})

    # ---- 后缀分派 ----
    sfx_obj = str(cfg.images.get("generate_style_suffix") or "")
    sfx_scn = str(cfg.images.get("generate_style_suffix_scene") or "")
    p_obj = images.build_generate_prompt("清代铜钱 串钱 实物", cfg)
    p_scn = images.build_generate_prompt("明代京城巡夜兵丁古画", cfg, "scene")
    check_true("默认（object）用器物后缀", sfx_obj in p_obj)
    check_true("kind=scene 用场景后缀", sfx_scn in p_scn)
    check_true("两条后缀不是同一条", bool(sfx_obj) and bool(sfx_scn) and sfx_obj != sfx_scn)
    check_true("场景后缀不再写死「只有器物本身」", "只有器物本身" not in sfx_scn)
    check_true("场景后缀以人物与场景为主体", "人物与场景" in sfx_scn)
    check_true("空 kind 走器物后缀", sfx_obj in images.build_generate_prompt("清代铜钱", cfg, ""))
    for _n, _s in (("器物", sfx_obj), ("场景", sfx_scn)):
        check_true(f"{_n}后缀都禁文字/印章/水印",
                   all(k in _s for k in ("文字", "印章", "水印")))
        check_true(f"{_n}后缀不含「工笔/绢本/摄影」（实测会诱发题跋或图库水印）",
                   not any(k in _s for k in ("工笔", "绢本", "摄影")))
    check_true("场景提示词仍在 1500 以内", len(p_scn) <= 1500, f"len={len(p_scn)}")


def test_scene_query_translation() -> None:
    """场景级英文检索词：解析、清洗、退回逻辑。"""
    print("\n[翻译 images.translate_scene_queries / _en_ok]")
    from hsg import images

    check_true("英文词通过校验", images._en_ok("Qing dynasty copper coins"))
    check_true("带引号的也通过（会清洗）", images._en_ok('"Ming dynasty painting"'))
    check_true("含汉字被挡掉", not images._en_ok("清代铜钱"))
    check_true("太短被挡掉", not images._en_ok("coins"))
    check_true("太长被挡掉", not images._en_ok("a b c d e f g h i j"))
    check_true("空的被挡掉", not images._en_ok(""))

    class _Fake:
        def __init__(self, payload):
            self.payload = payload

        def chat_json(self, *_a, **_kw):
            return self.payload

    cfg = load_config()
    rows = [(1, "清代 铜钱 串钱 道光通宝 实物"), (2, "清代 粥厂 施粥 古画"),
            (3, "清代 县衙差役 站班图 古画")]
    out = images.translate_scene_queries(rows, cfg, _Fake({"queries": [
        {"index": 1, "en": "Qing dynasty copper coins"},
        {"index": 2, "en": "清代铜钱"},            # 含汉字 → 丢
        {"index": 3, "en": "a"},                   # 太短 → 丢
        {"index": 9, "en": "Qing dynasty painting"},  # 不在输入里 → 丢
    ]}))
    check("只保留合法的、且必须在输入范围内", out, {1: "Qing dynasty copper coins"})

    # 关掉开关 → 一次调用都不发
    cfg.images["translate_scene_queries"] = False
    check("开关关掉后直接返回空（不调 LLM）", images.translate_scene_queries(rows, cfg, _Fake({})), {})
    cfg.images["translate_scene_queries"] = True

    # 没给 llm → 返回空，不炸
    check("没给 llm 时返回空", images.translate_scene_queries(rows, cfg, None), {})

    # LLM 抛异常 → 返回空（不阻断出片）
    class _Boom:
        def chat_json(self, *_a, **_kw):
            raise RuntimeError("boom")

    check("LLM 异常时静默退回", images.translate_scene_queries(rows, cfg, _Boom()), {})
    # 全是英文的输入（没有汉字）→ 不需要翻译
    check("输入里没有中文时不做翻译",
          images.translate_scene_queries([(1, "Qing dynasty painting")], cfg, _Fake({})), {})


def test_generate_image_prompt() -> None:
    """AI 生成配图的提示词拼装与开关守卫（不联网）。"""
    print("\n[生成配图 images.build_generate_prompt / generate_scene_image]")
    from hsg import images

    cfg = load_config()
    p = images.build_generate_prompt("清代 铜钱 串钱 道光通宝 实物", cfg)
    check_true("提示词含分镜检索词", "道光通宝" in p)
    # 不要断言后缀的具体措辞 —— 它按实测结论改过好几版（工笔/绢本会诱发书画题跋、
    # 「摄影」会诱发图库水印，最后定在器物静物向）。这里校验「意图」：
    # ① 配置里的后缀原样进了提示词；② 后缀必须同时禁掉文字、印章、水印。
    sfx = str(cfg.images.get("generate_style_suffix") or "")
    check_true("提示词里有风格后缀", bool(sfx) and sfx in p)
    check_true("风格后缀禁文字/印章/水印", all(k in sfx for k in ("文字", "印章", "水印")))
    check_true("提示词不超过接口上限 1500", len(p) <= 1500)

    cfg.images["generate_style_suffix"] = "只画器物，不要人"
    check_true("自定义风格后缀生效",
               images.build_generate_prompt("清代 铜钱", cfg) == "清代 铜钱。只画器物，不要人")
    check("空提示词时只用后缀", images.build_generate_prompt("", cfg), "只画器物，不要人")
    check("句尾多余句号被吃掉",
          images.build_generate_prompt("清代 铜钱。", cfg), "清代 铜钱。只画器物，不要人")
    cfg.images["generate_style_suffix"] = "A" * 2000
    check("超长时截断到 1500", len(images.build_generate_prompt("x", cfg)), 1500)

    # 开关关闭 / 缺 key → 直接返回空，且不发起请求
    cfg2 = load_config()
    cfg2.images["generate"] = False
    check("开关关闭时不生成",
          images.generate_scene_image(1, "清代 铜钱", cfg2, ROOT / "data/tmp"), (None, "", "", {}))


def test_series() -> None:
    """系列片：按集号取下一集、不进随机池、进度表、画面标签、落盘。"""
    print("\n[系列 topics.Series / next_episode / kicker_text]")
    import json
    import tempfile
    from pathlib import Path

    from hsg import history, pipeline, topics

    tmp = Path(tempfile.mkdtemp()) / "topics_user.json"
    S = "测试系列"
    pool = topics.UserPool(path=str(tmp))
    pool.series[S] = "这个系列讲什么（一句话）"
    # 故意乱序写入 + 集号跳号（模拟用户不是按顺序加的）
    pool.topics.append(topics.Topic(type="政变与权力", title="第三集标题", desc="d" * 20,
                                    series=S, ep=3))
    pool.topics.append(topics.Topic(type="政变与权力", title="第一集标题", desc="d" * 20,
                                    series=S, ep=1))
    pool.topics.append(topics.Topic(type="官府与吏治", title="普通单集", desc="d" * 20))
    topics.save_user_pool(pool)
    raw = json.loads(tmp.read_text(encoding="utf-8"))
    check_true("落盘带 series 段", "series" in raw and S in raw["series"], f"→ {list(raw)}")
    check_true("系列条目的 series/ep 单独存（不塞进标题）",
               raw["topics"][0].get("series") == S and raw["topics"][0].get("ep") == 3,
               f"→ {raw['topics'][0]}")
    check_true("非系列条目不写 series 字段", "series" not in raw["topics"][2],
               f"→ {raw['topics'][2]}")

    back = topics.load_user_pool(tmp)
    check("读回后系列说明不丢", back.series.get(S), pool.series[S])
    check_true("episodes() 按集号排序（写入顺序是乱的）",
               [t.ep for t in back.episodes(S)] == [1, 3],
               f"→ {[t.ep for t in back.episodes(S)]}")
    check_true("系列条目**不进**随机池（否则系列会被打乱）",
               all(not t.series for t in back.items("small")),
               f"→ {[t.title for t in back.items('small')]}")
    check_true("单集条目照旧能挑", any(t.title == "普通单集" for t in back.items("small")))

    # ---- 下一集：按集号升序 + 跳过已做过的
    nxt = topics.next_episode(back, S)
    check("没做过任何一集时 → 取集号最小的", nxt.ep, 1)
    done = {nxt.title}
    nxt2 = topics.next_episode(back, S, lambda x: x in done)
    check("第 1 集做过 → 取第 3 集", nxt2.ep, 3)
    nxt3 = topics.next_episode(back, S, lambda x: True)
    check("全做过 → None（不硬塞重复集）", nxt3, None)
    check("系列不存在 → None", topics.next_episode(back, "没有这个系列"), None)

    # ---- 进度表
    rec = {"title": "第一集标题", "generated_at": "2026-09-15T10:00:00"}
    txt = topics.series_progress(back, [rec], S)
    check_true("进度表标出已出的集", "✓" in txt and "2026-09-15" in txt, f"→ {txt[:120]}")
    check_true("进度表算出进度 1/2", "1/2" in txt, f"→ {txt[:120]}")
    check_true("进度表带 L1 类型（两级要看得全，不能只有标题）",
               "类型分布" in txt and "［政变与权力］" in txt, f"→ {txt[:200]}")
    check_true("类型分布的计数加起来 = 集数",
               all(int(x.split()[-1]) >= 1 for x in
                   txt.split("类型分布（L1）：")[1].split("\n")[0].split(" / ")),
               f"→ {txt.split('类型分布（L1）：')[1].splitlines()[0]}")
    check_true("没建系列时给出建系列的命令（不是空白）",
               "add_topic.py" in topics.series_progress(topics.UserPool(), [], ""))
    check_true("进度表认「成片标题」也能对上（生成记录字段有出入时不误报待做）",
               "✓" in topics.series_progress(
                   back, [{"title": "x", "topic": "第一集标题",
                           "generated_at": "2026-09-15"}], S))

    # ---- 画面标签
    st = __import__("hsg.models", fromlist=["Story"]).Story(topic="x", series=S, series_ep=3)

    class _Ch:
        index = 2
        heading = "权柄从哪里来"

    check("系列集 kicker = 栏目 · 系列 第N集", pipeline.kicker_text("历史小故事", st, _Ch()),
          f"历史小故事 · {S} 第3集")
    check_true("系列集 kicker 里不重复章节标题（下面那行大字就是）",
               "权柄" not in pipeline.kicker_text("历史小故事", st, _Ch()))
    check("封面（没章节参数）跟成片同口径",
          pipeline.kicker_text("历史小故事", st), f"历史小故事 · {S} 第3集")
    st2 = __import__("hsg.models", fromlist=["Story"]).Story(topic="x")
    check("单集照旧显示章节", pipeline.kicker_text("历史小故事", st2, _Ch()),
          "历史小故事 · 第2章 权柄从哪里来")
    st3 = __import__("hsg.models", fromlist=["Story"]).Story(topic="x", series=S)
    check("系列没写集号 → 不硬编「第0集」", pipeline.kicker_text("历史小故事", st3),
          f"历史小故事 · {S}")

    # ---- 系列标签在封面宽度里放得下（放不下会被 _wrap_cjk 静默截断）
    from PIL import ImageFont
    from hsg.media import _font, _wrap_cjk
    for name, w, h in (("竖屏", 1080, 1920), ("横屏", 1920, 1080)):
        f = _font(int(min(w * 0.040, h * 0.022)), bold=True)
        label = pipeline.kicker_text("历史小故事", st)
        lines = _wrap_cjk(label, f, int(w * (1 - 0.14)))
        check_true(f"封面 kicker 在{name}里一行放得下（不会被截断）", len(lines) == 1,
                   f"→ {len(lines)} 行 {label}")

    # ---- 生成记录 / metadata 带上系列（进度统计靠它）
    from hsg.config import load_config
    cfg = load_config()
    with tempfile.TemporaryDirectory() as td:
        cfg["paths"]["data_dir"] = td          # 别碰真实记录
        _st = __import__("hsg.models", fromlist=["Story"]).Story(
            topic="越策", title="越策标题", series=S, series_ep=3)
        rec = history.build_record(_st, cfg, total_seconds=10.0)
        check_true("build_record 带出 series/series_ep",
                   rec.get("series") == S and rec.get("series_ep") == 3, f"→ {rec.get('series')}")
        history.append_record(cfg, rec)
        got = [r for r in history.load(cfg) if r.get("topic") == "越策"]
        check_true("生成记录里有 series/series_ep（不然进度永远 0/N）",
                   got and got[0].get("series") == S and got[0].get("series_ep") == 3,
                   f"→ {got}")

    # ---- add_topic 的系列校验
    import subprocess
    import sys as _sys
    root = Path(__file__).resolve().parents[1]
    script = root / "scripts" / "add_topic.py"
    py = _sys.executable

    def _at(*argv):
        r = subprocess.run([py, str(script), "--file", str(tmp), *argv],
                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                           cwd=str(root))
        return (r.stdout or "") + (r.stderr or ""), r.returncode

    out, code = _at("--series", S, "--type", "政变与权力",
                    "--title", "缺集号的集：这条本该被拦下来才对？", "--desc", "d" * 20)
    check_true("系列片不给 --ep → 拦下（退出码 2）", code == 2 and "--ep" in out, f"→ {out.strip()[:80]}")
    out, code = _at("--series", S, "--ep", "1", "--type", "政变与权力",
                    "--title", "重复集号测试用的一条长标题内容", "--desc", "d" * 20)
    check_true("同一集号重复 → 拦下", code == 2 and "已经有" in out, f"→ {out.strip()[:80]}")
    out, code = _at("--series", S, "--ep", "2", "--type", "政变与权力",
                    "--title", "第二集标题：一条够长的切口问句内容", "--desc", "d" * 20)
    check_true("正常加一集", code == 0, f"→ {out.strip()[:120]}")
    back2 = topics.load_user_pool(tmp)
    check_true("加完之后集号连续（1/2/3）",
               [t.ep for t in back2.episodes(S)] == [1, 2, 3],
               f"→ {[t.ep for t in back2.episodes(S)]}")


def test_agent_plan_pick() -> None:
    """阶段 2 用哪份脚本：默认只认 plan 产物（别静默渲染错的一期）+ 多份在场的提醒。"""
    print("\n[agent.pick_plan / plan_candidates / pending_note]")
    import json
    import os
    import tempfile
    import time
    from pathlib import Path as _P

    from hsg import agent
    from hsg.config import load_config

    cfg = load_config()
    with tempfile.TemporaryDirectory() as td:
        cfg["paths"]["output_dir"] = td
        out = _P(td)

        def _write(name: str, topic: str, *, age: float = 0.0, series: str = "", ep: int = 0):
            f = out / name
            f.write_text(json.dumps({"topic": topic, "title": topic + "（标题）",
                                     "series": series, "series_ep": ep},
                                    ensure_ascii=False), encoding="utf-8")
            if age:
                old_t = time.time() - age
                os.utime(f, (old_t, old_t))
            return f

        # ① 只有非 plan 的 metadata（老路/整期跑完的产物）→ 兜底认它
        other = _write("20260101_某期_metadata.json", "整期跑完的旧产物")
        check("没有 plan 产物时兜底认最新的 metadata", agent.pick_plan(cfg).name, other.name)

        # ② plan 更旧、非 plan 更新（这正是会渲染错一期的场景）→ 必须仍然挑 plan
        plan = _write("20260102_plan_metadata.json", "待出片的这期", age=30)
        check_true("plan 更旧、别的 metadata 更新 → 仍然挑 plan（不挑更新的那份）",
                   agent.pick_plan(cfg).name == plan.name,
                   f"→ {agent.pick_plan(cfg).name}")
        check("显式 --metadata 优先于一切", agent.pick_plan(cfg, other).name, other.name)

        # ③ 候选按时间新→旧
        _write("20260103_plan_metadata.json", "更新的那期")
        check_true("plan_candidates 按时间新→旧",
                   [p.name for p in agent.plan_candidates(cfg)]
                   == ["20260103_plan_metadata.json", "20260102_plan_metadata.json"],
                   f"→ {[p.name for p in agent.plan_candidates(cfg)]}")

        # ④ 题材摘要：日志里只报文件名，人看不出是哪一期
        ser = _write("20260104_plan_metadata.json", "霍光：一个臣子凭什么能换掉皇帝？",
                     series="古代十大权臣", ep=1)
        check("metadata 摘要带系列与集号（不然看不出是哪一集）",
              agent.meta_brief(ser), "《古代十大权臣》第1集 霍光：一个臣子凭什么能换掉皇帝？")
        check("读不出来的 metadata → 空摘要（不让流程挂掉）",
              agent.meta_brief(out / "不存在的.json"), "")

        # ⑤ 多份在场的提醒
        note = agent.pending_note(cfg, ser)
        check_true("多份脚本在场 → 提醒里摊开清单", "--metadata" in note and "共" not in note[:2],
                   f"→ {note.splitlines()[0] if note else '（空）'}")
        check_true("提醒里点出默认用哪份", agent.pick_plan(cfg).name in note,
                   f"→ {note.splitlines()[0] if note else '（空）'}")
        # 只有一份 plan 时不该刷提示（没人愿意每跑一次都看一段清单）
        with tempfile.TemporaryDirectory() as td2:
            cfg2 = load_config()
            cfg2["paths"]["output_dir"] = td2
            only = _P(td2) / "only_plan_metadata.json"
            only.write_text(json.dumps({"topic": "只有这一期"}, ensure_ascii=False),
                            encoding="utf-8")
            check_true("只有一份脚本时不刷提示", agent.pending_note(cfg2, only) == "",
                       f"→ {agent.pending_note(cfg2, only)!r}")


def test_sources() -> None:
    """取景单：候选片源（未核实）+ 检索式 + 防编造 + 降级路径 + 插进需求清单。"""
    print("\n[sources 取景单]")
    import json
    import tempfile
    from pathlib import Path as _P

    from hsg import needs as N
    from hsg import sources as S
    from hsg.config import load_config
    from hsg.models import Chapter, Scene, Story

    # ---- ① 关键词兜底用章节标题，不是截断旁白（踩过：搜出来是废词）
    slot = {"slot": "s01_sh1", "callout": "遗诏托孤", "heading": "遗诏辅政：权从哪来？",
            "people": ["汉武帝", "霍光"], "era": "西汉昭帝、宣帝年间",
            "need": "汉武帝病榻前将一幅周公负成王图交予霍光，近景，烛光昏暗。"}
    check("兜底关键词 = 标注词 + 章节标题", S.fallback_keywords(slot), ["遗诏托孤", "遗诏辅政"])
    qs = S.queries_for(slot, [{"title": "汉武大帝"}], [])
    check_true("检索式里用剧名 + 关键词（能直接搜）", "汉武大帝 遗诏托孤" in qs, f"→ {qs}")
    check_true("检索式里不出现整句旁白（截断旁白是废词）",
               not any("病榻前" in x for x in qs), f"→ {qs}")
    check_true("检索式去重且有上限", len(qs) == len(set(qs)) and len(qs) <= 5, f"→ {qs}")
    check_true("模型给了关键词就用模型的",
               "汉武大帝 托孤" in S.queries_for(slot, [{"title": "汉武大帝"}], ["托孤"]))
    qs_none = S.queries_for(slot, [], [])
    check_true("连候选片源都没有时，仍给出人物/标注检索式（不空手）", bool(qs_none), f"→ {qs_none}")

    # ---- ② 防编造：模型塞集数/时间码必须被剪掉
    check_true("识别「第58集」", bool(S.FABRICATED.search("第58集")), "第58集")
    check_true("识别「12:30」", bool(S.FABRICATED.search("12:30")), "12:30")
    check_true("不误伤剧情阶段描述", not S.FABRICATED.search("汉武帝晚年、大结局前后"))

    class _FakeLLM:
        def __init__(self, payload):
            self.payload = payload

        def chat_json(self, *_a, **_k):
            return self.payload

    fake = _FakeLLM({"shots": [{
        "index": 1, "keywords": ["托孤", "周公负成王图"],
        "candidates": [
            {"title": "汉武大帝", "year": "2005", "locate": "汉武帝晚年 12:30 左右",
             "episodes_total": 58, "ep_range": "56-58", "in_episode": "集尾",
             "basis": "托孤是全剧收尾剧情", "why": "汉武帝晚年有托孤戏", "confidence": "high"},
            {"title": "乌龙闯情关", "year": "2002", "locate": "霍光擅权阶段",
             "episodes_total": 40, "ep_range": "第38~45集", "in_episode": "第 12 分 30 秒",
             "why": "讲刘询从民间登基", "confidence": "mid"},
        ]}]})

    class _DeadLLM:
        def chat_json(self, *_a, **_k):
            raise RuntimeError("模型挂了")

    need_obj = {
        "version": 1, "topic": "霍光：一个臣子凭什么能换掉皇帝？", "title": "霍光废帝",
        "era": "西汉", "generated_at": "2026-09-16 11:03", "slot_seconds": 8.5,
        "duration_source": {"audio": 0, "metadata": 0, "estimated": 3},
        "narration_seconds": 30.0,
        "slots": [
            {**slot, "scene": 1, "chapter": 1, "dur": 6.6, "priority": "must",
             "scene_priority": "must", "narration": "…"},
            {"slot": "s01_sh2", "scene": 1, "chapter": 1, "dur": 6.6, "priority": "reuse",
             "scene_priority": "must", "people": ["汉武帝"], "era": "西汉",
             "callout": "遗诏托孤", "heading": "遗诏辅政：权从哪来？", "need": "…"},
            {"slot": "s02_sh1", "scene": 2, "chapter": 1, "dur": 7.0, "priority": "nice",
             "scene_priority": "nice", "people": [], "era": "西汉", "callout": "",
             "heading": "第二章", "need": "…"},
        ],
    }
    cfg = load_config()

    src = S.build_sources(need_obj, cfg, fake)
    check("只给必须剪的槽位出取景单（其余不浪费 token）", len(src["items"]), 1)
    it = src["items"][0]
    check("槽位对得上", it["slot"], "s01_sh1")
    c0, c1 = it["candidates"][0], it["candidates"][1]
    check_true("时间码（分钟级）被剪掉 —— 那个精度是编的",
               "12:30" not in (c0["locate"] or ""), f"→ {c0['locate']}")
    check("推算集数区间被解析出来", c0["ep_range"], [56, 58])
    check("全剧进度由代码算（不让模型算）", c0["ep_progress"], "97%–100%")
    check("集内位置保留分段词", c0["in_episode"], "集尾")
    check_true("区间超出总集数 → 收窄到总集数并标出矛盾",
               c1["ep_range"] == [38, 40] and "超出总集数" in c1["ep_note"],
               f"→ {c1['ep_range']} {c1['ep_note']}")
    check_true("集内位置写成「第 12 分 30 秒」这种 → 丢掉（只认分段词）",
               c1["in_episode"] == "", f"→ {c1['in_episode']!r}")
    check_true("检索式里带上「剧名 第N集」（整集上传可直接搜到）",
               "汉武大帝 第56集" in it["queries"], f"→ {it['queries']}")
    check_true("预填的 import 命令绑上同场所有槽位（一条素材填满一场）",
               "s01_sh1,s01_sh2" in it["import_cmd"], f"→ {it['import_cmd']}")

    # ---- ②b 编号对齐（踩过：enumerate 从 0 起 → 每条候选错位一格）
    three = dict(need_obj)
    three["slots"] = [
        {**need_obj["slots"][0], "slot": "s01_sh1", "scene": 1, "need": "甲画面", "priority": "must"},
        {**need_obj["slots"][0], "slot": "s02_sh1", "scene": 2, "need": "乙画面", "priority": "must"},
        {**need_obj["slots"][0], "slot": "s03_sh1", "scene": 3, "need": "丙画面", "priority": "must"},
    ]

    class _Ordered:
        def chat_json(self, system, user, **_kw):
            # 回显收到的清单编号，并按编号给出各自的候选片源
            self_saw.append([ln.split(". 画面：")[0] for ln in user.splitlines()
                             if ". 画面：" in ln])
            return {"shots": [
                {"index": 1, "keywords": ["甲词"], "candidates": [{"title": "甲剧"}]},
                {"index": 2, "keywords": ["乙词"], "candidates": [{"title": "乙剧"}]},
                {"index": 3, "keywords": ["丙词"], "candidates": [{"title": "丙剧"}]},
            ]}

    self_saw: list[list[str]] = []
    got3 = S.build_sources(three, cfg, _Ordered())
    check("发给模型的清单编号从 1 开始（不是 0）", self_saw[0], ["1", "2", "3"])
    check_true("候选片源跟槽位一一对齐（不整体错位一格）",
               [i["candidates"][0]["title"] for i in got3["items"]] == ["甲剧", "乙剧", "丙剧"],
               f"→ {[(i['slot'], i['candidates'][0]['title']) for i in got3['items']]}")
    check_true("检索式跟着自己的槽位走",
               all(f"{k} 影视片段" in i["queries"] for k, i in
                   zip(["甲词", "乙词", "丙词"], got3["items"])),
               f"→ {[i['queries'][:2] for i in got3['items']]}")

    # ---- ③ 降级：模型挂了也要能用（只有检索式，不编候选）
    src_dead = S.build_sources(need_obj, cfg, _DeadLLM())
    check("模型挂了 → 不编候选片源", src_dead["items"][0]["candidates"], [])
    check_true("模型挂了 → 检索式照出（照搜不误）", bool(src_dead["items"][0]["queries"]),
               f"→ {src_dead['items'][0]['queries']}")
    check("llm_used 标明没问出来", src_dead["llm_used"], False)

    # ---- ②c 集数区间解析与校验（纯函数，全是确定性活）
    check("「第56~58集」→ (56,58)", S.parse_ep_range("第56~58集"), (56, 58))
    check("「56-58」→ (56,58)", S.parse_ep_range("56-58"), (56, 58))
    check("单个数字「57」→ (57,57)", S.parse_ep_range("57"), (57, 57))
    check("倒着写也认（58-56）", S.parse_ep_range("58-56"), (56, 58))
    check("瞎写 → None", S.parse_ep_range("不知道"), None)
    check("空 → None", S.parse_ep_range(""), None)
    check("进度算得对（56-58 / 58 集）", S.ep_progress(56, 58, 58), "97%–100%")
    check("没给总集数 → 不给百分比（不瞎算）", S.ep_progress(56, 58, 0), "")
    check_true("上界超出 → 收到总集数 + 备注（30-45 / 40 集 → 30-40）",
               S.check_ep_range(30, 45, 40)[0] == (30, 40)
               and "已收到 40" in S.check_ep_range(30, 45, 40)[1],
               f"→ {S.check_ep_range(30, 45, 40)}")
    check_true("整个区间都在总集数之外 → 不硬收窄，保留原值但明确告警"
               "（模型可能连总集数都记错了，收窄反而给它背书）",
               S.check_ep_range(90, 95, 40)[0] == (90, 95)
               and "超出总集数" in S.check_ep_range(90, 95, 40)[1],
               f"→ {S.check_ep_range(90, 95, 40)}")
    check("正常区间 → 不改动", S.check_ep_range(56, 58, 58)[0], (56, 58))

    # ---- ②d 区间过宽要标出来（不然「第 5–40 集」等于没给）
    class _Wide:
        def chat_json(self, *_a, **_k):
            return {"shots": [{"index": 1, "keywords": ["朝会"], "candidates": [
                {"title": "汉武大帝", "cast": "陈宝国", "episodes_total": 58,
                 "ep_range": "5-40", "in_episode": "中段", "locate": "朝会"}]}]}

    class _Unsure:
        def chat_json(self, *_a, **_k):
            return {"shots": [{"index": 1, "keywords": ["x"], "candidates": [
                {"title": "可能编的剧", "cast": "无", "in_cover": "in"},
                {"title": "汉武大帝", "cast": "陈宝国", "in_cover": "in"}]}]}

    def _vs(system, user, **_kw):
        # 自检把「可能编的剧」判为 unsure
        return {"verdicts": [{"title": "可能编的剧", "verdict": "unsure"},
                             {"title": "汉武大帝", "verdict": "sure"}]}

    class _UnsureWithCheck(_Unsure):
        def chat_json(self, system, user, **kw):
            if "核对员" in system:
                return _vs(system, user)
            return super().chat_json(system, user, **kw)

    src_u = S.build_sources(need_obj, cfg, _UnsureWithCheck())
    check_true("自检怀疑的候选排到最后（别挡在前面）",
               [c["title"] for c in src_u["items"][0]["candidates"]] == ["汉武大帝", "可能编的剧"],
               f"→ {[c['title'] for c in src_u['items'][0]['candidates']]}")

    md_wide = S.to_markdown(S.build_sources(need_obj, cfg, _Wide()))
    check_true("区间宽到 ≥15 集 → 标「参考价值低」并给替代做法",
               "参考价值低" in md_wide and "空镜" in md_wide,
               f"→ {[l for l in md_wide.splitlines() if '参考价值低' in l]}")
    class _Narrow:
        def chat_json(self, *_a, **_k):
            return {"shots": [{"index": 1, "keywords": ["托孤"], "candidates": [
                {"title": "汉武大帝", "cast": "陈宝国", "episodes_total": 58,
                 "ep_range": "56-58", "in_episode": "集尾", "locate": "托孤"}]}]}

    check_true("窄区间不标「参考价值低」（别把有用的也劝退）",
               "参考价值低" not in S.to_markdown(S.build_sources(need_obj, cfg, _Narrow())))

    # ---- ②e 覆盖范围：拍不到这事的剧要剔掉（踩过：把汉武帝驾崩后十几年的事
    #          推荐给「讲汉武帝一生」的《汉武大帝》，依据还写「应在剧末」）
    class _Cover:
        def chat_json(self, *_a, **_k):
            return {"shots": [{"index": 1, "keywords": ["废帝"], "candidates": [
                {"title": "汉武大帝", "cast": "陈宝国", "cover_to": "到汉武帝驾崩为止",
                 "in_cover": "out", "episodes_total": 58, "ep_range": "56-58"},
                {"title": "云中歌", "cast": "Angelababy", "cover_to": "覆盖昭帝、宣帝两朝",
                 "in_cover": "in", "episodes_total": 45, "ep_range": "30-44"},
                {"title": "某剧", "cast": "某人", "cover_to": "说不准 (in_cover 未定)",
                 "in_cover": "unknown", "episodes_total": 40, "ep_range": "20-30"},
            ]}]}

    src_cov = S.build_sources(need_obj, cfg, _Cover())
    titles = [c["title"] for c in src_cov["items"][0]["candidates"]]
    check_true("范围外的候选被剔掉（不推荐拍不到这事的剧）", "汉武大帝" not in titles, f"→ {titles}")
    check_true("范围外的记录在案（不静默消失）",
               any("汉武大帝" in d for d in src_cov["dropped_out_of_cover"]),
               f"→ {src_cov['dropped_out_of_cover']}")
    check_true("范围内的候保留", "云中歌" in titles, f"→ {titles}")
    check_true("没说清覆盖范围的照样给（但标 unknown）",
               any(c["title"] == "某剧" and c["in_cover"] == "unknown"
                   for c in src_cov["items"][0]["candidates"]), f"→ {titles}")
    md_cov = S.to_markdown(src_cov)
    cov_lines = [l.strip() for l in md_cov.splitlines() if "覆盖到" in l]
    check_true("渲染出「剧中覆盖到」",
               any("剧中覆盖到：覆盖昭帝、宣帝两朝" in l for l in cov_lines),
               f"→ {cov_lines}")
    check_true("覆盖范围说不准的标「未核实」（范围内/外是模型自述，别当事实）",
               any("模型自述" in l for l in cov_lines), f"→ {cov_lines}")
    check_true("模型挂了的时候没有这个字段也不报错",
               S.build_sources(need_obj, cfg, _DeadLLM())["dropped_out_of_cover"] == [])

    # ---- ②f 多轮合并：模型每轮给的候选都不一样，刷新不该覆盖
    A = [{"title": "汉武大帝", "confidence": "mid", "cast": "陈宝国"},
         {"title": "云中歌", "confidence": "high"}]
    B = [{"title": "汉武大帝", "confidence": "high", "cast": "陈宝国", "ep_range": [56, 58]},
         {"title": "乌龙闯情关", "confidence": "low"}]
    m = S.merge_candidates(A, B)
    check("合并后按剧名去重（3 条，不是 4 条）", [c["title"] for c in m],
          ["汉武大帝", "云中歌", "乌龙闯情关"])
    check("重复的剧名保留置信度更高的那条", m[0]["confidence"], "high")
    check_true("同时补齐字段（不丢上一轮的信息）",
               m[0].get("cast") == "陈宝国" and m[0].get("ep_range") == [56, 58],
               f"→ {m[0]}")
    check("上一轮的候选不会被本轮换掉（云中歌还在）", "云中歌" in [c["title"] for c in m], True)
    check("空的上轮 → 等于本轮", [c["title"] for c in S.merge_candidates([], B)],
          ["汉武大帝", "乌龙闯情关"])

    class _Round2:
        def __init__(self):
            self.n = 0

        def chat_json(self, *_a, **_k):
            self.n += 1
            return {"shots": [{"index": 1, "keywords": ["废帝"], "candidates": (
                [{"title": "云中歌", "cast": "Angelababy", "in_cover": "in"}]
                if self.n == 1 else
                [{"title": "乌龙闯情关", "cast": "孙耀威", "in_cover": "in"}])}]}

    r2 = _Round2()
    first = S.build_sources(need_obj, cfg, r2)
    second = S.build_sources(need_obj, cfg, r2, previous=first)
    check("第二轮把第一轮的候选并进来了（不是覆盖）",
          [c["title"] for c in second["items"][0]["candidates"]],
          ["云中歌", "乌龙闯情关"])
    check("标记出「并过上一轮」", second["merged_from_previous"], True)
    check("第一轮时没有这个标记", first["merged_from_previous"], False)

    # ---- ③b 剧名锚点：给不出主演的要标明「可能编的」，no_footage 要显眼
    class _Anchors:
        def chat_json(self, *_a, **_k):
            return {"shots": [{"index": 1, "keywords": ["托孤"], "candidates": [
                {"title": "汉武大帝", "year": "2005", "cast": "陈宝国、焦晃"},
                {"title": "编出来的剧", "year": "2020"},
            ]}]}

    src_anchor = S.build_sources(need_obj, cfg, _Anchors())
    md_a = S.to_markdown(src_anchor)
    check_true("有主演的候选照实显示", "陈宝国" in md_a, "陈宝国")
    check_true("给不出主演的候选被标「很可能是编的」（模型会编剧名）",
               "很可能是编的" in md_a, "很可能是编的")

    class _None:
        def chat_json(self, *_a, **_k):
            return {"shots": [{"index": 1, "no_footage": True, "keywords": ["无"]}]}

    md_none = S.to_markdown(S.build_sources(need_obj, cfg, _None()))
    check_true("模型说「没有影视剧拍过」时给出替代方案提示",
               "没有影视剧拍过" in md_none or "no_footage" in md_none or "回退画面" in md_none,
               f"→ {md_none[md_none.find('### 1'):][:120]}")

    # ---- ④ 上限
    many = dict(need_obj)
    many["slots"] = [{**s, "priority": "must", "scene": i + 1, "slot": f"s{i+1:02d}_sh1"}
                     for i, s in enumerate([need_obj["slots"][0]] * 20)]
    cfg2 = load_config()
    cfg2["needs"]["sources_max_slots"] = 5
    check("sources_max_slots 生效", len(S.build_sources(many, cfg2, fake)["items"]), 5)

    # ---- ⑤ 写进需求清单（只读一个文件），且排在槽位明细前面
    md_text = S.to_markdown(src)
    check_true("取景单标明「未核实」", "未核实" in md_text, "未核实")
    check_true("取景单给出回填栏（片源/集数/起止时间）",
               "起止时间" in md_text and "片源 ＝" in md_text)
    check_true("取景单给出可复制的检索式", "汉武大帝 托孤" in md_text, "汉武大帝 托孤")

    with tempfile.TemporaryDirectory() as td:
        cfg3 = load_config()
        cfg3["paths"]["data_dir"] = td
        md, js = N.save(need_obj, cfg3, None, sources=src)
        body = md.read_text(encoding="utf-8")
        check_true("需求清单 .md 里有取景单一节", "## 取景单" in body)
        check_true("取景单排在槽位明细之前（先看去哪找，再看每场几段）",
                   body.index("## 取景单") < body.index("## 槽位明细"))
        p = S.save(src, cfg3)
        check_true("取景单 json 落盘", p.exists() and "取景单" in p.name, f"→ {p.name}")
        check("json 里 items 完整", len(json.loads(p.read_text(encoding="utf-8"))["items"]), 1)


def test_crop_subtitle_band() -> None:
    """硬字幕/台标只能裁：按像素验收「裁完之后底部那条带没了」。"""
    print("\n[clips.normalize_clip 裁切去字幕带]")
    import shutil
    import subprocess
    import tempfile
    from pathlib import Path as _P

    from PIL import Image

    from hsg import clips as C
    from hsg.config import load_config

    cfg = load_config()
    spec = cfg.clips.get("spec") or {}
    FF = shutil.which("ffmpeg")      # 跟 video.run_ffmpeg 同一个来源（PATH）
    if not FF:
        check("环境里没有 ffmpeg", False, "跳过")
        return

    with tempfile.TemporaryDirectory() as td:
        tdp = _P(td)
        # 造假素材：上 80% 绿 + 下 20% 品红（模拟烧死在画面里的对白字幕带）
        src = (tdp / "with_subs.mp4").resolve()
        subprocess.run([FF, "-hide_banner", "-loglevel", "error", "-y",
                        "-f", "lavfi", "-i", "color=c=green:s=1920x864",
                        "-f", "lavfi", "-i", "color=c=magenta:s=1920x216",
                        "-filter_complex", "[0:v][1:v]vstack", "-t", "3",
                        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                        str(src)], check=True)

        def bottom_px(mp4) -> tuple:
            png = tdp / "f.png"
            subprocess.run([FF, "-hide_banner", "-loglevel", "error", "-y", "-ss", "1",
                            "-i", str(mp4), "-frames:v", "1", str(png)], check=True)
            im = Image.open(png).convert("RGB")
            return im.getpixel((im.width // 2, im.height - 6))

        check("源片底部是品红（字幕带）", bottom_px(src), (254, 0, 253))

        keep = C.normalize_clip(src, (tdp / "keep.mp4").resolve(), spec, 10.0)
        check("不裁 → 字幕带还在（对照组）", bottom_px(tdp / "keep.mp4"), (254, 0, 253))

        got = C.normalize_clip(src, (tdp / "cut.mp4").resolve(), spec, 10.0, crop_bottom=0.20)
        px = bottom_px(tdp / "cut.mp4")
        check_true("裁下 20% → 底部那条字幕带没了（变成上面的绿色）",
                   px[1] > 100 and px[0] < 100 and px[2] < 100, f"→ {px}")
        check("裁完仍是目标画幅（不出黑边/不变形）",
              (got["width"], got["height"]), (int(spec["width"]), int(spec["height"])))
        check("裁完时长不变", round(float(got["duration"]), 1), 3.0)
        check_true("记录里存了裁切比例（举证要说清裁过什么）",
                   got["crop_bottom"] == 0.2 and got["crop_top"] == 0.0, f"→ {got}")
        check_true("记录里存了取源片段的时间码",
                   got["src_in"] == 0.0 and got["src_out"] == 3.0, f"→ {got}")

        got2 = C.normalize_clip(src, (tdp / "cut2.mp4").resolve(), spec, 10.0,
                                src_in=1.0, src_out=2.5, crop_top=0.05, crop_bottom=0.20)
        check("按时间码取一段 + 上下都裁",
              (got2["src_in"], got2["src_out"], round(float(got2["duration"]), 1)), (1.0, 2.5, 1.5))
        try:
            C.normalize_clip(src, (tdp / "bad.mp4").resolve(), spec, 10.0,
                             crop_top=0.4, crop_bottom=0.4)
            check("上下裁太多要拦下", False, True)
        except Exception as exc:  # noqa: BLE001
            check_true("上下裁太多（0.8）→ 拦下不乱出片", "太多" in str(exc), f"→ {exc}")


def test_pyflakes_gate() -> None:
    """静态检查：拦住「函数体里引用了没定义的名字」这类只在真跑时才崩的 bug。

    为什么加这道：`agent._prepare_story` 里写了 `tts_mod.TTS(...)` 但模块顶部没 import ——
    冒烟测试用桩 TTS，永远走不到那行，直到**第一次真跑阶段 2** 才 `NameError` 崩掉。
    这类错 pyflakes 一秒就能看出来（实测：`undefined name 'tts_mod'`）。
    只拦「未定义名 / 重复定义」；未用 import 那些是无害噪声，不拦。
    """
    print("\n[静态检查 pyflakes：未定义名/重复定义]")
    import subprocess
    import sys as _sys
    from pathlib import Path as _P

    root = _P(__file__).resolve().parents[1]
    targets = sorted(str(p) for p in (root / "src" / "hsg").glob("*.py"))
    targets += sorted(str(p) for p in (root / "scripts").glob("*.py"))
    try:
        proc = subprocess.run([_sys.executable, "-m", "pyflakes", *targets],
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", cwd=str(root))
    except FileNotFoundError:
        check("没装 pyflakes（pip install pyflakes）", False, "跳过")
        return
    out = (proc.stdout or "") + (proc.stderr or "")
    if "No module named" in out:
        check("没装 pyflakes（pip install pyflakes）", False, "跳过")
        return
    bad = [ln for ln in out.splitlines()
           if "undefined name" in ln or "redefinition" in ln]
    noise = len([ln for ln in out.splitlines() if ln.strip()]) - len(bad)
    check_true("没有「引用了没定义的名字」（真跑才崩的那类）", not bad,
               f"→ {bad[:3]}")
    check_true("pyflakes 能跑起来（门槛有效）", proc.returncode in (0, 1),
               f"rc={proc.returncode}")
    if noise:
        print(f"  （另有 {noise} 条无害提示：未用 import / f-string 没占位符，不管）")


def test_intro_and_cover_style() -> None:
    """开场白（念出 L1 系列 + L2 片名）+ 封面橙色风格（按像素验色调）。"""
    print("\n[开场白 pipeline.intro_speech / 封面橙色 media.build_cover]")
    import tempfile
    from pathlib import Path as _P

    from PIL import Image

    from hsg import media, pipeline
    from hsg.config import load_config
    from hsg.models import Chapter, Scene, Story

    cfg = load_config()

    def _st(**kw):
        base = {"topic": "霍光：一个臣子凭什么能换掉皇帝？",
                "title": "霍光废帝：一个臣子凭什么能换掉皇帝？",
                "hook": "公元前74年夏天，长安未央宫里。", "series": "古代十大权臣",
                "series_ep": 1}
        base.update(kw)
        return Story(**base)

    txt = pipeline.intro_speech(_st(), cfg, "历史小故事")
    check_true("开场白念出系列名（L1）", "《古代十大权臣》" in txt, f"→ {txt[:60]}")
    check_true("开场白念出期号", "第一期" in txt, f"→ {txt[:60]}")
    check_true("开场白念出片名（L2）", "霍光废帝" in txt, f"→ {txt[:60]}")
    check_true("开场白接上开篇钩子（不是只有一句报幕）",
               txt.endswith("长安未央宫里。"), f"→ {txt[-24:]}")
    check_true("标题以问号结尾时不出现「？。」双标点", "？。" not in txt, f"→ {txt[:70]}")
    check_true("非系列单集保持原句式（不硬塞「系列」二字）",
               "系列" not in pipeline.intro_speech(_st(series="", series_ep=0),
                                                  cfg, "历史小故事")
               and "本期为您讲述" in pipeline.intro_speech(_st(series="", series_ep=0),
                                                           cfg, "历史小故事"))
    # ★ 真实路径：stage 2 是用 storyio 从 metadata 重建 story 的
    #  （上一版漏传 series/series_ep，导致开场白和画面系列标签在成片里都是缺的）
    import json as _json
    with tempfile.TemporaryDirectory() as _td:
        mp = _P(_td) / "20260916_plan_x_metadata.json"
        mp.write_text(_json.dumps({
            "topic": "霍光：一个臣子凭什么能换掉皇帝？", "title": "霍光废帝：一个臣子凭什么能换掉皇帝？",
            "hook": "公元前74年夏天。", "series": "古代十大权臣", "series_ep": 1,
            "period": "西汉", "period_start": -100, "period_end": 0, "tts_spec": {},
            "chapters": [{"index": 1, "heading": "遗诏辅政", "summary": "s",
                          "scenes": [{"index": 1, "text": "旁白", "caption": "图注"}]}]}),
            encoding="utf-8")
        import logging as _logging
        from hsg import storyio
        st_real, _raw = storyio.load_story(mp, cfg, _P(_td) / "audio",
                                           _logging.getLogger("test.storyio"))
        check("metadata → story 带上系列名（stage 2 真实路径）", st_real.series, "古代十大权臣")
        check("metadata → story 带上集号", st_real.series_ep, 1)
        t_real = pipeline.intro_speech(st_real, cfg, "历史小故事")
        check_true("真实路径的开场白也报系列与期号",
                   "古代十大权臣" in t_real and "第一期" in t_real, f"→ {t_real[:52]}")
        check_true("真实路径的画面系列标签也带上",
                   "古代十大权臣" in pipeline.kicker_text("历史小故事", st_real,
                                                          st_real.chapters[0]),
                   pipeline.kicker_text("历史小故事", st_real, st_real.chapters[0]))

    t2 = pipeline.intro_speech(_st(series_ep=2, title="曹操：他凭什么挟天子？"), cfg, "历史小故事")
    check_true("第 2 期不再说「从这一期开始」（改说「接着讲」）",
               "接着讲" in t2 and "从这一期开始" not in t2, f"→ {t2[:46]}")
    check_true("第 2 期也报出期号与片名", "第二期" in t2 and "曹操" in t2, f"→ {t2[:46]}")
    check("期号念中文（12 → 十二）", pipeline.num_cn(12), "十二")
    check("10 以内念中文", pipeline.num_cn(7), "七")
    check("问号结尾不加句号", pipeline.join_sentences("他凭什么？", "接着讲"),
          "他凭什么？接着讲")
    check("普通句子之间补句号", pipeline.join_sentences("甲", "乙"), "甲。乙")

    # ---- 封面橙色：按像素验色调（R 明显大于 B，整体偏暖）
    with tempfile.TemporaryDirectory() as td:
        nd = _P(td)
        title = "霍光废帝：一个臣子凭什么能换掉皇帝？"
        cover = media.build_cover(nd / "c.jpg", (1920, 1080), cfg, title=title,
                                  kicker="历史小故事 · 古代十大权臣 第1集",
                                  subtitle="公元前1世纪中叶", foot="一盏茶的时间，听一段旧事")
        check_true("封面出图", cover.exists() and cover.stat().st_size > 5000, str(cover))
        with Image.open(cover) as im:
            px = im.convert("RGB").resize((60, 34))
            data = list(px.getdata())
            r = sum(p[0] for p in data) / len(data)
            g = sum(p[1] for p in data) / len(data)
            b = sum(p[2] for p in data) / len(data)
        check_true(f"封面整体偏暖（R{r:.0f} > G{g:.0f} > B{b:.0f}）", r > g > b,
                   f"→ R{r:.0f} G{g:.0f} B{b:.0f}")
        check_true("橙色够明显（R 至少比 B 高 40）", r - b > 40,
                   f"→ R-B={r - b:.0f}")
        # 关掉色调统一 → 应该回到冷色底（证明这个开关真的起作用，不是摆设）
        cfg2 = load_config()
        cfg2["video"]["cover_tint_alpha"] = 0.0
        cfg2["video"]["cover_base"] = "#101820"
        cover2 = media.build_cover(nd / "c2.jpg", (1920, 1080), cfg2, title=title)
        with Image.open(cover2) as im:
            d2 = list(im.convert("RGB").resize((60, 34)).getdata())
            r2 = sum(p[0] for p in d2) / len(d2)
            b2 = sum(p[2] for p in d2) / len(d2)
        check_true("关掉橙色（tint_alpha=0 + 冷色底）→ 不再偏暖", r2 <= b2 + 10,
                   f"→ R{r2:.0f} B{b2:.0f}")
        # 真实封面底下是**配图**（常偏冷），不是渐变 —— 实测渐变测得出橙色、真实图差一口气，
        # 所以再拿一张冷色合成图走一遍，判据才跟生产对齐。
        cold = nd / "cold.jpg"
        Image.new("RGB", (900, 675), (188, 205, 228)).save(cold)
        cov3 = media.build_cover(nd / "c3.jpg", (1920, 1080), cfg, title=title,
                                 kicker="历史小故事 · 古代十大权臣 第1期",
                                 subtitle="公元前1世纪中叶", foot="一盏茶的时间，听一段旧事",
                                 image_path=cold)
        d3 = list(Image.open(cov3).convert("RGB").resize((60, 34)).get_flattened_data())
        r3 = sum(p[0] for p in d3) / len(d3)
        g3 = sum(p[1] for p in d3) / len(d3)
        b3 = sum(p[2] for p in d3) / len(d3)
        # 判据定在 30：冷色底实测 35、真实那一期封面 42 —— 基线是「冷底图也能压成橙调」
        check_true(f"冷色配图当底也照样偏橙（R{r3:.0f} G{g3:.0f} B{b3:.0f}）",
                   r3 > g3 > b3 and r3 - b3 >= 30, f"→ R-B={r3 - b3:.0f}")


def test_comic() -> None:
    """四格漫画路线：扩写 / 拼提示词 / 切格 / 一格格上屏 / blurpad 不切头切脚。"""
    print("\n[四格漫画 comic + shotvideo.encode_comic_shot + media fit]")
    import subprocess
    import tempfile
    from pathlib import Path as _P

    from PIL import Image, ImageDraw

    from hsg import comic as comic_mod
    from hsg import media, shotvideo
    from hsg.config import load_config

    cfg = load_config()

    # ---- ① 拼提示词：四格指令 + 画风 + 负面清单
    ex = {"panels": ["第1格：张三跪在廷上。", "第2格：李四展开帛画。",
                     "第3格：群臣侧目。", "第4格：窗外天光初亮。"],
          "continuity": "张三：四十岁，进贤冠，绛色朝服", "style": "连环画风格"}
    p = comic_mod.build_prompt(ex, cfg)
    check_true("提示词写明是四格连环画与格子排布", "四格连环画" in p and "2 行 × 2 列" in p, p[:40])
    check_true("四格按顺序都在提示词里",
               all(x in p for x in ("第1格", "第2格", "第3格", "第4格")), p[:60])
    check_true("带上人物与场景统一设定（跨格一致靠它）", "进贤冠" in p, "—")
    check_true("负面清单点名禁掉对白气泡与文字",
               "对白气泡" in p and "水印" in p, "—")
    check_true("不超接口上限 1500 字符", len(p) <= 1500, f"{len(p)} 字符")
    check_true("不出现连续句号", "。。" not in p, "—")

    # ---- ② 扩写兜底（LLM 不可用时不许断流程）
    mech = comic_mod.mechanical_panels("第一句。第二句。第三句。第四句。第五句。")
    check("机械四拍永远是 4 格", len(mech), 4)
    check_true("机械四拍把五句并成四格（顺序不乱）",
               mech[0].startswith("第1格") and "第一句" in mech[0], mech[0][:24])
    short = comic_mod.mechanical_panels("只有一句。")
    check_true("旁白太短时补空格而不是漏格", len(short) == 4 and "只有一句" in short[0],
               " / ".join(x[:10] for x in short))
    ex2 = comic_mod.expand({"scene": 1, "narration": "甲。乙。丙。丁。"},
                           type("S", (), {"period": "西汉"})(), cfg, None)
    check_true("没给 LLM 时走机械四拍且标记未扩写",
               ex2["expanded"] is False and len(ex2["panels"]) == 4, str(ex2["expanded"]))

    # ---- ③ 切格：四格互不重叠、面积均等、读序正确
    with tempfile.TemporaryDirectory() as td:
        nd = _P(td)
        cols = [(220, 30, 30), (30, 200, 30), (30, 60, 220), (240, 210, 40)]
        im = Image.new("RGB", (400, 400))
        d = ImageDraw.Draw(im)
        for j, col in enumerate(cols):
            r, c = divmod(j, 2)
            d.rectangle([c * 200, r * 200, (c + 1) * 200 - 1, (r + 1) * 200 - 1], fill=col)
        sheet = nd / "sheet.jpg"
        im.save(sheet, quality=98)
        panels = comic_mod.split_panels(sheet, "2x2")
        check("切成 4 格", len(panels), 4)
        check_true("四格尺寸均等", len({x.size for x in panels}) == 1, str(panels[0].size))
        got = [x.resize((1, 1)).getpixel((0, 0)) for x in panels]
        near = all(sum(abs(got[j][i] - cols[j][i]) for i in range(3)) < 90 for j in range(4))
        check_true("按读序切（左上→右上→左下→右下）", near, str(got))

        # ---- ④ 一格一格上屏：四个时间点各对应一格（合成四色卡，判据决定性）
        work = nd / "seg"
        slide = work / "slides"
        slide.mkdir(parents=True)
        out = shotvideo.encode_comic_shot(work, sheet, "c.mp4", (1280, 720), 8.0, cfg,
                                          slide_root=slide, stem="t", motion_mode=0)
        check_true("出段成功", out.exists() and out.stat().st_size > 5000, out.name)
        import json as _json
        dur = float(_json.loads(subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json",
             str(out)], capture_output=True, text=True).stdout)["format"]["duration"])
        check_true("段时长 = 8 秒（四格各 2 秒）", abs(dur - 8.0) < 0.3, f"{dur:.2f}s")
        refs = [list(x.resize((10, 10)).get_flattened_data()) for x in panels]
        hits = 0
        for j in range(4):
            t = (j + 0.5) * 8.0 / 4
            fr = nd / f"f{j}.jpg"
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", f"{t:.2f}",
                            "-i", str(out), "-frames:v", "1", "-q:v", "1", "-y", str(fr)],
                           check=True)
            img = Image.open(fr).convert("RGB")
            w, h = img.size
            sig = list(img.crop((int(w * .30), int(h * .34), int(w * .70),
                                 int(h * .58))).resize((10, 10)).get_flattened_data())
            def dist(a, b):
                return sum((x[0] - y[0]) ** 2 + (x[1] - y[1]) ** 2 + (x[2] - y[2]) ** 2
                           for x, y in zip(a, b)) ** 0.5
            ds = [dist(sig, r) for r in refs]
            hits += (ds.index(min(ds)) == j)
        check_true("四个时间点分别落在第 1/2/3/4 格上（真的在一格格推进）", hits == 4,
                   f"{hits}/4 命中")

        # ---- ⑤ blurpad：方形漫画格进 16:9 画布不许切头切脚
        # 做法：格子上沿画一条绿带、下沿画一条品红带 —— 带子还在就说明整格都在
        sq = Image.new("RGB", (400, 400), (110, 110, 110))
        d2 = ImageDraw.Draw(sq)
        d2.rectangle([0, 0, 399, 46], fill=(0, 200, 0))
        d2.rectangle([0, 354, 399, 399], fill=(250, 0, 250))
        band = nd / "band.jpg"
        sq.save(band, quality=98)

        def strips(bgpath):
            img = Image.open(bgpath).convert("RGB")
            w, h = img.size

            def hit(rgb, y0, y1, tol=45):
                xs = range(int(w * 0.40), int(w * 0.60), 6)
                ys = [y for y in range(int(h * y0), int(h * y1), 2)
                      if sum(1 for x in xs
                             if all(abs(img.getpixel((x, y))[i] - rgb[i]) < tol
                                    for i in range(3))) >= 0.6 * len(list(xs))]
                return bool(ys)

            return hit((0, 200, 0), 0.0, 0.20), hit((250, 0, 250), 0.80, 1.0)

        # sheet 模式：整张一起展示 —— 随便哪一帧都应该同时看得到四个格子
        cfg_sheet = load_config()
        cfg_sheet.comic["mode"] = "sheet"
        out_sheet = shotvideo.encode_comic_shot(work, sheet, "s.mp4", (1280, 720), 6.0, cfg_sheet,
                                                slide_root=slide, stem="s", motion_mode=0)
        fr2 = nd / "sheet_f.jpg"
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", "3.0",
                        "-i", str(out_sheet), "-frames:v", "1", "-q:v", "1", "-y", str(fr2)],
                       check=True)
        img2 = Image.open(fr2).convert("RGB")
        w2, h2 = img2.size
        quads = [(0.35, 0.36), (0.65, 0.36), (0.35, 0.62), (0.65, 0.62)]
        seen = []
        for fx, fy in quads:
            px = img2.getpixel((int(w2 * fx), int(h2 * fy)))
            seen.append(min(sum(abs(px[i] - c[i]) for i in range(3)) for c in cols) < 150)
        check_true("mode=sheet：一帧里四格同时可见（不再一格格切）",
                   sum(seen) >= 3, f"四角命中 {sum(seen)}/4")

        for fit, want in (("blurpad", True), ("cover", False)):
            media.build_layers(nd / f"b_{fit}_bg.jpg", nd / f"b_{fit}_fg.png", (1920, 1080),
                               cfg, image_path=band, fit=fit, darken=0.0)
            g, m = strips(nd / f"b_{fit}_bg.jpg")
            check_true(f"fit={fit}：整格可见 = {want}", (g and m) is want,
                       f"上沿 {g} 下沿 {m}")


def test_image_reuse() -> None:
    """配图复用：提示词没变就不重新生成（重渲不再白烧 18 张）。"""
    print("\n[配图复用 images.generate_scene_image]")
    import json
    import tempfile
    from pathlib import Path as _P

    from hsg import images
    from hsg.config import load_config

    cfg = load_config()
    with tempfile.TemporaryDirectory() as td:
        nd = _P(td)
        (nd / "scene_001.jpg").write_bytes(b"\xff\xd8\xff" + b"0" * 6000)
        from hsg.images import build_generate_prompt
        good = build_generate_prompt("汉代未央宫前殿", cfg, "scene")
        (nd / "_sources.json").write_text(json.dumps({
            "entries": [{"scene": 1, "result": "scene_001.jpg",
                         "attempts": [{"generator": "image-01", "prompt": good}]}]}),
            encoding="utf-8")
        check("能反查到上次用的提示词", images._recorded_prompt(nd, 1), good)
        pth, _label, _src, rec = images.generate_scene_image(1, "汉代未央宫前殿", cfg, nd, "scene")
        check_true("提示词没变 → 复用已有图（不调 API）",
                   rec.get("reused") is True and pth and pth.name == "scene_001.jpg",
                   f"reused={rec.get('reused')} err={rec.get('error')}")
        # 复用的记录必须带版权标注，且要能被 pipeline 那套取值口径拿到
        # （pipeline 取的是 attempts 里带 "picked" 的那条的 license）
        check_true("复用记录带 picked（pipeline 靠它取版权）", bool(rec.get("picked")),
                   str(rec.get("picked")))
        check("复用记录的版权标注不是空的", bool(rec.get("license")), True)
        # 提示词变了（换了画风）→ 不许复用，会走生成（这里没 key，退化为失败而不复用）
        (nd / "_sources.json").write_text(json.dumps({
            "entries": [{"scene": 1, "attempts": [{"prompt": "完全不同的提示词"}]}]}),
            encoding="utf-8")
        pth2, _l2, _s2, rec2 = images.generate_scene_image(1, "汉代未央宫前殿", cfg, nd, "scene")
        check_true("提示词变了 → 不复用（走生成路径）", rec2.get("reused") is None,
                   f"reused={rec2.get('reused')}")
        # 关掉开关 → 不复用
        cfg2 = load_config()
        cfg2.images["reuse_existing"] = False
        pth3, _l3, _s3, rec3 = images.generate_scene_image(1, "汉代未央宫前殿", cfg2, nd, "scene")
        check_true("reuse_existing=false → 不复用", rec3.get("reused") is None,
                   f"reused={rec3.get('reused')}")


def test_render_kicker_shape() -> None:
    """渲染路径必须跟 kicker_text 共用口径（别自己拼 kicker）。

    踩过：EDL 渲染路径自己拼 f"{channel} · 第N章 {heading}"，绕过了 kicker_text，
    于是系列名与期号永远上不了屏幕，而测试只钉 kicker_text，验不到真实路径。
    """
    print("\n[渲染路径 kicker 口径]")
    import re as _re
    from pathlib import Path as _P
    src = (_P(__file__).resolve().parents[1] / "src" / "hsg" / "pipeline.py").read_text(
        encoding="utf-8")
    bad = _re.findall(r'kicker = f"\{[^\n]*第\{', src)
    check_true("渲染路径没有自己拼 kicker（必须走 kicker_text）", not bad, f"发现 {len(bad)} 处")


def test_bright_amber_cover_and_no_clips() -> None:
    """封面=明亮橙黄（用户 2026-09-17 指定）；clips.enabled=false → 成片里一个切片都没有。"""
    print("\n[明亮橙黄封面 + 不用切片的 EDL]")
    import tempfile
    from collections import Counter
    from pathlib import Path as _P

    from PIL import Image

    from hsg import edl as edl_mod
    from hsg import media
    from hsg.config import load_config
    from hsg.models import Chapter, Scene, Story

    cfg = load_config()

    # ---- 封面：拿一张**偏暗的**合成图当底（真实配图就是暗的），看橙色够不够亮
    with tempfile.TemporaryDirectory() as td:
        nd = _P(td)
        dark = nd / "dark.jpg"
        Image.new("RGB", (900, 675), (58, 50, 46)).save(dark, quality=95)
        cv = media.build_cover(nd / "cv.jpg", (1920, 1080), cfg,
                               title="霍光废帝：一个臣子凭什么能换掉皇帝？",
                               kicker="历史小故事 · 古代十大权臣 第1集",
                               subtitle="西汉昭帝、宣帝年间，公元前1世纪中叶",
                               foot="一盏茶的时间，听一段旧事", image_path=dark)
        px = list(Image.open(cv).convert("RGB").resize((60, 34)).get_flattened_data())
        r = sum(p[0] for p in px) / len(px)
        g = sum(p[1] for p in px) / len(px)
        b = sum(p[2] for p in px) / len(px)
        luma = 0.299 * r + 0.587 * g + 0.114 * b
        check_true(f"暗底图也压成橙黄（R{r:.0f} G{g:.0f} B{b:.0f}）", r > g > b,
                   f"→ R{r:.0f} G{g:.0f} B{b:.0f}")
        check_true(f"橙黄够暖（R-B={r - b:.0f}，要 >60）", r - b > 60, f"→ {r - b:.0f}")
        check_true(f"够明亮（亮度 {luma:.0f}，要 ≥85）", luma >= 85, f"→ {luma:.0f}")
        check_true(f"偏黄不偏红（R-G={r - g:.0f}，要 ≥20）", r - g >= 20, f"→ {r - g:.0f}")

    # ---- EDL：clips.enabled=false 时不许出切片镜头
    def _mk_story():
        return Story(topic="t", title="测试集", series="古代十大权臣", series_ep=1,
                     chapters=[Chapter(index=1, heading="第一章", scenes=[
                         Scene(index=1, text="旁白一", chapter_index=1, duration=12.0),
                         Scene(index=2, text="旁白二", chapter_index=1, duration=12.0)])])
    # ⚠️ 槽位名必须是约定的 s01_sh1（候选是按 scene+shot 反推槽位名匹配的），
    # 所以为了"漫画只在有图时才用"这条用例，必须把 comic.dir 指到临时目录 ——
    # 磁盘上真有 data/comics/s01_sh1.jpg，不隔离的话这条用例会测到别的期去。
    needs = {"slots": [
        {"scene": 1, "slot": "s01_sh1", "chapter": 1, "dur": 6.0, "callout": "托孤",
         "narration": "旁白一", "fallback": "generate"},
        {"scene": 1, "slot": "s01_sh2", "chapter": 1, "dur": 6.0, "narration": "旁白一"},
        {"scene": 2, "slot": "s02_sh1", "chapter": 1, "dur": 6.0, "narration": "旁白二"},
        {"scene": 2, "slot": "s02_sh2", "chapter": 1, "dur": 6.0, "narration": "旁白二"}]}
    index = {"version": 1, "clips": [{"id": "hg_tuogu", "file": "norm/hg_tuogu.mp4", "dur": 7.0,
                                      "title": "汉武大帝", "year": "2005", "people": ["霍光"],
                                      "era": "西汉", "slots": ["s01_sh1"], "desc": "托孤"}]}

    comic_dir = _P(td) / "comics"      # 隔离：这里没有漫画图
    cfg_off = load_config()
    cfg_off.comic["dir"] = str(comic_dir)
    cfg_off.clips["enabled"] = False
    e_off = edl_mod.build_edl(_mk_story(), needs, index, cfg_off, None)
    k_off = Counter(s["kind"] for p in e_off["scenes"] for s in p["shots"])
    check("clips.enabled=false：一个切片/复用镜头都没有",
          k_off.get("clip", 0) + k_off.get("reuse", 0), 0)
    check("clips.enabled=false：切片占比为 0", edl_mod.clip_share(e_off), 0.0)

    cfg_on = load_config()
    cfg_on.comic["dir"] = str(comic_dir)
    cfg_on.clips["enabled"] = True
    e_on = edl_mod.build_edl(_mk_story(), needs, index, cfg_on, None)
    k_on = Counter(s["kind"] for p in e_on["scenes"] for s in p["shots"])
    check_true("对照：打开切片时那条绑定素材会被用上",
               k_on.get("clip", 0) + k_on.get("reuse", 0) >= 1, str(dict(k_on)))

    # ---- 漫画只在**出了图**时才用（没图的槽位照旧生成图）
    cfg_c = load_config()
    cfg_c.comic["dir"] = str(comic_dir)
    cfg_c.comic["enabled"] = True
    e_c = edl_mod.build_edl(_mk_story(), needs, index, cfg_c, None)
    k_c = Counter(s["kind"] for p in e_c["scenes"] for s in p["shots"])
    check("没出过漫画图的槽位不会写成 comic（免得渲染层空等）", k_c.get("comic", 0), 0)


def test_audit_triage() -> None:
    """审校结果过滤：它自己判了「不报/无误」的条目不许进「待人工核对」清单。"""
    print("\n[审校噪声过滤 verify.triage]")
    from hsg import verify
    items = [
        {"scene": 1, "kind": "fail", "detail": "真问题：年份对不上｜建议：改成前66年"},
        {"scene": 2, "kind": "fail", "detail": "……此处表述正确。此条不报。｜建议："},
        {"scene": 3, "kind": "fail", "detail": "……此条无误。｜建议："},
        {"scene": 4, "kind": "fail", "detail": "……是对的，不必报。｜建议："},
        {"scene": 6, "kind": "fail", "detail": "……正确。｜建议：无需修改。"},
        {"scene": 5, "kind": "fail", "detail": "另一处真问题｜建议：换个说法"},
    ]
    out = verify.triage(items)
    check("只留真报项", [i["scene"] for i in out], [1, 5])
    check("全是噪声时返回空（三种标记都要认）", verify.triage(items[1:5]), [])
    check("空输入不炸", verify.triage(None), [])


def test_text_provider_switch() -> None:
    """写稿/校验模型分离（2026-09-17 用户指定）：MiniMax 写稿要显式确认，校验固定 DeepSeek。"""
    print("\n[写稿/校验 provider 分离 config.assert_text_provider / verify_provider]")
    from hsg import config as config_mod
    from hsg.config import load_config
    from hsg.llm import LLM

    cfg = load_config()
    cfg.llm["provider"] = "deepseek"
    cfg.llm["allow_nondeepseek_text"] = False
    check("默认写稿走 deepseek", config_mod.assert_text_provider(cfg), "deepseek")
    cfg.verify["provider"] = ""
    check("校验不填时默认 deepseek", config_mod.verify_provider(cfg), "deepseek")

    cfg.llm["provider"] = "minimax"
    try:
        config_mod.assert_text_provider(cfg)
        blocked = False
    except RuntimeError as exc:
        blocked = "allow_nondeepseek_text" in str(exc)
    check_true("切 MiniMax 但不显式确认 → 拦下并告诉怎么开", blocked, "没拦住")
    cfg.llm["allow_nondeepseek_text"] = True
    check("显式确认后放行", config_mod.assert_text_provider(cfg), "minimax")
    check("写稿切了 MiniMax，校验仍是 deepseek（两模型互挑错）",
          config_mod.verify_provider(cfg), "deepseek")

    cfg.llm["provider"] = "aliyun"
    try:
        config_mod.assert_text_provider(cfg)
        bad = False
    except RuntimeError:
        bad = True
    check_true("白名单外的 provider 直接拒绝", bad, "没拒绝")

    cfg.llm["provider"] = "minimax"
    check("LLM(provider=...) 能覆盖写稿模型",
          LLM(cfg, provider="deepseek").provider, "deepseek")
    check("LLM() 默认跟随配置", LLM(cfg).provider, "minimax")


def test_generate_only_images() -> None:
    """generate_only：只用生成图，不退回图库（用户 2026-09-17 要求删掉网上找图）。"""
    print("\n[只用生成图 images.fetch_for_scene generate_only]")
    import tempfile
    from pathlib import Path as _P

    from hsg import images
    from hsg.config import load_config

    cfg = load_config()
    check("配置里已关掉所有图库源", list(cfg.images.get("providers") or []), [])
    check_true("generate_only 开着", bool(cfg.images.get("generate_only")), "关着")
    with tempfile.TemporaryDirectory() as td:
        nd = _P(td)
        # generate_only 且取不到 key → 生成失败 → 应当**直接返回无图**，不许去找图库
        cfg2 = load_config()
        cfg2.images["generate_only"] = True
        cfg2.images["generate_retries"] = 2
        cfg2.images["generate"] = False   # 模拟"生成失败"，避免误打真实 API（会花钱、且慢）
        p, label, src, attempts = images.fetch_for_scene(
            1, ["某个很具体的检索词"], cfg2, nd, [], queries_en=["very specific query"],
            kind=images.KIND_SCENE)
        check_true("生成失败时不退回图库（没有库检索记录）",
                   all(str(a.get("round", "")).startswith("generate") for a in attempts),
                   str([a.get("round") for a in attempts])[:60])
        check_true("重试后仍无图 → 交给调用方用渐变底图兜底", p is None, str(p))


def test_script_notes_roundtrip() -> None:
    """「待人工核对」清单不能丢：metadata 落盘的字段叫 unresolved，读回来必须是 notes。

    踩过的坑（2026-09-17，第 2 集）：write_metadata 把 story.notes 写进 metadata 的
    `unresolved`，而 storyio.load_story 只读 `notes` —— 于是「读进来再写出去」这一步
    会把整份清单洗成空（阶段 2 出的成片 metadata 也跟着空，发布前最该看的那份反而没了）。
    """
    print("\n[脚本留档 · unresolved ↔ notes 往返]")
    import json as _json
    import logging
    import tempfile

    from hsg import pipeline, storyio

    cfg = load_config()
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "m.json"
        s = Scene(index=1, text="测试口播一句。", image_query="检索词", chapter_index=1)
        s.duration = 5.0
        story = Story(topic="题材", title="标题",
                      chapters=[Chapter(index=1, heading="一章", scenes=[s])])
        story.notes = ["分镜 1：某某说法查无出处", "（人工复核）本轮改了 3 处"]
        pipeline.write_metadata(story, cfg, p, [], {})
        data = _json.loads(p.read_text(encoding="utf-8"))
        check("落盘字段名是 unresolved", len(data.get("unresolved") or []), 2)
        st2, _ = storyio.load_story(p, cfg, Path(td) / "audio", logging.getLogger("t"))
        check("读回来落到 story.notes", len(st2.notes), 2)
        p2 = Path(td) / "m2.json"
        pipeline.write_metadata(st2, cfg, p2, [], {})
        check("再写一次不丢（往返稳定）",
              len(_json.loads(p2.read_text(encoding="utf-8")).get("unresolved") or []), 2)


def test_stage2_record() -> None:
    """阶段 2 出片后必须留档：成片 metadata + 生成记录（系列进度靠它算）。

    为什么值得钉住：阶段 2 是出成片的**唯一**路径，而它在 2026-09-17 之前
    从不写生成记录 —— 第 1 集出过两版成片，`run.bat series` 仍显示 0/10，
    「下一集」一直挑回第 1 集（系列片会一直在原地打转）。
    """
    print("\n[阶段 2 留档 · 成片 metadata + 生成记录 + 系列进度]")
    import json as _json
    import tempfile
    import time as _time

    from hsg import agent as A, history as H, topics as T

    cfg = load_config()
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        out = base / "output"
        out.mkdir(parents=True, exist_ok=True)
        cfg["paths"]["data_dir"] = str(base / "data")
        cfg["paths"]["output_dir"] = str(out)

        s = Scene(index=1, text="曹操把天子迎到许都，诏书从这里发出去。",
                  image_query="汉代诏书简牍", chapter_index=1)
        s.duration = 8.5
        story = Story(topic="曹操：挟天子到底能令多少诸侯？", title="挟天子这面旗：曹操能调动谁",
                      series="古代十大权臣", series_ep=2, topic_type="政变与权力",
                      period="东汉末年 196 年", period_start=196, period_end=220,
                      chapters=[Chapter(index=1, heading="把天子握在手里", scenes=[s])])
        plan = out / "20260918_plan_挟天子这面旗：曹操能调动谁_metadata.json"
        plan.write_text(_json.dumps({"title": story.title, "series": story.series,
                                     "series_ep": 2, "topic": story.topic,
                                     "plan_only": True}, ensure_ascii=False),
                        encoding="utf-8")
        stem = A.pipeline.safe_filename(story.title)
        mp4 = out / f"20260918_{stem}_横屏.mp4"
        mp4.write_bytes(b"0" * 4096)

        meta_path, rec_path = A._record_finished(
            cfg, story, total=8.5, outs=[mp4], covers=["cover.jpg"],
            video_seconds={"landscape": 9.1}, started=_time.time() - 3.0,
            plan_meta=plan)
        check_true("成片 metadata 落盘", meta_path is not None and meta_path.exists())
        md = _json.loads(meta_path.read_text(encoding="utf-8")) if meta_path else {}
        check("成片 metadata 记为出片（不是 plan_only）", md.get("plan_only"), False)
        check("成片 metadata 带系列与集号",
              (md.get("series"), md.get("series_ep")), ("古代十大权臣", 2))
        check_true("成片 metadata 留了音色/语速（rerender 旧期要靠它）",
                   bool((md.get("tts_spec") or {}).get("voice_id")))
        check_true("成片 metadata 记了成片路径", mp4.name in " ".join(md.get("outputs") or []))
        check("生成记录已写入", len(H.load(cfg)), 1)
        got = H.load(cfg)[0]
        check("记录里带系列与集号", (got.get("series"), got.get("series_ep")),
              ("古代十大权臣", 2))
        check_true("记录指向成片", any(mp4.name in str(x) for x in got.get("outputs") or []))
        check_true("记录指向阶段 1 的稿子（不是空字符串）",
                   "__no_such__" not in str(got.get("script")), str(got.get("script")))

        # 重跑阶段 2（换开场白/补竖屏版）不该堆出同一期的多份副本
        A._record_finished(cfg, story, total=8.5, outs=[mp4], covers=[],
                           video_seconds={"portrait": 9.4}, started=_time.time() - 2.0,
                           plan_meta=plan)
        check("重跑同一期是更新而不是新增", len(H.load(cfg)), 1)
        check_true("第一次出片的时间被保留",
                   bool(H.load(cfg)[0].get("first_generated_at")))

        # 系列进度：这就是这一整段代码存在的理由
        pool = T.load_user_pool(T.user_pool_path(cfg))
        txt = T.series_progress(pool, H.load(cfg), "古代十大权臣")
        check_true("系列进度认到这一集（1/10）", "进度 1/10 集" in txt)
        # 注意：✓/· 标记在标题的**下一行**（第一行放标题与类型，第二行放状态与描述）
        rows_ = txt.splitlines()
        idx = [i for i, ln in enumerate(rows_) if "曹操：挟天子" in ln]
        ep2 = rows_[idx[0] + 1] if idx else ""
        check_true("第 2 集那行标成已出", "✓ 已出" in ep2, ep2)
        check_true("刚出的一集被判成「做过」",
                   H.is_used(cfg, story.topic) or H.is_used(cfg, story.title))
        nxt = T.next_episode(pool, "古代十大权臣", lambda x: H.is_used(cfg, x))
        check_true("下一集往前走到第 1 集（本期做过，不会再被挑中）",
                   nxt is not None and nxt.ep == 1, str(getattr(nxt, "ep", None)))

    # backfill 的例外：plan 产物 + 成片确实躺在 output 里 → 认回这一期
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        out = base / "output"
        out.mkdir(parents=True, exist_ok=True)
        cfg2 = load_config()
        cfg2["paths"]["data_dir"] = str(base / "data")
        cfg2["paths"]["output_dir"] = str(out)
        title = "某期已经出过片但没记进历史"
        stem = A.pipeline.safe_filename(title)
        (out / f"20260910_plan_{stem}_metadata.json").write_text(
            _json.dumps({"title": title, "topic": "某题材", "plan_only": True,
                         "chapters": [{"scenes": [{"text": "一二三四五六七八九十"}]}]},
                        ensure_ascii=False), encoding="utf-8")
        # ① 只有稿子、没有成片 → 不算一期
        check("plan 产物没成片 → 不补录", H.backfill_from_metadata(cfg2), 0)
        # ② 成片在（>1KB）→ 补录，并带上成片路径
        (out / f"20260910_{stem}_横屏.mp4").write_bytes(b"0" * 4096)
        check("成片确实存在 → 补录 1 条", H.backfill_from_metadata(cfg2), 1)
        rec = H.load(cfg2)[0]
        check_true("补录的记录指向成片",
                   any(stem in str(x) for x in rec.get("outputs") or []))
        check_true("补录的记录标了来源 plan metadata", bool(rec.get("plan_metadata")))


def main() -> int:
    test_sanitize()
    test_fix_line_punct()
    test_extract_years()
    test_audit_filter()
    test_rule_check_catches()
    test_motion()
    test_cues()
    test_dedupe()
    test_dedupe_llm_verdict()
    test_history_roundtrip()
    test_angle_mode()
    test_topic_levels()
    test_series()
    test_agent_plan_pick()
    test_sources()
    test_fact_audit()
    test_license_policy()
    test_culture_filter()
    test_era_match()
    test_final_check_gate()
    test_license_plumbing()
    test_client_and_tts_guards()
    test_renumber_and_feedback()
    test_tts_speed_plumbing()
    test_cover()
    test_intro_and_cover_style()
    test_comic()
    test_image_reuse()
    test_render_kicker_shape()
    test_bright_amber_cover_and_no_clips()
    test_audit_triage()
    test_text_provider_switch()
    test_generate_only_images()
    test_clip_index()
    test_clip_normalize()
    test_crop_subtitle_band()
    test_pyflakes_gate()
    test_needs()
    test_edl()
    test_edl_overlong_shot()
    test_frames()
    test_silence_and_audio_track()
    test_playlist_bgm()
    test_scene_kinds()
    test_scene_query_translation()
    test_generate_image_prompt()
    test_stage2_record()
    test_script_notes_roundtrip()
    print("\n" + "=" * 60)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    for f in FAIL:
        print(f"  ✗ {f}")
    print("=" * 60)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
