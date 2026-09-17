"""四格漫画素材（用户 2026-09-17 的点子）。

把「手工抓影视切片」这条路线换成「按分镜生成四格漫画」：
    ① 扩写（LLM 一次调用）：旁白 → 四格各画什么（同一场戏的四拍：起 / 承 / 转 / 合）
    ② 拼提示词（代码）：四格指令 + 画风 + 禁文字负面清单（复用配图踩出来的经验）
    ③ 生成（MiniMax image-01）：**一次出一整张 2x2**，不分开生成四次
    ④ 渲染（shotvideo.encode_comic_shot）：一张 2x2 切四格，按镜头时长轮流展示

为什么四格必须在**同一张图**里：四格漫画的灵魂是「格与格之间人物长得一样」。
    分开生成四次，人物/服装/光线必然飘（实测配图那套一次一张，同一期 18 张里
    同一个人物就换过脸），而一张图一次成稿天然共享同一套设定。

为什么扩写要交给 LLM、拼提示词要留给代码（沿用项目一贯的分工）：
    · 四格怎么分拍、画面里有什么 —— 创作判断，归 LLM；
    · 格数/画幅/负面清单/长度截断 —— 确定性的事，归代码，且必须可测。
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from .config import Config
from .llm import strip_think

log = logging.getLogger("hsg.comic")

PANELS = 4
LAYOUTS = {"2x2": (2, 2), "1x4": (1, 4), "4x1": (4, 1)}

# 扩写不出来时的兜底画风（配置里 comic.style 优先）
_STYLE_DEFAULT = ("中国古代连环画风格（小人书），白描线稿加淡彩，人物比例写实，"
                  "衣冠器物考据，中景与近景交替")
# 负面清单：漫画最容易长出对白气泡和拟声词，必须点名禁掉
_NEGATIVE_DEFAULT = ("整幅画面不要任何文字、对白气泡、拟声词、印章、署名、水印、边框花边，"
                     "不要仿照书画题跋的排版，不要分镜编号")


# ------------------------------------------------------------------ 扩写
_EXPAND_SYSTEM = """你是历史解说视频的分镜画师。给你一段旁白（还有章节标题、年代），
你要把这段旁白扩写成**同一场戏的四拍**，用来生成一张四格漫画。

硬性要求：
1. 四格必须是**同一场戏的四个瞬间**（起 / 承 / 转 / 合），不是四张无关的图：
   第 1 格交代人、地、时；第 2、3 格推进这件事；第 4 格给出结果或情绪落点。
2. 每格 25-45 个汉字，必须写**看得见的东西**：谁的什么动作、穿什么、手里拿什么、
   环境里有什么器物与建筑细节、光线与时间。不要写"气氛紧张"这类看不见的形容。
3. 四格里同一人物必须用**同一套**外貌与服装描述（年龄、脸型、胡须、冠帽、服色），
   在 continuity 字段里把那套设定写出来，四格描述里也都要复述关键几项。
4. 时代不能错：只出现该年代已有的器物、服饰、建筑。不确定的细节就别写。
5. 画面里**不许出现任何文字**（榜文、匾额、书名、印章、对白气泡、拟声词都不行）。

只输出 JSON：
{"panels": ["第1格…", "第2格…", "第3格…", "第4格…"],
 "continuity": "人物与场景的统一设定（一句话，写清外貌/服装/环境主色）",
 "style": "画风建议（一句话）"}"""


def expand(scene_row: dict, story, cfg: Config, llm) -> dict:
    """把一条旁白扩写成四格。LLM 不可用/失败时退回**机械四拍**（不能让流程断）。

    scene_row: {"scene": int, "narration": str, "caption": str, "heading": str}
    """
    narration = str(scene_row.get("narration") or "").strip()
    caption = str(scene_row.get("caption") or "").strip()
    heading = str(scene_row.get("heading") or "").strip()
    period = str(getattr(story, "period", "") or "")
    out = {"panels": [], "continuity": "", "style": "", "expanded": False}
    if bool(cfg.comic.get("expand_prompt", True)) and llm is not None and narration:
        ask = (f"年代：{period or '（未给）'}\n章节：{heading or '（无）'}\n"
               f"旁白：{narration}\n"
               + (f"画面图注：{caption}\n" if caption else ""))
        try:
            raw = llm.chat_json(system=_EXPAND_SYSTEM, user=ask,
                                temperature=float(cfg.llm.get("outline_temperature", 0.6)),
                                max_tokens=1200)
            panels = [str(x).strip() for x in (raw.get("panels") or []) if str(x).strip()]
            if len(panels) >= PANELS:
                out["panels"] = panels[:PANELS]
                out["continuity"] = str(raw.get("continuity") or "").strip()
                out["style"] = str(raw.get("style") or "").strip()
                out["expanded"] = True
                return out
            log.warning("分镜 %s 扩写只回了 %d 格（要 %d 格），退回机械四拍",
                        scene_row.get("scene"), len(panels), PANELS)
        except Exception as exc:  # noqa: BLE001
            log.warning("分镜 %s 扩写失败（退回机械四拍）：%s", scene_row.get("scene"), exc)
    return {**out, "panels": mechanical_panels(narration), "style": ""}


def mechanical_panels(narration: str) -> list[str]:
    """兜底四拍：把旁白按标点切成不多于四段，各配一句「画面要有什么」。

    它明显不如 LLM 扩写（拿不到"同一套人物设定"），但**保证流程不断**，
    而且给出来的东西是可读的——出问题时一眼能看出是扩写没生效。
    """
    import re
    parts = [p.strip() for p in re.split(r"[。！？；?!;]", narration or "") if p.strip()]
    if not parts:
        return [""] * PANELS
    if len(parts) <= PANELS:                    # 太短就每格一句，剩下的留空
        parts = parts + [""] * (PANELS - len(parts))
    else:                                       # 太长就均分成四份（保持顺序）
        step = len(parts) / PANELS
        parts = ["；".join(parts[int(i * step):int((i + 1) * step)]) for i in range(PANELS)]
    return [f"第{i + 1}格：{p}" if p else f"第{i + 1}格：（旁白没给到这一段，按前后格自然延续）"
            for i, p in enumerate(parts)]


# ------------------------------------------------------------------ 拼提示词
def build_prompt(expanded: dict, cfg: Config) -> str:
    """四格指令 + 画风 + 负面清单，截到接口上限 1500 字符。

    注意用词（配图那轮实测出来的坑，别随手改）：
      · 「工笔 / 绢本」会诱发模型模仿书画题跋（伪题字 + 红印章）；
      · 「摄影 / 写实照片」会诱发图库式水印；
      所以画风用**连环画 / 白描 / 淡彩**这种中性描述，并保留负面清单。
    """
    rows, cols = LAYOUTS.get(str(cfg.comic.get("layout") or "2x2"), (2, 2))
    style = str(cfg.comic.get("style") or "").strip() or str(expanded.get("style") or "").strip() \
        or _STYLE_DEFAULT
    negative = str(cfg.comic.get("negative") or "").strip() or _NEGATIVE_DEFAULT
    head = (f"中国古代历史题材的四格连环画，整幅为 {rows} 行 × {cols} 列的四个格子，"
            f"格子之间用细直线分隔，四格画的是同一场戏的四个瞬间。")
    cont = str(expanded.get("continuity") or "").strip()
    body = "".join(f"{p}。" for p in (expanded.get("panels") or []) if str(p).strip())
    tail = f"人物与场景统一设定：{cont}。" if cont else ""
    full = f"{head}{tail}{body}{style}。{negative}。"
    return full.replace("。。", "。")[:1500]


# ------------------------------------------------------------------ 生成
def comic_path(cfg: Config, name: str) -> Path:
    root = Path(str(cfg.comic.get("dir") or "data/comics"))
    return root / f"{name}.jpg"


def generate(name: str, prompt: str, cfg: Config, *, force: bool = False) -> tuple[Path | None, dict]:
    """用 MiniMax image-01 生成一张四格漫画。返回 (路径, 记录)。

    已有同名图就**直接复用**（生成一张要 25 秒左右，且是花钱的）——
    想重出加 --force 或先删文件。
    """
    import httpx

    from .config import env_get
    from .images import _prepare

    dest = comic_path(cfg, name)
    rec: dict = {"name": name, "prompt": prompt}
    if dest.exists() and not force and dest.stat().st_size > 5000:
        rec.update({"reused": True, "file": dest.name})
        return dest, rec
    if not bool(cfg.comic.get("enabled", True)):
        log.info("comic.enabled 关着（渲染不会走漫画路线）—— 但你是显式调用，照常生成")
    key = env_get("MINIMAX_API_KEY")
    if not key:
        rec["error"] = "取不到 MINIMAX_API_KEY"
        log.warning("漫画生成：%s", rec["error"])
        return None, rec

    base = str(cfg.tts.minimax.base_url).rstrip("/")
    payload = {
        "model": str(cfg.comic.get("model") or "image-01"),
        "prompt": prompt[:1500],
        "aspect_ratio": str(cfg.comic.get("aspect") or "1:1"),
        "response_format": "url",
        "n": 1,
        "prompt_optimizer": True,
        "aigc_watermark": bool(cfg.images.get("generate_aigc_watermark", False)),
    }
    rec.update({"model": payload["model"], "aspect": payload["aspect_ratio"]})
    t0 = time.monotonic()
    try:
        with httpx.Client(timeout=float(cfg.comic.get("timeout", 300)),
                          follow_redirects=True) as c:
            r = c.post(f"{base}/image_generation",
                       headers={"Authorization": f"Bearer {key}",
                                "Content-Type": "application/json"}, json=payload)
            data = r.json()
        code = (data.get("base_resp") or {}).get("status_code")
        if code not in (0, None):
            rec["error"] = f"{code} {(data.get('base_resp') or {}).get('status_msg')}"
            log.warning("漫画 %s 生成失败：%s", name, rec["error"])
            return None, rec
        d = data.get("data") or {}
        url = (d.get("image_urls") or [None])[0]
        dest.parent.mkdir(parents=True, exist_ok=True)
        raw = dest.parent / f"_{name}_raw.png"
        with httpx.Client(timeout=300.0, follow_redirects=True) as c:
            if url:
                got = c.get(url, headers={"User-Agent": "Mozilla/5.0"})
                got.raise_for_status()
                raw.write_bytes(got.content)
            elif d.get("image_base64"):
                import base64
                raw.write_bytes(base64.b64decode(d["image_base64"][0]))
            else:
                rec["error"] = "接口没返回图片数据"
                return None, rec
        final = _prepare(raw, dest)
        raw.unlink(missing_ok=True)
        rec.update({"file": final.name, "seconds": round(time.monotonic() - t0, 1),
                    "license": "AI 生成（无第三方版权）"})
        log.info("漫画 %s：%s（%s／%.1fs）", name, final.name, payload["model"],
                 rec["seconds"])
        return final, rec
    except Exception as exc:  # noqa: BLE001
        rec["error"] = str(exc)
        log.warning("漫画 %s 生成异常：%s", name, exc)
        return None, rec


# ------------------------------------------------------------------ 一张工作单
def split_panels(img_path: Path, layout: str = "2x2") -> list:
    """把一整张四格切成四张图（渲染时一格一格上屏要用）。"""
    from PIL import Image
    rows, cols = LAYOUTS.get(str(layout or "2x2"), (2, 2))
    im = Image.open(img_path).convert("RGB")
    w, h = im.size
    out = []
    for r in range(rows):
        for c in range(cols):
            box = (int(w * c / cols), int(h * r / rows),
                   int(w * (c + 1) / cols), int(h * (r + 1) / rows))
            out.append(im.crop(box))
    return out


def sheet_for(cfg: Config, slot: str) -> Path | None:
    """按槽位找四格漫画图（渲染层用）。没出过图就返回 None —— 由调用方退回配图/静态。"""
    slot = str(slot or "").strip()
    if not slot:
        return None
    p = comic_path(cfg, slot)
    if p.exists() and p.stat().st_size > 5000:
        return p
    return None


def write_worklist(rows: list[dict], dest: Path, *, title: str = "") -> Path:
    """把扩写出来的提示词写成**人能读的清单**（用户要看的正是这一步的产物）。"""
    lines = [f"# 四格漫画提示词清单{f'：{title}' if title else ''}", "",
             "> 由 `run.bat comic` 生成。每格一段：**扩写后的画面描述**；",
             "> 下面是实际发给 image-01 的完整提示词（改它就能改画风）。", ""]
    for r in rows:
        lines += [f"## {r.get('name')}　（分镜 {r.get('scene')} · {r.get('heading') or ''}）",
                  f"- 旁白：{r.get('narration') or '—'}",
                  f"- 扩写：{'LLM 四拍' if r.get('expanded') else '**机械四拍兜底**（LLM 没生效）'}",
                  f"- 人物与场景统一设定：{r.get('continuity') or '—'}", ""]
        for i, p in enumerate(r.get("panels") or [], 1):
            lines.append(f"  {i}. {p}")
        lines += ["", "```text", str(r.get("prompt") or ""), "```", "",
                  f"- 出图：{r.get('file') or '（未生成）'}"
                  + (f"　错误：{r['error']}" if r.get("error") else ""), ""]
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("\n".join(lines), encoding="utf-8")
    return dest


def scene_rows_from_plan(plan: dict) -> list[dict]:
    """从 plan（stage 1 产物）里取出每个分镜的旁白/图注/章节标题，供扩写用。"""
    rows = []
    for ch in plan.get("chapters") or []:
        for s in ch.get("scenes") or []:
            rows.append({"scene": int(s.get("index") or 0),
                         "narration": str(s.get("text") or "").strip(),
                         "caption": str(s.get("caption") or "").strip(),
                         "heading": ch.get("heading") or ""})
    return rows
