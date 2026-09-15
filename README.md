# historical_story_gen · 历史小故事视频生成

给一个历史题材（或者不给我），跑出一条 5-10 分钟的视频：

```
选素材 → AI 出分章大纲 → AI 逐章写口播稿（切成「分镜」）
      → 史实校验（程序化 + LLM 审校，不合格带问题重写）
      → TTS 配音 → 找历史配图 → Pillow 合成画面 + ASS 字幕
      → ffmpeg 编码分镜 → 拼接 → 横屏 / 竖屏两版成片
```

一句话概括技术选择：**文本用 DeepSeek，语音用 MiniMax（克隆音色），画面用 Pillow 离线合成，
ffmpeg 只做编码和拼接**。

> 🤖 让 AI agent 在本项目干活前，先读 **`AGENTS.md`** —— 那里写了硬性约定
> （文本模型锁、配音固定用哪个音色和语速、改代码的护栏、已知机制局限）。

---

## 1. 快速开始

依赖已经装好了（`.venv` + 项目已 editable 安装）。

```bat
cd /d D:\Resources\code\historical_story_gen

run.bat                                           :: 随机题材，出横竖两版
run.bat -t "赤壁之战：一场大火如何改写三国"          :: 指定题材
run.bat --minutes 9                               :: 目标 9 分钟
run.bat plan -t "安史之乱"                          :: 只写稿（几毛钱，不出视频）
```

不想用批处理也可以：

```bash
.venv/Scripts/python.exe run.py -t "鸿门宴上，项羽为什么没杀刘邦"
```

> 从 Hermes 这类会注入 uv `PYTHONHOME` 的终端里跑，前面加
> `env -u PYTHONHOME -u UV_INTERNAL__PYTHONHOME`；`run.bat` 里已经清掉了。
>
> 完整命令表见 `命令速查.md`。

---

## 2. 目录结构

```
historical_story_gen/
├── AGENTS.md              给 AI agent 的项目约定（硬性约定/护栏/已知局限）← 先读这个
├── config.yaml            所有可调参数（时长、音色、图源、字幕、编码…）
├── README.md / 命令速查.md 说明与命令表
├── run.bat / run.py       入口
├── src/hsg/
│   ├── config.py          配置加载 + 密钥三路兜底 + 文本模型锁
│   ├── models.py          Story / Chapter / Scene 数据结构
│   ├── textfmt.py         口播清洗（去 markdown/来源词、日期不补零、折行修正）
│   ├── llm.py             OpenAI 兼容的 LLM 客户端（JSON 模式 + 截断自动重试）
│   ├── material.py        素材检索（Bing 摘要，作人名/年份锚点）
│   ├── topics.py          选题池（默认随机挑）
│   ├── outline.py         阶段 A：分章大纲
│   ├── writer.py          阶段 B：逐章写稿 + 长度自适应扩写/精简
│   ├── verify.py          史实校验（规则层 + LLM 审校 + 锚点核查 + 复检闸门）
│   ├── tts.py             MiniMax T2A（+ edge-tts 兜底）
│   ├── images.py          配图：版权安全图库优先（Cleveland CC0 等）+ 年代匹配 + 去重
│   ├── subtitles.py       ASS 字幕（按分镜时长铺时间轴）
│   ├── media.py           Pillow 合成画面（背景层 + 前景层，两块分开出图）
│   ├── video.py           ffmpeg：编码片段 / 拼接 / 混 BGM / 读回参数
│   ├── pipeline.py        编排 + 长度控制 + 产物输出
│   └── cli.py             命令行
├── scripts/
│   ├── smoke_video.py     零 LLM 的媒体链路冒烟测试 ← 媒体出问题先跑它
│   ├── test_verify.py     零成本回归测试（清洗/校验/复检/版权策略/查重/分镜编号/语速传导，189 项）
│   ├── probe_clean_images.py  零成本配图专项探测（clean 策略下的命中率/版权/年代）
│   ├── rerender.py        从已有 metadata 重渲染（复用文稿和语音，只换配图/画面）
│   ├── check_layout.py    量一帧里标题带和字幕带是否重叠（不靠肉眼）
│   ├── clone_voice.py     用参考音频克隆音色（MiniMax voice_clone → 可复用的 voice_id）
│   ├── tts_preview.py     多音色 A/B 试听（同一篇稿子各出一条 mp3，不跑 LLM/配图/渲染）
│   └── audit_config.py    配置审计（找出代码从不读的假开关 / 配置里没写的键）
└── data/                  产物（见「产物位置」）
```

---

## 3. 每个阶段在做什么、为什么这么做

### 3.1 素材（material.py）
拿 Bing 检索摘要当「人名/年份锚点」。**不是**让模型照抄，而是给它一个参照，
写稿仍以模型自身知识为主，再靠 `verify.py` 兜住事实。

为什么不用维基百科 —— 实测（2026-09，国内网络）：

| 来源 | 实测结果 |
|---|---|
| `zh.wikipedia.org` / `commons.wikimedia.org` | 连接超时，不可用 |
| `baike.baidu.com` | HTTP 403（人机校验） |
| `cn.bing.com`（网页/图片检索） | **可用** ← 采用 |
| `api.artic.edu`（芝加哥艺术博物馆开放接口） | 可用，公共领域 |
| `collectionapi.metmuseum.org`（大都会 CC0） | 可用 |
| `openaccess-api.clevelandart.org` | 超时 |

### 3.2 写稿：为什么要分成两次 LLM 调用
- **阶段 A（大纲）**：一次调用只出结构（章节标题、要点、目标秒数、配图检索词）。
- **阶段 B（写稿）**：每章一次调用，返回若干「分镜」，每段 70-120 字。
- 好处：输出短、不易截断；6 章可以 **4 路并发**（实测比单次大调用快得多）；
  长度控制有地方下手（章节数 / 每章秒数）。

### 3.2b 切入方式：小切口（默认）还是事件式

`config.yaml` 的 `story.angle_mode` 决定整期的内容视角 —— 选题池、大纲、写稿、审校
四处提示词会一起切过去：

| 模式 | 讲法 | 例子 |
|---|---|---|
| `small`（默认） | 从一个具体的日常问题切进去，再用史实把答案讲成故事 | 「流放三千里：被流放的人一路上靠什么活下来？」<br>「城破那一天：官员、商人、僧人、工匠各自的结局」<br>「官员出差：从京城到广州，走多久、住哪儿、路上花多少钱？」 |
| `event` | 直接讲一场战役 / 一次变法的来龙去脉（旧行为） | 「赤壁之战：一场大火改写了三国」 |

小切口模式有三个关键设计，**每一个都是实跑撞出来的**：

1. **大纲必须先给「史实锚点」（facts）** —— 每章至少一条可考事实；
   写不出来就说明这个角度立不住，要换角度。
   起因：第一版让模型自己回答「一天走多少里、一天几文」，它为了答得漂亮，
   直接编了「每天走五十里」「米一升折银五厘」还说成《大清会典》的规定 ——
   审校一次抓出 8 条编造的具体数字。
2. **写稿前有「锚点核查」闸门**（`verify.audit_facts`，由 `verify.fact_audit` 控制）：
   逐条判断锚点 ok / unsure / wrong —— ok 保留、unsure 降级标注、wrong 删除。
   降级后的锚点写稿时只能用模糊说法（「史料记载不一 / 没有明确规定」），
   不许引出处和数字。
   判定标准要分清两类：**制度层面的通行描述（学界共识）判 ok，只有精确数字
   和逐字引用才要求出处落实**。第一版标准过严，18 条锚点全判存疑，
   内容被抽空 —— 严不等于好，会把节目变成一堆「史料没记载」。
3. **写稿只许用已确认的锚点**，并且明确允许「没有记载」本身就是答案。
   实测这句话很管用：稿子里会出现「律例里并没有规定一天要走多少里，
   实际快慢全看路况、天气和押解的差役」—— 可信度和信息量都高于一个编出来的数字。

四轮 prompt 迭代的效果（同一题材）：

| 轮次 | 结果 |
|---|---|
| 只有小切口 prompt | 审校抓出 **8 条编造数字**（还都声称出自某会典） |
| + 大纲 facts 锚点 + 写稿只许用锚点 | 降到 **2 条**真错（年代张冠李戴、里程与实际不符），不再有编造出处 |
| + 锚点核查（标准过严） | 18 条全判存疑 → 内容被抽空 |
| + 核查标准分两类 | 确认 8 / 存疑降级 10 / 删除 0，成稿后审校 **0 条必修** |

### 3.3 分镜（Scene）是整个流水线的原子单位
一个分镜 = 一段口播 + 一张配图 + 一条字幕 + 一段语音。

这么切的原因：如果一章 68 秒只配一张图会显得很呆；而如果整章合成一段语音再
按时间切开对齐画面，切口很容易切在字中间。按分镜合成则天然是
「一段文字 = 一段语音 = 一张图 = 一片字幕」，边界严丝合缝，字幕和语音不可能错位。

配套的一个正确性细节：音频缓存的文件名是 `scene_003_<文本哈希>.mp3`。
只按分镜号命名的话，改写后（分镜号没变、文字变了）会复用到旧语音，
字幕和声音就对不上了。

### 3.4 史实校验（verify.py）—— 这是本项目最值钱的一环
两层：

**规则层（零成本）**：空/过短/过长分镜、跨分镜重复句子、占位符残留、
markdown 残留、清洗后仍出现的「百科/维基/百度」等来源词、
年份是否落在故事声明的年代区间附近。

**LLM 审校层**：让模型当「史实校对员」，对照年代区间与素材逐条挑错，
只报能确证的问题（宁可漏报不要误报）。命中的分镜带着「错在哪、应该怎么改」
重写一次。

实测效果（第一次真跑「赤壁之战」，18 个分镜）：审校抓出 **10 条真实错误**，
包括

- 「刘备南撤到江夏」→ 应为夏口（且与后文自相矛盾）
- 「周瑜任左都督、程普任右都督」→ 东吴当时是**左督/右督**，「都督」是后世官称
- 「孙刘联军逆江而上」→ 方向说反（联军是溯江西上迎击，曹军才是顺江东下）
- 「三国鼎立从此拉开」→ 赤壁之后只是**三分雏形**，鼎立正式形成于 229 年孙权称帝
- 「火攻后周瑜立刻拿下南郡」→ 南郡之战打到 209 年，时间线不准

这类错误提示词约束根本拦不住，必须靠一遍独立的审校。

**复检闸门（verify.final_check）**：审校发现问题 → 带纠正意见重写章节 →
**重写的结果必须再验一遍**（规则层 + 审校层各跑一次）。没有这一步，
「修没修好」全靠猜。

- 复检通过 → 正常进 TTS。
- 复检仍有必修问题 → 清单写进稿件的「⚠️ 待人工核对」一节 + metadata 的
  `unresolved`，日志显著提示；
  `verify.block_on_fail: true` 时直接中止（退出码 3），**有问题就不出片**。

实测：审校抓 10 条 → 改写后复检 0 条必修（鸿门宴）；城破那天抓 3 条真错 →
复检归零。没归零的会以清单形式交到人手上，不会被静默忽略。

### 3.5 配图（images.py）—— 版权安全优先

**第一原则是版权**（防止视觉中国式索赔），所以默认 `images.license_policy: clean`：
**只用版权明确的图库**，一个分镜一张，找到即用。

| 图源 | 版权 | 需要 key | 特点 |
|---|---|---|---|
| `cleveland` | **CC0 公共领域** | 不需要 | 克利夫兰艺术博物馆。带 `creation_date`，**能按年代匹配**；中国绘画收藏好，图片两三 MB，下载快 |
| `artic` | **公共领域** | 不需要 | 芝加哥艺术博物馆。带 date_start/date_end |
| `met` | **CC0** | 不需要 | 大都会艺术博物馆。可限定「亚洲艺术部」，但原图常有十几 MB，排在后面 |
| `unsplash` | Unsplash License（免费商用、免署名） | `UNSPLASH_ACCESS_KEY` | 现代摄影，历史题材基本用不上 |
| `pexels` | Pexels License（免费商用、免署名） | `PEXELS_API_KEY` | 同上 |
| `si` | CC0 | `SMITHSONIAN_API_KEY` | Smithsonian Open Access |
| `rijks` | 公共领域 | `RIJKSMUSEUM_API_KEY` | 荷兰国立博物馆 |
| `bing` | **版权未明** | 不需要 | 全网关键词检索。**只在 `mixed` 策略下参与兜底** |

没配 key 的图源会自动跳过，不影响运行 —— 缺 key 不会让流程没图可用。

**实测（2026-09，国内网络直连）**：无 key 且**真的下得动**的公共领域源，目前只剩
`cleveland` 一家（met 的图服务器只有 20 KB/s，artic 的图床一律 403 —— 搜得到但下不动）。
Openverse 连接超时、美国国会图书馆被 Cloudflare 拦（403）、Wikimedia Commons 不可达、
Rijksmuseum 旧 demo key 已失效。所以要扩充图源，最实际的是去领 Unsplash / Pexels
的免费 key（注册即得），填进环境变量即可，代码不用改。

选图顺序（**实跑调出来的**）：

1. **年代贴合** —— 博物馆接口带年代字段，明末的故事优先选明代作品（陈洪绶 1598–1652
   这种），宋代山水差 470 年就往后排。这是用博物馆图源换来的额外好处。
2. **馆藏来源**（`prefer_page_keywords`：博物馆/档案馆/美术馆/古籍库）优先。
3. **美术标记**（画/卷/壁画/绢本/青铜/陶/俑…）优先。
4. **版权干净** 优先于版权未明。
5. **图源顺序**（同样贴合时克利夫兰/芝加哥排在 Met 前面，因为它们给的图小、下得快）。

两条必须的护栏：

- **文化圈过滤（`culture_filter`）**：博物馆接口是关键词匹配，版权干净也可能**离题** ——
  实测搜 `Ming dynasty painting city wall` 时，芝加哥艺术馆返回了《大碗岛的星期天》和
  伦敦议会大厦，大都会返回了波斯手抄本。讲明代城破的片子里放一张莫奈，比放现代照片
  还糟。所以默认只保留中国相关作品（`culture_keywords`），讲日本/欧洲故事时改这个词表。
- **单图体积上限（`max_mb: 25`）**：博物馆原图有几十 MB 的，画布最多 4K，超限直接跳过，
  否则一张图就能把流程卡住几分钟。

每张图选了哪张、什么许可证、什么出处，都写进 `data/images/_sources.json`
与稿件末尾的「配图出处与版权」一节 —— 公开被质询时这就是凭据。
版权未明的图会被显式标成 `⚠️版权未明`，不会混在干净图里看不出来。

想临时用回关键词检索（赶时间/自己判断风险）：`run.bat -t "主题" --license-policy mixed`，
或把 config 的 `license_policy` 改成 `mixed`。

背景：实跑搜「函谷关遗址 古关隘」拿到的是现代景区照片，画面里**有电线杆**，
历史沉浸感当场破功。上面这套排序是为此加的。

配套的质量措施：
- 分辨率门槛（默认 ≥700×480）、长宽比门槛、明显不适合当背景的 URL 直接跳过；
- **平均哈希去重**：同一张图不会在片子里出现两次；
- 每个分镜的检索过程写进 `data/images/_sources.json`（试了哪些词、多少候选、
  最终选了哪张、来自哪一组），没图时好排查。

**⚠️ 诚实说明这条链路的边界**：关键词检索没法保证「年代准确」。
排序优化能去掉大部分现代元素（景区照、素材图），但仍可能选到年代不对的历史照片
（实测秦汉语境下选到过一张民国时期的老照片）。要真正解决只有两条路：
① 把图源限制成 `["artic","met"]`（博物馆开放接口，公共领域、年代靠谱，但命中率低）；
② 接一个视觉模型做配图复核（需要视觉模型 key，当前项目没有，属可选扩展）。

博物馆接口的图是公共领域，Bing 搜来的图版权不明 —— 每个分镜的出处都会写进
稿件的「配图出处」段和 `metadata.json`。`images.show_credit: true` 可以把出处
显示在画面上（默认关）。要公开发布建议走 `artic`/`met` 那条路。

### 3.6 画面（media.py + video.py）
Pillow 出**两层**图，运动交给 ffmpeg：

- `bg.jpg` —— 配图按 `motion_scale`(1.16) 放大后铺满 + 压暗，**这层在 ffmpeg 里缓移**
- `fg.png` —— 透明层：上下压暗渐变 + 栏目名/章节标题/图注，**这层不动**

这样配图缓慢横移时文字纹丝不动，比整帧缩放耐看得多；运动用 `crop` 的
时间表达式实现（比 `zoompan` 便宜得多，20 秒片段只编 10 秒）。

分段编码 + `concat -c copy` 拼接。转场是「淡黑」而不是 `xfade` ——
`xfade` 要全片重编码且滤镜链脆弱，淡黑在片段编码阶段就完成了，拼接是秒级的。

### 3.7 长度控制（pipeline.fit_length）
需求是 5-10 分钟，所以设了硬性区间 `[300s, 600s]`：

1. TTS 完之后立刻量总时长；
2. 在区间内 → 直接过（实测第一次就跑出 406.7s = 6.78 分钟）；
3. 超区间 → 按比例算出每章目标，挑偏差最大的章节带指令改写
   （扩写要求「补细节/场景/因果」，精简要求「砍重复/铺陈」），只重合成被改写的章节；
   最多两轮。

即临时长仍超出会明确告警，并指出该调哪个旋钮（`story.chapters` /
`seconds_per_chapter`）—— 不会静默给一条 4 分钟或 18 分钟的片子。

---

## 4. 模型选择（硬性）

### 4.1 文本：只用 DeepSeek

**文本内容只用 DeepSeek，MiniMax 只用于 TTS 与音色克隆。** 三层固化，不是靠注释：

| 层 | 机制 | 拦掉什么 |
|---|---|---|
| 启动 | `config.assert_text_provider()` 非 deepseek 直接抛错 | 有人改了 `config.yaml` |
| CLI | `--provider` 只接受 `deepseek`（argparse 直接退出 2） | `--provider minimax` |
| 运行时 | 每次运行打印分工横幅 | 「到底是不是在用 DeepSeek」的疑问 |

每次运行都会看到：

```
文本模型 (选题/写稿/史实审校) : deepseek / deepseek-chat
语音合成 (TTS)                : minimax / audiobook_male_1
```

要换文本模型：同时改 `src/hsg/config.py` 的 `TEXT_PROVIDER_LOCK` 和
`config.yaml` 的 `llm.provider`，两处必须一致。

### 4.2 语音：固定用系统音色 audiobook_male_1，语速 1.1

```yaml
tts:
  minimax:
    voice_id: "audiobook_male_1"   # 系统音色（默认）
    speed: 1.1
```

- 2026-09-14 曾换成克隆音色 `hsg_story_v2`，试听后认为听感不佳，当天改回系统音色。
  克隆流程本身可用，`hsg_story_v2` 仍挂在账号下：
  `data/base_voice/api-response_2.mp3`（15.87s 参考音频）经 MiniMax `voice_clone` 克隆而来。
- 克隆/换音频：`python scripts\clone_voice.py --check` 先验门槛，再
  `--voice-id <新id>`；工具会打印该往 config 里填什么。
- **换音色或改语速后必须重新校准 `story.chars_per_second`**，详见 §6。
  校准用 `python scripts\calib_rate.py`（0 成本，从已生成期的逐分镜时长反算），
  不要直接填 `probe-tts` 的范文值 —— 那个偏快。
- 想看两个音色的差别：`python scripts\tts_preview.py --script <脚本.md>
  --voice audiobook_male_1 --voice hsg_story_v2 --announce`。

---

## 5. 密钥（三路兜底）

`DEEPSEEK_API_KEY` / `MINIMAX_API_KEY` 本机在 **Windows 系统级环境变量**里，
正常不用配。但有些从图形界面启动的终端会把子进程环境清洗一遍（实测 Hermes 里
Python 读到的是空字符串），所以读取顺序是：

1. 进程环境变量
2. 项目 `.env`（参考 `.env.example`，会被 .gitignore 忽略）
3. **Windows 注册表**里的 Machine / User 环境变量（`src/hsg/config.py` 的
   `_registry_env`）—— 这是绕开清洗的那一层

三种途径任意一种有值就能跑，运行时会打印 `API Key：deepseek=已读取 minimax=已读取`。

---

## 6. 已知限制 / 需要注意

- **时长靠章节数而不是靠"目标时长"**。`story.target_total_seconds` 只用来算比例
  和打日志；真正决定内容量的是 `story.chapters` 和 `seconds_per_chapter`。
- **语速换算必须实测，而且必须跟上音色**。当前 `chars_per_second: 4.60` 是
  系统音色 `audiobook_male_1` + `speed=1.1` 的实测值（从第 1–6 期 90 段
  10760 字 / 2341.8 秒反算 = 4.59 字/秒）。
  ⚠️ 换算值要用 `python scripts\calib_rate.py` 从**已生成期的逐分镜时长**反算，
  不要直接填 `run.bat probe-tts` 的范文值 —— probe-tts 是连续合成，比流水线的
  分镜合成快一截（克隆音色那次：范文 3.91 / 实际 3.45；系统音色 4.77 / 实际 4.59）。
  换音色或改语速后必须重算，否则初始估算偏、多花一轮改写 + TTS。
  参考：edge 的 `zh-CN-YunxiNeural` 实测 4.75 字/秒@1.0、5.22@1.1。
- **素材摘要质量一般**（Bing 摘要会混进游戏站、诗文站）。它的定位只是锚点，
  事实准确性最终靠审校层。
- **配图版权**：默认 `license_policy: clean`，只用 CC0/公共领域/免费商用图库
  （实测能用的只有克利夫兰艺术博物馆一家，见 3.5）。Bing（版权未明）只在
  `mixed` 策略下兜底。想扩充图源就去领 Unsplash/Pexels 免费 key 填环境变量。
- **TTS 兜底必须真的装上**。`tts.fallback_edge_tts: true` 只是代码路径 ——
  2026-09-11 真踩过：MiniMax 余额耗尽（错误码 1008），18 段语音全灭，
  而兜底报的是 `No module named 'edge_tts'`。已 `pip install edge-tts` 修好，
  现在 `run.bat --tts-provider edge` 可以用免费音色跑通全流程（试音/验证用）。
- **BGM 默认关闭**。要开：把一首免版权曲（如 Mixkit Free Music）放到
  `data/bgm/story_bed.mp3`，然后 `bgm.enabled: true`。不要用有版权的影视/节目配乐。
- 渲染时间：一段 20 秒竖屏片段在 preset=medium 下约 10 秒编完，18 个分镜 ×2 个
  朝向 ≈ 6-8 分钟。想更快把 `video.preset` 改成 `faster`（实测差别不大）。

---

## 7. 生成记录与选题去重（不重复讲做过的故事）

每次跑完自动追加一条记录：

```
data/history.json     机器可读的完整记录（查重就靠它）
data/生成记录.md       人看的表格版（累计期数、时长、配图命中、校验情况）
```

看一下做过什么：

```bat
run.bat history                 :: 列出已生成的期目
run.bat history --backfill      :: 把 data/output 下已有的 metadata 补录进来
```

去重是两层的，因为「同一个故事换个说法」纯规则抓不住：

| 层 | 判据 | 成本 |
|---|---|---|
| 规则层 | 规范化后相同 / 一方是另一方子串（≥3 字）/ 二元组 Jaccard ≥ 0.55 | 免费 |
| 模型裁定层 | 只在「沾边但规则判不出」时调一次 DeepSeek，问「这两个题材是不是同一个历史事件」 | 一次便宜调用 |

实测效果：

  · `-t "鸿门宴"` → 规则层命中（子串），直接拦下，退出码 2
  · `-t "鸿门宴：项羽为什么放走了刘邦"` → 规则层判不出来（Jaccard 只有 0.19），
    模型裁定层判定「与已做过的《鸿门一宴，项羽为何放走刘邦？》是同一个故事」→ 拦下
  · 不带 `-t` 自动选题 → 自动跳过做过的题材（每次都会打印「已生成 N 期，本次已避开做过的」）
  · 选题池 32 个题材全部做过之后 → 让 DeepSeek 出一个没做过的新题（出重了会重出，最多 3 次）

被拦下时的提示：

```
ERROR  这个题材已经做过了：鸿门一宴，项羽为何放走刘邦？（2026-09-11，6.8 分钟）
         · 想换一个：去掉 -t/--topic，选题会自动跳过做过的题材
         · 确实要重做：加 --allow-duplicate
```

想去掉这个机制：`config.yaml` 里 `runtime.record_history: false`（记录仍可手动用
`history --backfill` 生成，但不再查重）。

记录里存了什么（`history.json` 一条的样子）：

```json
{
  "run_id": "20260911-114614",
  "generated_at": "2026-09-11T11:46:14",
  "topic": "虎门销烟：禁烟背后的账本",
  "title": "白银、鸦片与钦差：虎门销烟前的三本账",
  "chapters": 6, "scenes": 18, "chars": 1827,
  "speech_seconds": 442.0, "speech_minutes": 7.37,
  "video_seconds": {"portrait": 489.5, "landscape": 489.5},
  "images": {"total": 18, "found": 18, "fallback": 0},
  "verify": {"rule_fail": 0, "rule_warn": 0, "audit_issues": 3, "regenerated_chapters": [5]},
  "outputs": ["...\\_竖屏.mp4", "...\\_横屏.mp4"],
  "text_model": "deepseek/deepseek-chat",
  "tts": "minimax/audiobook_male_1"
}
```

---

## 8. 产物位置

```
data/output/20260911_<标题>_横屏.mp4        成片
data/output/20260911_<标题>_竖屏.mp4        成片
data/output/20260911_<标题>_脚本.md         口播稿 + 章节结构 + 配图出处
data/output/20260911_<标题>_metadata.json   全量元数据（分镜/时长/配图/时间轴/校验）
data/history.json                          生成记录（查重依据）
data/生成记录.md                            生成记录（人看的表格版）
data/material/                             素材缓存
data/audio/                                每个分镜的语音（文件名带文本哈希）
data/images/                               配图 + _sources.json
data/slides/<朝向>/                         画面（*_bg.jpg 配图层 / *_fg.png 文字层）
data/segments/<朝向>/                       分镜片段 + _concat_*.mp4（拼接母版）
```

---

## 9. 排查顺序（媒体出问题不要先怀疑流水线）

```bat
run.bat smoke          :: 零 LLM 跑通 配图→画面→字幕→编码→拼接
python scripts\smoke_video.py --no-tts    :: 连 TTS 都不用，完全不花钱
```

`smoke` 通过说明媒体层没问题，问题在内容层（就先看 `plan` 的输出）；
`smoke` 不通过，报错信息能直接定位到是配图、Pillow 合成、ASS 还是 ffmpeg。

判断版面（标题和字幕有没有打架）不要靠肉眼：

```bat
ffmpeg -hide_banner -loglevel error -y -ss 12 -i <成片> -frames:v 1 frame.png
python scripts\check_layout.py frame.png
```
