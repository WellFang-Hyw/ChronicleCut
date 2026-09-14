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
        check_true("模型分工被记录", "deepseek/" in loaded[0]["text_model"])
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
    check_true("small 模式选题来自小切口池", nxt in topics.TOPICS_ANGLE)
    check_true("小切口池与事件池不重叠",
               not (set(topics.TOPICS_ANGLE) & set(topics.TOPICS_EVENT)))

    cfg.story["angle_mode"] = "event"
    check("event 模式：切回事件式提示词", "来龙去脉" in outline.system_for(cfg), True)
    check("event 模式：schema 里没有 angle_question", "angle_question" in outline.schema_for(cfg), False)
    nxt = topics.pick(seed=5, mode="event")
    check_true("event 模式选题来自事件池", nxt in topics.TOPICS_EVENT)
    check("缺省（未配置）按 small 处理", outline.mode_of(load_config()), "small")

    # 小切口池的主题都应该是「切口：问题」的问句形式
    bad = [t for t in topics.TOPICS_ANGLE if "：" not in t and "?" not in t and "？" not in t]
    check_true("小切口题材都带冒号或问号（提问式）", not bad)


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
    check_true("封面画上了文字（亮像素数量合理）", 3000 < bright < 200000,
               f"bright={bright}")
    check_true("封面不是纯色空图（有明暗层次）", stat.stddev[0] > 20,
               f"stddev={stat.stddev[0]:.1f}")
    out.unlink(missing_ok=True)
    src.unlink(missing_ok=True)


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
    test_scene_query_translation()
    test_generate_image_prompt()
    print("\n" + "=" * 60)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    for f in FAIL:
        print(f"  ✗ {f}")
    print("=" * 60)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
