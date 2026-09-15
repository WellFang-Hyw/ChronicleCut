# AGENTS.md — historical_story_gen 项目约定

给所有在本项目里干活的 AI agent（以及未来的自己）看的约定。
**先读这一页，再看代码。** 详细背景在 `README.md`，命令表在 `命令速查.md`。

---

## 1. 这是什么

历史小故事视频生成器。一条流水线出一期 5–10 分钟成片（横竖两版）：

```
选题 → 素材(Bing摘要) → 大纲 → 锚点核查 → 逐章写稿(分镜粒度)
     → 规则校验 + 史实审校 → 复检 → TTS → 时长自适应改写
     → 配图(版权安全) → Pillow 两层画面 + ASS 字幕 → ffmpeg 编码 → 拼接
```

---

## 2. 硬性约定（不要自作主张改）

### 2.1 文本内容只用 DeepSeek

MiniMax **只**用于语音（TTS 与音色克隆）。这条有三层锁，改配置也绕不过去：

| 层 | 位置 |
|---|---|
| 启动校验 | `src/hsg/config.py` 的 `assert_text_provider()` |
| CLI | `--provider` 的 choices 只接受 `deepseek` |
| 运行时 | 每次运行打印分工横幅 |

真要换文本模型：必须**同时**改 `config.py` 的 `TEXT_PROVIDER_LOCK` 和 `config.yaml` 的
`llm.provider`，两处一致才放行。用户对这条反复强调过 —— 他不要口头确认，要能跑出来的证据。

### 2.2 配音固定用系统音色 audiobook_male_1，语速 1.1

用户 2026-09-14 先是指定换成克隆音色 `hsg_story_v2`，试听后认为听感不佳，
当天改回系统默认音色 `audiobook_male_1`，语速保持 1.1：

```yaml
tts:
  minimax:
    voice_id: "audiobook_male_1"   # 系统音色（默认）
    speed: 1.1                     # 语速 1.1（1.0 太慢）
```

- 克隆流程本身可用（`hsg_story_v2` 仍挂在账号下）：`data/base_voice/api-response_2.mp3`
  （15.87s 参考音频）经 MiniMax `voice_clone` 克隆而来。
  想再试克隆音色：`python scripts\clone_voice.py --check` 先验 → `--voice-id <id>` 克隆
  → 把 id 填回上面两行。
- **换音色或改语速后必须重新校准语速换算**：

  ```bat
  run.bat probe-tts        :: 先看个大概
  ```

  ⚠️ 但 **probe-tts 的值偏快，不要直接填**。它是整篇范文一次合成，
  而流水线是按分镜逐段合成、每段都带首尾静音。正确的做法是拿实拍的
  逐分镜时长反算（`data/output/*_脚本.md` 里每段都记着字数与时长）：

  - 当前值：`story.chars_per_second: 4.60`
    （来源：第 1–6 期 90 段 / 10760 字 / 2341.8 秒 = 4.59 字/秒）
  - 同时期 probe-tts 给 4.77，克隆音色那次范文 3.91 vs 实际 3.45，差的就是这一段。

  0 成本反算脚本：`python scripts\calib_rate.py`（含方法自校验：拿已知值的那一期
  复算，算得出来才信）。不要拿单句样本反推速率去凑一个值 —— 短句带头尾停顿，
  反推会偏低，等于给自己造一个「看着合理、其实没依据」的数字。

---

## 3. 环境（Windows）

- shell 是 git-bash，不是 PowerShell。
- **从 Hermes/uv 终端跑 Python 必须清 `PYTHONHOME`**，否则别的解释器会崩在
  `AssertionError: SRE module mismatch`：

  ```bash
  env -u PYTHONHOME -u UV_INTERNAL__PYTHONHOME .venv/Scripts/python.exe run.py --help
  ```

  `run.bat` 内部已经清掉了，所以优先用 `run.bat`。
- API key 三路兜底：进程环境变量 → 项目 `.env` → **Windows 注册表**。
  注册表那层不是多余的：Hermes 会清洗子进程环境变量，实测 Python 里读到空字符串，
  而注册表里有值（`src/hsg/config.py` 的 `_registry_env`）。
- 跑脚本一律用 `.venv/Scripts/python.exe`（项目已 editable 安装）。

---

## 4. 常用命令

```bat
run.bat                                   :: 随机选题，全流程，横竖两版
run.bat -t "主题" --minutes 9              :: 指定题材 / 目标时长
run.bat plan -t "主题"                     :: 只写稿（不花 TTS 和渲染）
run.bat probe-tts                         :: 实测字/秒（换音色/语速后必跑）
run.bat test                              :: 零成本回归测试（189 项，不调 API）
run.bat smoke                             :: 零 LLM 媒体链路冒烟
run.bat history [--backfill]              :: 生成记录 / 选题去重
python scripts\clone_voice.py --list      :: 列出账号下的克隆音色
python scripts\tts_preview.py --script <脚本.md> --voice A --voice B
python scripts\audit_config.py            :: 配置审计（动了 config.yaml 之后跑一下）
python scripts\rerender.py                :: 复用文稿+语音，只重做配图/画面
python scripts\check_layout.py frame.png  :: 程序化判定标题带/字幕带是否重叠
```

**改完代码先跑 `run.bat test`**（189 项，零成本，覆盖的都是实跑撞过的坑）。

---

## 5. 改代码时的护栏

每一条都是实跑撞出来的，改动相关内容时不要绕开：

1. **任何改写章节之后必须调 `pipeline.renumber_scenes(story)`。**
   `writer.rewrite_for_length` 产出的分镜是「每章从 0 开始」编号的，
   不重编就会出现 seg_000 目录互相覆盖、音频撞名、时间轴错位。
   注意 `fit_length`（时长自适应）必须在**合成之前**重编。
2. **兜底音色必须显式告警。** MiniMax 欠费/限流时会静默回退 edge-tts，
   成片配音会变成另一个人。`synthesize_all` 汇总告警、`probe-tts` 直接拦住并返回退出码 5。
3. **改写反馈只给本章的问题**（`pipeline.chapter_feedback`）。
   拿全量 issues 当反馈会把别章的问题发过去，改写方向被带偏。
4. **`_脚本.md` 是数据源之一**：`tts_preview.py` 靠 `**[分镜 N]**` 行抽口播正文，
   改稿件格式时别破坏这个标记。
5. **ffmpeg 一律以片段目录为 cwd、只用相对文件名**，规避 Windows 路径转义；
   concat 清单里要写绝对路径（片段在子目录里）。
6. **配图判据是「下得动」不是「搜得到」**：改图源后必须
   `run.bat probe-images --download` 实测下载速度。
7. 音色克隆重名/参数坑见 `命令速查.md` 的「音色克隆」一节
   （`file_id` 必须传整数；`get_voice` 列表看不到克隆音色，要靠在声才算成功）。
8. **配图风格后缀必须按分镜类型分派，不要合成一套。**
   器物向后缀里「整幅画面只有器物本身」这种约束会把**叙事分镜**也压成静物小品
   （第 7 期实测：要找坊墙坊门给了干裂土地、要找巡夜兵丁给了灯笼、
   要找衙门审案给了**一把西式法槌**）。分派逻辑在 `images.classify_scene_kinds`，
   规则兜底 + 一次 LLM 覆盖。两条后缀都**不许**出现「工笔/绢本」（会诱发伪书法
   题跋 + 红印章）和「摄影」（会诱发图库水印），这个坑踩过两轮。
9. **封面/画面的位置类改动，必须用像素量，不许用视觉模型估。**
   做法：按 `media.build_cover` 的同一路径只重建背景层（配图裁切→压暗→模糊，
   不画字），和成品做 `ImageChops.difference`，差出来的就是程序画上去的元素，
   按行统计能直接算出每个文字块的确切 y 区间。
   为什么立这条：上一轮靠视觉模型目测，把「标语距离底 11.6%」判成了「约 10%，安全」，
   结论正好相反。同理，标题折行这类回归也要靠断言钉住（`media.cover_title_fit`），
   在图上看不出「怎/样」和「怎样」的区别。

---

## 6. 已知的机制性局限（不要试图「优化」掉）

- **自动改写不收敛**：把复检未解决项反复喂回去改，会在
  「编造数字 → 算术错误 → 重复句/跨章指代断裂」之间来回震荡（实测 6→2→5→5 条）。
  根因是 `rewrite_for_length` 整章重写，会打乱跨章钩子句和已改好的句子。
  项目「只改一轮 + 把清单交人工核对」是**有意为之**，不是偷懒。
- **审校层已到顶**：定稿后再审，报的多是措辞问题（它自己在理由里写「表述基本正确」
  却仍判必修）。`severity` 只有 high/low，缺「这不是史实错误」的出口。
- **时长主要靠 `story.chapters` 和 `seconds_per_chapter`**，
  `target_total_seconds` 只用来算比例和打日志。

---

## 7. 不要动的东西

- **不要删 `data/` 下的产物、调试脚本、旧版成片**（用户明确要求：保留调试脚本和旧产物）。
- **不要 push 任何远端**，本项目没有远端。
- 不要为了「干净」批量重命名或移动已有文件；新增文件可以，改动现有文件名要先问。
