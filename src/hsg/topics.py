"""选题池（**两级**）。

为什么改成两级（用户 2026-09-15 的要求）：

  **L1 故事类型** —— 高层描述：这一类故事讲什么、从哪儿切进去。
  **L2 具体选题** —— 详细标题 + 与这个故事相关性最高的描述。

两级各解决一个具体问题：

  · L1 让「类型」成为一等概念，于是能：
      - 按类型挑题（`--type "行旅与驿传"`）；
      - 避开连着做同一类（`pick(recent_types=...)` 优先选近期没做过的类型）；
      - 让写稿一开始就知道自己在写哪一路故事（类型描述直接进大纲提示词）。
  · L2 的 `desc` **不是标题的复述**，而是「这一期到底讲什么、要回答什么」。
    它有三处用处：进大纲提示词当语境、进 metadata/脚本留档、进生成记录供查重。

⚠️ 写 `desc` 的规矩：只描述**这一期要讲的范围与要回答的问题**，不要在这里
   塞具体数字或结论 —— 那些要由写稿阶段的史实校验兜住，写在这里就等于
   把一个未验证的说法写进了选题池（池子是长期资产，错了会一直传下去）。

`angle_mode`（config.story.angle_mode）决定**切入方式**（small 小切口 / event 事件式），
`type` 是横跨两者的另一个维度：小切口按「生活面」分类型，事件式按「事件性质」分。

选题标准（小切口模式）：
  · 问题是具体的是日常的：吃、穿、住、行、钱、病、死、罪、税、路、时间
  · 能靠史料回答：有制度、有档案、有数字（价格/里程/天数/人数），不是空谈
  · 切口小但能带出大历史：一个人的遭遇能照见一个时代的运行方式
  · 有实物可配图：古画、文物、文书、器物、遗址、老照片
  · 避开近现代政治敏感题材
"""

from __future__ import annotations

import random
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("hsg.topics")
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# ---------------------------------------------------------------- L1 故事类型
STORY_TYPES: dict[str, str] = {
    "行旅与驿传": "人与物的长途移动：路怎么走、住哪儿、要多久、花多少钱。"
                  "从一段路程里看驿传、漕运、商旅与普通人的出行半径。",
    "生计与物价": "吃穿住用的日常开销：一天吃什么、夏天怎么消暑、下雨怎么排水、"
                  "夜里能不能出门。用生活细节还原当时的生活成本。",
    "钱粮与税役": "财政与赋役落到具体人身上的样子：交多少、什么时候交、交不上怎么办，"
                  "以及货币、盐铁这类专卖制度实际怎么运转。",
    "官府与吏治": "官场与公务的日常：俸禄够不够养家、朝会怎么站班、幕僚师爷怎么请，"
                  "以及地方官府之外的社会机构承担了什么。",
    "刑狱与流放": "罪与罚的执行过程：牢里吃什么、流放路上怎么活、"
                  "城破之后不同身份的人各自遭遇什么。",
    "疾病与丧葬": "病、死、卫生如何被当时的制度和家庭承担：请不请得起郎中、"
                  "瘟疫谁来管、一口棺材一块地要多少钱。",
    "战乱与灾变": "秩序崩坏时刻的普通人处境：被征的兵、随军的民夫、"
                  "灾年的赈济链条与百姓自救。",
    "婚育与教化": "婚嫁、读书、手艺人：聘礼嫁妆怎么压垮一个家庭、"
                  "孩子能供几年书、官营作坊的匠人是什么身份。",
    "战役与军事": "一场仗怎么打、为什么输赢：兵力、地形、补给与决策的细节。",
    "政变与权力": "权力更替的关键时刻：那一天发生了什么、谁做了什么选择、代价是什么。",
    "变法与改革": "一次制度变革的动机、阻力与结局，以及它在当时人生活里的痕迹。",
    "外交与交涉": "两个政权之间的谈判与冲突：谈了什么、为什么谈不成、事后如何。",
    "人物与抉择": "一个人处在关键时刻的选择：他的处境、他算的账、他付出的代价。",
    "交通与技术": "一项技术或工程的来龙去脉：谁做的、怎么做成的、成本与影响。",
    "文化工程": "书籍、典籍、工程与艺术背后的组织与人力：谁在主持、花了多少年。",
}

# 没给出类型时的兜底名（`classify()` 认不出来时用）
UNKNOWN_TYPE = "未分类"
# 用户自己制定的选题放在这个文件（config: story.user_topics；格式见 scripts/add_topic.py）
DEFAULT_USER_FILE = "data/topics_user.json"


@dataclass(frozen=True)
class UserPool:
    """用户自己制定的选题池：可以补充/新增类型，也可以追加选题条目。"""
    types: dict[str, str] = field(default_factory=dict)
    topics: list["Topic"] = field(default_factory=list)
    path: str = ""

    @property
    def is_empty(self) -> bool:
        return not self.types and not self.topics

    def merged_types(self) -> dict[str, str]:
        """内置类型表 + 用户自定义类型（用户可覆盖同名类型的描述）。"""
        return {**STORY_TYPES, **self.types}

    def items(self, mode: str = "small") -> list["Topic"]:
        m = str(mode or "small")
        return [t for t in self.topics if getattr(t, "mode", m) == m or not getattr(t, "mode", "")]


def user_pool_path(cfg=None) -> object:
    """用户选题池文件路径（读配置，相对项目根解析）。"""
    rel = DEFAULT_USER_FILE
    if cfg is not None:
        rel = str(cfg.story.get("user_topics") or DEFAULT_USER_FILE)
    q = Path(rel)
    if q.is_absolute():
        return q
    return PROJECT_ROOT / q


def load_user_pool(path=None) -> UserPool:
    """读用户选题池。文件不存在/坏掉都返回空池，不抛错（不能让手写的文件搞崩出片）。

    格式：
        {"types": {"自定义类型": "这一路故事讲什么"},
         "topics": [{"type": "自定义类型", "title": "标题", "desc": "这一期讲什么",
                     "mode": "small"}]}
    """
    q = Path(path) if path else user_pool_path()
    if not q.exists():
        return UserPool(path=str(q))
    try:
        raw = json.loads(q.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        log.warning("用户选题池读不了（忽略，继续用内置池）：%s —— %s", q, exc)
        return UserPool(path=str(q))
    types = {str(k): str(v) for k, v in (raw.get("types") or {}).items() if str(k).strip()}
    items: list[Topic] = []
    for it in raw.get("topics") or []:
        if not isinstance(it, dict):
            continue
        title = str(it.get("title") or "").strip()
        if not title:
            log.warning("用户选题池里有一条没有 title，跳过：%s", it)
            continue
        items.append(Topic(type=str(it.get("type") or "").strip(), title=title,
                           desc=str(it.get("desc") or "").strip(),
                           mode=str(it.get("mode") or "small").strip()))
    if items or types:
        log.info("用户选题池：%d 条选题 / %d 个自定义类型（%s）", len(items), len(types), q.name)
    return UserPool(types=types, topics=items, path=str(q))


def save_user_pool(pool: UserPool) -> object:
    q = Path(pool.path)
    q.parent.mkdir(parents=True, exist_ok=True)
    q.write_text(json.dumps({
        "types": pool.types,
        "topics": [{"type": t.type, "title": t.title, "desc": t.desc,
                    "mode": getattr(t, "mode", "small")} for t in pool.topics],
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    return q


# ---------------------------------------------------------------- L2 选题条目
@dataclass(frozen=True)
class Topic:
    """一个选题 = L1 类型 + L2 标题与描述。

    ⚠️ 位置参数顺序是 **(类型, 标题, 描述)** —— 按两级从高到低排的阅读顺序。
    池子里 60 多条都是这么位置传参的，改字段顺序会让整池数据错位
    （这个坑当场踩过：类型被塞进标题、标题被当成类型，池子打出来是空的）。
    """
    type: str = ""              # L1 故事类型（STORY_TYPES 的键，或用户自定义类型）
    title: str = ""             # L2 详细标题（原来那个字符串）
    desc: str = ""              # L2 与这个故事相关性最高的描述（可由 LLM 补）
    mode: str = ""              # 只对用户自定义条目有意义：small / event（空=both）

    @property
    def type_desc(self) -> str:
        return STORY_TYPES.get(self.type, "")

    def as_dict(self) -> dict:
        return {"title": self.title, "type": self.type, "type_desc": self.type_desc,
                "desc": self.desc}

    def __str__(self) -> str:   # 兼容旧代码把它当字符串用（日志、查重）
        return self.title


def topic_of(title: str, user: UserPool | None = None) -> Topic:
    """按标题回找一个池子里的条目（带类型和描述）；池子里没有就返回裸标题。"""
    pool = list((user or UserPool()).topics) + ITEMS_SMALL + ITEMS_EVENT
    for t in pool:
        if t.title == title:
            return t
    return Topic(title=str(title or ""))


def type_desc(name: str, user: UserPool | None = None) -> str:
    table = (user.merged_types() if user else STORY_TYPES)
    return table.get(str(name or ""), "")


def all_types(user: UserPool | None = None) -> list[str]:
    return list(user.merged_types() if user else STORY_TYPES)


# ---------------------------------------------------------------- 小切口池（默认）
# 每条 = Topic(类型, 标题, 描述)。描述只讲范围与要回答的问题，不塞具体结论。
ITEMS_SMALL: list[Topic] = [
    # ---- 行旅与驿传
    Topic("行旅与驿传", "官员出差：从京城到广州，走多久、住哪儿、路上花多少钱？",
          "一名官员奉命远行的完整账：路线怎么定、住驿站还是客栈、盘缠和随从的开销从哪出。"),
    Topic("行旅与驿传", "流放三千里：被流放的人一路上靠什么活下来？",
          "从判决到抵达戍地的全过程：怎么押解、路上吃什么、病了怎么办、有多少人走不到。"),
    Topic("行旅与驿传", "驿站那匹马：一封加急军报从边关到京城要跑几天？",
          "一封加急公文在路上的实际过程：怎么换马接力、速度等级怎么定、雨雪与疲劳怎么拖慢它。"),
    Topic("行旅与驿传", "进京赶考：一个乡下考生要走几个月，盘缠从哪来？",
          "乡下考生赴考的账：路程与月份、盘缠从哪凑、沿途投宿，以及考不上怎么回去。"),
    Topic("行旅与驿传", "漕运那一船粮：从江南运到北京，路上要损耗多少？",
          "漕粮从江南到北京的全链条：怎么征、怎么运、过闸要等多久，损耗落在谁头上。"),
    Topic("行旅与驿传", "没有公交的古代：普通人一辈子能走多远？",
          "普通人的出行半径：步行、搭车、雇船各要什么代价，绝大多数人一生走过多远。"),
    # ---- 生计与物价
    Topic("生计与物价", "没有冰箱的夏天：宫里的冰和百姓的井水",
          "夏天怎么消暑：官府的冰从哪来、给谁用，普通人家靠什么应付暑热。"),
    Topic("生计与物价", "古代的厕所与澡堂：那时候的人怎么讲卫生？",
          "城里的卫生设施与习惯：厕所、澡堂、污物怎么处置，以及这件事的阶层差别。"),
    Topic("生计与物价", "一顿饭多少钱：不同身份的人一天吃什么？",
          "不同身份的人一天吃什么、花多少：主食、菜、肉的出现频率与这笔开销占收入的比例。"),
    Topic("生计与物价", "生火这件小事：没有火柴的年代怎么点火？",
          "取火这件小事背后的一切：火种怎么保存、火石火镰怎么用、一炉火要多少柴炭。"),
    Topic("生计与物价", "夜里的城：宵禁之后出门会怎样？",
          "宵禁制度怎么落地：什么时候关坊门、谁在巡夜、犯规会怎样，以及夜市的例外。"),
    Topic("生计与物价", "下雨天：没有下水道的城市怎么排水？",
          "城市怎么对付雨水：沟渠怎么修、哪里最容易被淹、大雨之后最遭殃的是谁。"),
    # ---- 钱粮与税役
    Topic("钱粮与税役", "交税的日子：农民一年要交多少，交不上怎么办？",
          "一个农户一年的税与役：什么时候交、怎么折算、交不上会经历什么。"),
    Topic("钱粮与税役", "盐价与私盐：为什么盐贩子屡禁不止？",
          "盐从产地到餐桌的加价过程，以及私盐为什么总有活路、官府怎么查禁。"),
    Topic("钱粮与税役", "古代的钱怎么花：铜钱、银子、交子用起来有多麻烦？",
          "不同货币的实际使用体验：成色、兑价、携带与找零的麻烦，以及假币怎么防。"),
    # ---- 官府与吏治
    Topic("官府与吏治", "一个县令的账本：他的俸禄够养家、够应酬吗？",
          "一个县级官员的收入与支出：俸禄、幕僚师爷、迎来送往，以及缺口从哪补。"),
    Topic("官府与吏治", "上朝那一天：大臣几点起、站多久、能不能上厕所？",
          "朝会的完整流程与体感：几点起身、怎么站班、奏事规矩、散朝之后去哪。"),
    Topic("官府与吏治", "僧道之外：寺庙和道观在当时承担了哪些社会功能？",
          "寺观作为社会机构的那一面：救济、寄存、借贷、旅宿，以及户籍之外的人。"),
    # ---- 刑狱与流放
    Topic("刑狱与流放", "县衙大牢里：关在里面的人吃什么，家人能送饭吗？",
          "牢里的日常：饭食从哪来、家属能不能探送、狱卒的规矩与勒索。"),
    Topic("刑狱与流放", "被流放的官员：妻儿怎么安置，能不能回来？",
          "流放对一家人的影响：家口随行还是留乡、戍地的生活、多年之后有没有赦还的机会。"),
    Topic("刑狱与流放", "城破那一天：官员、商人、僧人、工匠各自的结局",
          "城破之后不同身份的人各自遭遇什么，秩序在几天内怎么崩掉、又怎么重建。"),
    # ---- 疾病与丧葬
    Topic("疾病与丧葬", "古代看病：一个普通人请得起郎中吗？",
          "普通人的就医账：请郎中、抓药各要什么代价，请不起的时候靠什么。"),
    Topic("疾病与丧葬", "一场瘟疫来了：城里都是谁在管、怎么管？",
          "疫情之下官府、医者、寺观、里甲各做什么，隔离与掩埋怎么执行。"),
    Topic("疾病与丧葬", "普通人的后事：一口棺材、一块地要花多少钱？",
          "一场丧事的实际花销：棺木、地、法事、宴席，以及穷人家怎么办。"),
    # ---- 战乱与灾变
    Topic("战乱与灾变", "战争里的普通人：被征的兵、随军的民夫、留在家的女人",
          "一场战争把普通人分成几种处境，每一种具体要面对什么。"),
    Topic("战乱与灾变", "饥荒那一年：朝廷怎么赈、百姓怎么活？",
          "灾年的赈济链条：报灾、勘灾、放粮，中间的损耗与百姓的自救。"),
    Topic("战乱与灾变", "兵从哪里来：一个人被征入伍，家里会怎样？",
          "征兵的制度与一家人随后的处境：谁去、去多久、少了劳力家里怎么撑。"),
    # ---- 婚育与教化
    Topic("婚育与教化", "古代的孩子几岁上学、学什么、学费多少？",
          "一个孩子的读书账：几岁开蒙、读什么书、束脩多少，普通人家能供几年。"),
    Topic("婚育与教化", "娶一个媳妇要多少钱：聘礼和嫁妆压垮过多少人家？",
          "一场婚事双方的账：聘礼、嫁妆、酒席，以及这笔支出对一个家庭的挤压。"),
    Topic("婚育与教化", "工匠那双手：官营作坊里的匠人过的是什么日子？",
          "官营作坊里匠人的作息、报酬与身份：怎么征来的、做什么、能不能脱离匠籍。"),
]

# ---------------------------------------------------------------- 事件式池（旧）
ITEMS_EVENT: list[Topic] = [
    # ---- 战役与军事
    Topic("战役与军事", "长平之战：四十万人是怎么没的", "一场围歼战怎么打成：地形、粮道、决策，以及被围之后发生了什么。"),
    Topic("战役与军事", "官渡之战：曹操怎样以弱胜强", "弱势一方靠什么翻盘：粮草、情报、内部叛变与决断时刻。"),
    Topic("战役与军事", "赤壁之战：一场大火改写了三国", "水战怎么打、火攻怎么成功，以及胜负之后天下的走向。"),
    Topic("战役与军事", "淝水之战：八万对八十万的真实内情", "兵力悬殊之下胜负如何发生：阵型、士气与那一次后退。"),
    Topic("战役与军事", "李愬雪夜入蔡州：中国军事史上最冷的一次奇袭", "一次极端天气下的奇袭怎么策划与执行。"),
    # ---- 政变与权力
    Topic("政变与权力", "荆轲刺秦王：一场注定失败的刺杀", "这场刺杀的策划、执行与失败原因，以及它在事后的意义。"),
    Topic("政变与权力", "鸿门宴上，项羽为什么没杀刘邦", "宴会上的座位、言语与决断，以及这个选择后来的代价。"),
    Topic("政变与权力", "巫蛊之祸：汉武帝晚年的那场宫廷风暴", "一场由巫蛊引发的宫廷清算怎么扩大，牵连了谁。"),
    Topic("政变与权力", "玄武门之变：那一天到底发生了什么", "这一天的时间线、参与者和事后安排。"),
    Topic("政变与权力", "靖康之变：北宋最后的那几天", "围城之下朝廷内部的决策与开封城最后的日子。"),
    Topic("政变与权力", "土木堡之变：皇帝被俘之后", "皇帝被俘后朝廷怎么应对，谁在主持局面。"),
    Topic("政变与权力", "岳飞之死：十二道金牌背后的账", "召还与下狱的过程，以及背后的政治账。"),
    Topic("政变与权力", "崖山海战：一个王朝最后的一天", "最后一战怎么打、最后的君臣做了什么。"),
    # ---- 变法与改革
    Topic("变法与改革", "王安石变法：一个人对抗整个时代的开始", "变法的目标、阻力与执行中的变形。"),
    Topic("变法与改革", "张居正改革：给大明续命的那十年", "改革动了谁的利、怎么推下去、人走之后为什么废掉。"),
    Topic("变法与改革", "洋务运动：造了三十年船，为什么还是没赢", "三十年办洋务的账：钱花在哪、谁在办、哪里没跟上。"),
    Topic("变法与改革", "戊戌变法：一百零三天", "这一百多天里做了什么、谁在反对、为什么会这样收场。"),
    Topic("变法与改革", "科举制是怎么诞生的", "选官方式怎么从推荐变成考试，以及这个变化解决了什么问题。"),
    # ---- 外交与交涉
    Topic("外交与交涉", "澶渊之盟：打胜了为什么还要给钱", "胜势之下为什么选择议和，以及这份和约的实际内容。"),
    Topic("外交与交涉", "尼布楚条约：康熙和俄国人谈了什么", "双方各自要什么、怎么谈的、边界怎么划。"),
    Topic("外交与交涉", "马戛尔尼访华：一场没能谈成的外交", "使团带来了什么、要求什么、为什么没谈成。"),
    # ---- 人物与抉择
    Topic("人物与抉择", "苏武牧羊：在北海边守了十九年", "一个人在极北之地的十九年：怎么活下来、靠什么撑住。"),
    Topic("人物与抉择", "司马迁为什么一定要写完《史记》", "他受刑之后的处境与这个选择背后的理由。"),
    Topic("人物与抉择", "玄奘西行：偷渡出关的取经人", "出发时的身份与路线：怎么出关、路上遇到什么。"),
    Topic("人物与抉择", "王阳明龙场悟道：被贬到蛮荒之后的顿悟", "被贬之后的处境与他在那里的转变。"),
    Topic("人物与抉择", "秦始皇统一六国：十年灭国战的细节", "十年之内灭六国的节奏与顺序，以及每一战的关键。"),
    # ---- 交通与技术
    Topic("交通与技术", "张骞凿空西域：十三年，一个人打通一条路", "出使的经过与路线：走了哪些地方、带回了什么。"),
    Topic("交通与技术", "交子：世界上第一张纸币是怎么出现的", "纸币在什么处境下被发明出来，以及它怎么流通。"),
    Topic("交通与技术", "郑和下西洋：七下西洋花了多少钱", "七次远航的组织与开销：船、人、货与回赐。"),
    Topic("交通与技术", "虎门销烟：禁烟背后的账本", "禁烟前后的账：走私规模、禁烟措施与它的后果。"),
    # ---- 文化工程
    Topic("文化工程", "敦煌藏经洞：一个道士和一个洞的发现", "洞是怎么被发现、里面的东西后来怎么流散。"),
    Topic("文化工程", "安史之乱：盛唐是怎么在一夜间转向的", "叛乱的起因、进程与它对人口和城市的影响。"),
]

# 兼容旧引用：只保留标题列表（按标题查类型用 `topic_of()`）
TOPICS_ANGLE: list[str] = [t.title for t in ITEMS_SMALL]
TOPICS_EVENT: list[str] = [t.title for t in ITEMS_EVENT]
TOPICS = TOPICS_ANGLE


def pool_for(mode: str, user: UserPool | None = None) -> list[Topic]:
    """某个切入方式的选题池（内置 + 用户自定义，返回带类型与描述的条目）。

    用户条目前面插（先看自己写的），内置在后。
    """
    builtin = list(ITEMS_EVENT if str(mode) == "event" else ITEMS_SMALL)
    return (list((user or UserPool()).items(mode)) if user else []) + builtin


def used_types(records: list[dict], last: int = 4, user: UserPool | None = None) -> list[str]:
    """最近几期做过的类型（用来让选题避开连着做同一类）。

    老记录没有 `topic_type` 字段（这个字段是后加的）→ 按标题回查池子补类型，
    回查不到再用关键词规则猜。
    为什么要补：不补的话「避开最近类型」这个功能要等所有期重跑一遍才生效，
    在那之前它一直是个空转的假开关（实测：日志里打「类型避开最近 0 期」）。
    """
    out: list[str] = []
    for r in records[-max(0, last):]:
        t = str(r.get("topic_type") or "").strip()
        if not t:
            title = str(r.get("topic") or r.get("title") or "")
            t = topic_of(title, user).type or (_guess_type(title) if title else "")
        if t:
            out.append(t)
    return out


def pick(seed: int | None = None, is_used=None, mode: str = "small",
         type_filter: str = "", recent_types: list[str] | None = None,
         user: UserPool | None = None) -> Topic | None:
    """从池子里挑一个**没用过**的选题。

    优先级：类型近期没做过 > 类型做过。同一档内随机。
    为什么按类型排优先级：连着做三期间一个类型的故事，观众会觉得栏目只会这一路。
    `is_used(candidate_title) -> bool` 由调用方给（它知道生成记录）；
    池子被挑完了返回 None，由调用方决定怎么办（本项目是让模型出个新题）。
    """
    pool = pool_for(mode, user)
    if type_filter:
        pool = [t for t in pool if t.type == type_filter]
    rng = random.Random(seed)
    rng.shuffle(pool)
    recent = set(recent_types or [])
    fresh = [t for t in pool if t.type not in recent]
    for group in (fresh, pool):
        for t in group:
            if is_used is not None and is_used(t.title):
                continue
            return t
    return None


# ---------------------------------------------------------------- 出题（池子挑空时用）
PROPOSE_SYSTEM_SMALL = """你是历史纪录片栏目的选题编辑。栏目形式是：**用一个小切口提问，再用史实回答问题**。

好的选题长这样：
  · 「流放三千里：被流放的人一路上靠什么活下来？」（从一个人的处境切进去）
  · 「官员出差：从京城到广州，走多久、住哪儿、路上花多少钱？」（从日常开销切进去）
  · 「城破那一天：官员、商人、僧人、工匠各自的结局」（从一个瞬间切进去）

要求：
1. 切口必须小、具体、日常 —— 围绕吃穿住行、钱、税、病、死、罪、路、时间、身份。
   不要「某场战役的经过」这类宏大叙事，也不要「某制度的演变史」这类知识罗列。
2. 小切口要能带出大历史：一个具体问题能照见一个时代的运行方式。
3. 能用史料回答：有制度、有档案、有具体数字（价格/里程/天数/人数）可以考据。
4. 有实物可配图：古画、文物、文书、器物、遗址、老照片。
5. 范围：先秦到清末民初。避开近现代政治敏感题材。
6. 不要重复用户已经做过的题材，也不要只是换同义词。
7. 先定**故事类型**，再定具体标题 —— 类型只能从给的类型表里选。
8. 全部简体中文，标题 15-30 字，用「小切口：具体疑问」的形式。
9. `desc` 写「这一期到底讲什么、要回答什么」，35-60 字；
   **不要在这里塞具体数字或结论**（那些要由写稿阶段的史实校验兜住）。"""

PROPOSE_SYSTEM_EVENT = """你是历史纪录片栏目的选题编辑，负责给一档 5-10 分钟的历史故事节目出题。

要求：
1. 只出**有明确事件线**的题材（起因 → 冲突 → 转折 → 结局），不要单纯的人物生平，
   也不要知识罗列式的题材（如「古代科举制的演变」）。
2. 有实物可配图：古画、文物、遗址、奏折、画像、老照片、地图。
3. 优先中国古代到近代（先秦—清末民初），避开近现代政治敏感题材。
4. 不要重复用户已经做过的题材，也不要只是把已做过的换成同义词。
5. 先定**故事类型**，再定具体标题 —— 类型只能从给的类型表里选。
6. `desc` 写「这一期到底讲什么、要回答什么」，35-60 字，不要塞具体数字或结论。
7. 全部简体中文。"""


def _types_block(mode: str) -> str:
    """给 LLM 的候选类型表（只列这个模式用得上的那几类）。"""
    if str(mode) == "event":
        names = ["战役与军事", "政变与权力", "变法与改革", "外交与交涉",
                 "人物与抉择", "交通与技术", "文化工程"]
    else:
        names = ["行旅与驿传", "生计与物价", "钱粮与税役", "官府与吏治",
                 "刑狱与流放", "疾病与丧葬", "战乱与灾变", "婚育与教化"]
    return "\n".join(f"- {n}：{STORY_TYPES[n]}" for n in names if n in STORY_TYPES)


def _propose_user(mode: str, avoid: str) -> str:
    return (f"可选的故事类型（type 必须从这里面选）：\n{_types_block(mode)}\n\n"
            f"我们已经做过下面这些题材了：\n\n{avoid}\n\n"
            f"请再给一个**全新的**题材。\n\n"
            f'【输出】只输出 JSON：{{"type": "类型名", "title": "标题", "desc": "这一期讲什么"}}')


def _parse_topic(data, mode: str) -> Topic | None:
    """把 LLM 的返回解析成 Topic（容错：type 不在表里就归到未分类）。"""
    if isinstance(data, str):
        data = {"title": data}
    if not isinstance(data, dict):
        return None
    title = str(data.get("title") or data.get("topic") or "").strip()
    if len(title) < 6:
        return None
    ttype = str(data.get("type") or "").strip()
    if ttype not in STORY_TYPES:
        ttype = _guess_type(title, mode)
    return Topic(title=title, type=ttype, desc=str(data.get("desc") or "").strip())


# 规则兜底：按标题里的关键词猜类型（LLM 挂了/返回不规范时用）
_TYPE_HINTS: list[tuple[str, tuple[str, ...]]] = [
    ("行旅与驿传", ("驿", "漕运", "赶考", "出差", "流放路上", "走多远", "路程")),
    ("钱粮与税役", ("税", "盐", "钱", "银子", "交子", "赋", "役", "俸禄")),
    ("刑狱与流放", ("牢", "狱", "流放", "城破", "斩", "案")),
    ("疾病与丧葬", ("病", "医", "瘟疫", "丧", "棺材", "后事")),
    ("战乱与灾变", ("战", "兵", "饥荒", "赈", "灾", "围城")),
    ("婚育与教化", ("娶", "嫁", "聘礼", "上学", "读书", "工匠")),
    ("官府与吏治", ("县令", "上朝", "官员", "官府", "衙", "朝")),
    ("生计与物价", ("吃", "饭", "冰", "厕所", "澡", "火", "宵禁", "排水", "价")),
]


def _guess_type(title: str, mode: str = "small") -> str:
    text = str(title or "")
    for name, hints in _TYPE_HINTS:
        if any(h in text for h in hints):
            return name
    return UNKNOWN_TYPE


def propose(cfg, llm, avoid: list[str], mode: str = "small") -> Topic:
    """让模型出一个没做过的新题材（两级：先定类型，再定标题与描述）。"""
    avoid_txt = "\n".join(f"- {a}" for a in avoid[-80:] if a) or "（还没有做过任何题材）"
    system = PROPOSE_SYSTEM_EVENT if str(mode) == "event" else PROPOSE_SYSTEM_SMALL
    try:
        data = llm.chat_json(system, _propose_user(mode, avoid_txt),
                             max_tokens=900, temperature=0.85)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"让模型出新题材失败：{exc}") from exc
    topic = _parse_topic(data, mode)
    if topic is None:
        raise RuntimeError("模型没有给出可用的新题材")
    return topic


ENSURE_DESC_SYSTEM = """你是历史纪录片栏目的选题编辑。给一个已有的选题补一句
**与这个故事相关性最高的描述**：说明这一期到底讲什么、要回答什么。

要求：
1. 35-60 字，一句话，简体中文。
2. **不要复述标题**，要说出标题背后的那件事。
3. **不要塞具体数字、年代或结论** —— 那些由写稿阶段的史实校验兜住，
   写在这里就等于把一个未经核实的说法固化进选题池。
4. 只输出 JSON：{"desc": "…"}"""


def ensure_desc(topic: Topic, cfg, llm) -> Topic:
    """补 L2 的描述（池子条目自带描述，手填 -t 的题需要现生成）。

    失败/没有 LLM 时原样返回 —— 描述是锦上添花，不能因为它阻断出片。
    """
    if topic.desc or llm is None:
        return topic
    try:
        data = llm.chat_json(
            ENSURE_DESC_SYSTEM,
            f"【类型】{topic.type or UNKNOWN_TYPE}"
            f"{('（' + topic.type_desc + '）') if topic.type_desc else ''}\n"
            f"【标题】{topic.title}\n\n【输出】只输出 JSON：{{\"desc\": \"…\"}}",
            max_tokens=300, temperature=0.6)
    except Exception:  # noqa: BLE001
        return topic
    desc = ""
    if isinstance(data, dict):
        desc = str(data.get("desc") or "").strip()
    if not desc or len(desc) < 10:
        return topic
    return Topic(title=topic.title, type=topic.type, desc=desc)


CLASSIFY_SYSTEM = """你是历史纪录片栏目的选题编辑。请判断下面这个选题属于哪个**故事类型**。

类型只能从表里选（把你选中的类型名原样输出）：
{types}

只输出 JSON：{{"type": "类型名"}}"""


def classify(title: str, cfg, llm, user: UserPool | None = None) -> str:
    """给一个手填的选题判类型（`-t "..."` 走的这条）。失败退回关键词规则。"""
    user_types = user.merged_types() if user else None
    if llm is not None:
        try:
            data = llm.chat_json(
                CLASSIFY_SYSTEM.format(types=_types_block("small") + "\n" + _types_block("event")),
                f"【选题】{str(title or '').strip()}\n\n【输出】只输出 JSON：{{\"type\": \"…\"}}",
                max_tokens=120, temperature=0.2)
            t = str((data or {}).get("type") or "").strip() if isinstance(data, dict) else ""
            if t in (user_types or STORY_TYPES):
                return t
        except Exception:  # noqa: BLE001
            pass
    return _guess_type(title)


def fill_levels(topic: Topic, cfg, llm) -> Topic:
    """补齐选题的两级：类型（没有就判一个）+ 描述（没有就生成一条）。

    这是**唯一**实现 —— `pipeline.run`（正式出片）和 `cli.cmd_plan`（只写稿）
    都走它，免得两条路慢慢长歪（一个补类型另一个不补，产物就对不上了）。

    任何一步失败都退回原值：两级是给写稿更好的语境，不能因为它阻断出片。
    """
    ttype = str(topic.type or "")
    tdesc = str(topic.desc or "")
    if not ttype:
        ttype = classify(topic.title, cfg, llm)
    if not tdesc:
        tdesc = ensure_desc(Topic(title=topic.title, type=ttype), cfg, llm).desc
    return Topic(title=topic.title, type=ttype, desc=tdesc)


def render_pool(mode: str = "small", user: UserPool | None = None) -> str:
    """把两级选题池打成可读文本（`run.bat topics` 用 —— 让人能先看类型再挑题）。"""
    pool = pool_for(mode, user)
    extra = f"，其中自己写的 {len((user or UserPool()).items(mode))} 条" if user else ""
    lines = [f"选题池（{mode} 模式，共 {len(pool)} 条{extra}）", ""]
    by_type: dict[str, list[Topic]] = {}
    for t in pool:
        by_type.setdefault(t.type, []).append(t)
    for name in all_types(user):
        items = by_type.get(name)
        if not items:
            continue
        lines.append(f"■ {name}　{type_desc(name, user)}")
        for t in items:
            lines.append(f"    · {t.title}")
            if t.desc:
                lines.append(f"      {t.desc}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
