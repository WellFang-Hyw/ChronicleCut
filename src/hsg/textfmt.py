"""文本格式化与口播清洗。

两类问题在这里解决：

1) **数字/日期读错**：`"%m月%d日"` 会渲染成「09月10日」，
   TTS 很可能念成「零九月十日」。面向语音和画面的日期一律不补零。

2) **模型输出不干净**：markdown 星号、井号标题、`【】` 标记、
   括号注释、以及「维基百科」「百度百科」这类引用来源词，
   都会原样念出来。必须程序化清一遍（提示词约束一定会漏）。
"""

from __future__ import annotations

import re
from datetime import datetime

# 中文数字（用于年份口播，如 公元前221年）
_CN_DIGITS = "零一二三四五六七八九"


# ---------------------------------------------------------------- 日期
def md(d: datetime) -> str:
    """9月10日（不补零）。"""
    return f"{d.month}月{d.day}日"


def md_hm(d: datetime) -> str:
    """9月10日 5:00（不补零）。"""
    return f"{d.month}月{d.day}日 {d.hour}:{d.minute:02d}"


# ---------------------------------------------------------------- 清洗
_MD_PATTERNS = (
    (re.compile(r"\*\*(.+?)\*\*", re.S), r"\1"),
    (re.compile(r"\*(.+?)\*", re.S), r"\1"),
    (re.compile(r"__(.+?)__", re.S), r"\1"),
    (re.compile(r"`{1,3}(.+?)`{1,3}", re.S), r"\1"),
    (re.compile(r"^\s{0,3}#{1,6}\s*", re.M), ""),
    (re.compile(r"^\s{0,3}[-*·]\s+", re.M), ""),
    (re.compile(r"^\s*>{1,2}\s*", re.M), ""),
    (re.compile(r"^\s*-{3,}\s*$", re.M), ""),
    (re.compile(r"[【】\[\]]"), ""),
    (re.compile(r"^[\s　]*第[一二三四五六七八九十\d]+[章回节][:：、\s]*", re.M), ""),
)

# 引用来源词 —— 播报里不该出现（且属于版权/品牌敏感）
_SOURCE_WORDS = (
    "维基百科", "百度百科", "互动百科", "搜狗百科", "百科",
    "百度知道", "知乎", "公众号", "百家号", "视频号", "小红书",
    "据某网站", "据网站", "据网络资料", "网上资料",
)

# 零补零日期 / 时间
_RE_PAD_DATE = re.compile(r"(?<!\d)0(\d)(?=月)")
_RE_PAD_DAY = re.compile(r"(?<!\d)0(\d)(?=日)")

# 公元纪年：-221年 / 前221年 / BC221 → 公元前221年
_RE_BC = re.compile(r"(?:公元前\s*|前\s*|BC\s*)(\d{1,4})\s*年?", re.I)

# 年份区间 208-209年 / 208—209年 / 208~209年 → 208年到209年
_RE_YEAR_RANGE = re.compile(r"(?<!\d)(\d{2,4})\s*[-—–~～至]\s*(\d{2,4})\s*年")

# 阿拉伯数字 + 年（保留，TTS 读得对）；但 3 位以上带千分位要清掉
_RE_THOUSAND_SEP = re.compile(r"(\d),(\d{3})")

_RE_MULTI_SPACE = re.compile(r"[ \t\u00a0]+")
_RE_MULTI_NL = re.compile(r"\n{2,}")
# 括注里的英文/拼音（念出来很怪）—— 只清全英文的短括注
_RE_PAREN_EN = re.compile(r"[（(]\s*[A-Za-z][A-Za-z\s.\-']{1,40}\s*[）)]")
# 舞台提示：整块删掉（否则「停顿」会被念出来）
_RE_STAGE_WORDS = re.compile(
    r"[【\[（(][^】\]）)]{0,14}(?:停顿|配图|画面|音效|音乐|字幕|旁白|镜头|特效)[^】\]）)]{0,14}[】\]）)]"
)


def sanitize_narration(text: str) -> str:
    """口播稿清洗：去 markdown、去舞台提示、去引用来源、日期不补零。

    这是最后一道防线 —— 提示词里已经要求过，但实测模型仍会漏。
    """
    s = strip_stage_marks((text or "").strip())
    if not s:
        return ""

    for pat, rep in _MD_PATTERNS:
        s = pat.sub(rep, s)

    # 去掉「据维基百科记载」这种前缀词，而不是整句删掉
    for w in _SOURCE_WORDS:
        s = s.replace(f"据{w}记载", "据史料记载")
        s = s.replace(f"根据{w}记载", "据史料记载")
        s = s.replace(f"据{w}显示", "据史料记载")
        s = s.replace(w, "")

    s = _RE_THOUSAND_SEP.sub(r"\1\2", s)
    s = _RE_YEAR_RANGE.sub(r"\1年到\2年", s)
    s = _RE_BC.sub(lambda m: f"公元前{m.group(1)}年", s)
    s = _RE_PAD_DATE.sub(r"\1", s)
    s = _RE_PAD_DAY.sub(r"\1", s)
    s = _RE_PAREN_EN.sub("", s)

    s = s.replace("～", "到")
    s = _RE_MULTI_SPACE.sub(" ", s)
    s = _RE_MULTI_NL.sub("\n", s)
    # 清理清洗后可能出现的重复标点
    s = re.sub(r"，，+", "，", s)
    s = re.sub(r"。，", "。", s)
    s = re.sub(r"^[，。、；：\s]+", "", s)
    return s.strip()


def strip_stage_marks(text: str) -> str:
    """去掉「（停顿）」「【配图】」「[此处音效]」这类舞台提示。

    注意：整块删掉而不是只删括号 —— 只删括号的话「停顿」两个字会被念出来。
    """
    s = _RE_STAGE_WORDS.sub("", text or "")
    s = re.sub(r"^[\s　]*[-—]{2,}.*$", "", s, flags=re.M)
    return s.strip()


# ---------------------------------------------------------------- 年份 / 数字
def extract_years(text: str) -> list[int]:
    """抽取文中出现的年份。公元前为负数。

    这里只认「带年字」的数字，避免把兵力「八十万」、年龄「三十岁」当成年份。
    """
    years: list[int] = []
    # 公元前 xxx 年
    for m in re.finditer(r"公元前\s*(\d{1,4})\s*年", text or ""):
        years.append(-int(m.group(1)))
    # 公元后 xxx 年（排除紧跟在「公元前」后的）
    for m in re.finditer(r"(?<!公元前)(?<!前)(?<!\d)(\d{2,4})\s*年(?!前)", text or ""):
        val = int(m.group(1))
        if 1 <= val <= 2100:
            years.append(val)
    return years


def normalize_year(y: int) -> str:
    return f"公元前{abs(y)}年" if y < 0 else f"{y}年"


# ---------------------------------------------------------------- 人名
# 抽取「可能的专有名词」做一致性检查：2-4 个连续汉字且不是常见虚词
_NAME_STOP = set(
    "这个那个他们我们你们就是还是可以因为所以但是而且然后于是后来当时此前"
    "一个一年一直一起之后之前从此因此天下天下人于是然后如今现在后来"
)


def extract_names(text: str, limit: int = 40) -> list[str]:
    """粗抽 2-4 字的中文专名（用于跨段落写法一致性检查）。

    精确的人名识别需要模型，这里只要「同一个名字写法是否前后一致」，
    所以粗抽 + 词频统计就够了。
    """
    cands: list[str] = []
    for m in re.finditer(r"[\u4e00-\u9fa5]{2,4}", text or ""):
        w = m.group(0)
        if w in _NAME_STOP or len(w) < 2:
            continue
        cands.append(w)
    seen: dict[str, int] = {}
    for w in cands:
        seen[w] = seen.get(w, 0) + 1
    # 出现次数多、且不是纯虚词的优先
    return [w for w, _ in sorted(seen.items(), key=lambda kv: -kv[1])[:limit]]


def neutralize_extra(text: str, extra_forbidden: list[str]) -> str:
    """按配置额外抹掉一些词（如指定不要出现的站名）。"""
    s = text or ""
    for w in extra_forbidden or []:
        if w:
            s = s.replace(w, "")
    return s


# ---------------------------------------------------------------- 折行修正
_LEADING_PUNCT = "，。、；：！？）】》」』…·"


def fix_line_punct(lines: list[str]) -> list[str]:
    """把落在行首的标点挪回上一行行尾。

    字幕按字数硬折行时，很常见的是第二行以「，」开头 ——
    视觉上很难看（排版硬伤），而原文其实没有这个问题。
    """
    out: list[str] = []
    for ln in lines:
        if out and ln and ln[0] in _LEADING_PUNCT:
            out[-1] += ln[0]
            ln = ln[1:]
        if ln:
            out.append(ln)
    return out
