"""素材库：影视切片的索引、检索、规范化。

为什么要有索引，而不是靠文件名：
  ① 检索条件是**多维**的（人物 + 年代 + 题材 + 场景描述），文件名塞不下；
  ② 版权留痕必须是结构化字段（片源 / 年份 / 引用说明），发布前要能出出处清单；
  ③ 同一个片段会被多个「槽位」复用（第 4 和第 11 分镜都用这个曹操特写），
     靠文件名表达不了这种关系。

槽位（slot）是什么：需求清单里的一条 = 分镜号 + 镜头序号，例如 s04_sh1。
素材导入时可选地绑定槽位；不绑定也能用（靠标签检索）。

合规硬线（config.clips）：
  · 单段 ≤ max_seconds（默认 10 秒）
  · 必须去音轨（不用原声）
  · 成片里切片总时长占比 ≤ max_share
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from . import video
from .config import Config

log = logging.getLogger("hsg.clips")

INDEX_VERSION = 1

# 导入时必填的字段（缺了会被 add_clip 拒掉）
REQUIRED = ("id", "file")


def empty_index() -> dict:
    return {"version": INDEX_VERSION, "clips": []}


def clip_dir(cfg: Config) -> Path:
    """素材库根目录（读 config.clips.dir，相对项目根解析）。"""
    p = Path(str(cfg.clips.get("dir") or "data/clips"))
    return p if p.is_absolute() else (cfg.paths.get_path("data_dir").parent / p)


def clip_file(cfg: Config, clip_id: str) -> Path | None:
    """按素材 id 取规范化产物的绝对路径（渲染时用）。找不到返回 None。

    路径只有一个来源：索引里的 `file` 字段（相对 index 所在目录）。
    别在渲染层再拼一遍路径 —— 那是「同一件事两个实现」，早晚跑偏。
    """
    idx = load_index(index_path(cfg))
    for c in idx.get("clips") or []:
        if str(c.get("id")) == str(clip_id):
            rel = str(c.get("file") or "").strip()
            if not rel:
                return None
            return (index_path(cfg).parent / rel).resolve()
    return None


def index_path(cfg: Config) -> Path:
    """索引文件位置（读 config.clips.index）。

    为什么要走配置：这两个键写在 config.yaml 里，如果代码自己拼路径，
    它们就成了「假开关」—— 改了配置不生效，而 audit_config 会把它标出来。
    """
    p = Path(str(cfg.clips.get("index") or "data/clips/index.json"))
    return p if p.is_absolute() else (cfg.paths.get_path("data_dir").parent / p)


def load_index(path: Path) -> dict:
    """读索引；文件不存在或坏了都返回空索引（不炸，让流水线继续）。"""
    if not path.exists():
        return empty_index()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        log.warning("素材索引读不动（%s），按空索引处理：%s", path.name, exc)
        return empty_index()
    if not isinstance(data, dict) or not isinstance(data.get("clips"), list):
        return empty_index()
    data.setdefault("version", INDEX_VERSION)
    return data


def save_index(path: Path, idx: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(idx, ensure_ascii=False, indent=1), encoding="utf-8")


def add_clip(idx: dict, rec: dict) -> dict | None:
    """登记一条素材。id 重复或必填字段缺失 → 返回 None（调用方自己报错）。

    重名是有意拦下的：id 是检索与槽位绑定的键，重了会让渲哪一条变得不确定。
    要覆盖请先 remove_clip。
    """
    if not all(str(rec.get(k) or "").strip() for k in REQUIRED):
        return None
    if any(str(c.get("id")) == str(rec["id"]) for c in idx.get("clips", [])):
        return None
    rec = dict(rec)
    for k in ("people", "topic"):
        if isinstance(rec.get(k), str):
            rec[k] = [s for s in (x.strip() for x in rec[k].replace("，", ",").split(",")) if s]
        rec.setdefault(k, [])
    idx.setdefault("clips", []).append(rec)
    return rec


def remove_clip(idx: dict, clip_id: str) -> bool:
    """删掉一条（用于重导入同一 id 之前）。"""
    before = len(idx.get("clips", []))
    idx["clips"] = [c for c in idx.get("clips", []) if str(c.get("id")) != str(clip_id)]
    return len(idx["clips"]) != before


def search(idx: dict, *, people=None, era: str = "", topic=None,
           slot: str = "", text: str = "") -> list[dict]:
    """按标签检索素材。

    规则：people / topic 是**任一命中**（OR），不同维度之间是 **AND**。
    空条件不参与过滤。era 用「包含」匹配（"东汉末" 能命中 "东汉末/三国"）。
    """
    want_people = {str(p) for p in (people or []) if str(p).strip()}
    want_topic = {str(t) for t in (topic or []) if str(t).strip()}
    out: list[dict] = []
    for c in idx.get("clips", []):
        if want_people and not (want_people & {str(x) for x in (c.get("people") or [])}):
            continue
        if want_topic and not (want_topic & {str(x) for x in (c.get("topic") or [])}):
            continue
        if era and str(era) not in str(c.get("era") or ""):
            continue
        if slot and str(slot) not in [str(s) for s in (c.get("slots") or [])]:
            continue
        if text:
            blob = " ".join(str(c.get(k) or "") for k in
                            ("desc", "note", "title", "file", "id"))
            if str(text) not in blob:
                continue
        out.append(c)
    return out


def probe(path: Path) -> dict:
    """读一条素材的规格：分辨率 / 帧率 / 时长 / **有没有音轨**。"""
    info = video.probe_streams(path)
    v = info.get("video") or {}
    return {"duration": info.get("duration", 0.0),
            "width": v.get("width"), "height": v.get("height"),
            "fps": v.get("fps"), "has_audio": bool(info.get("audio"))}


def normalize_clip(src: Path, dest: Path, spec: dict, max_seconds: float,
                   src_in: float = 0.0, src_out: float = 0.0) -> dict:
    """把人工剪好的片子规范化：统一规格 + **去音轨** + 限制时长。

    `-an` 是这一步的关键：不用原声（版权），而且我们有自己的配音。
    画面用 force_original_aspect_ratio=increase + crop 铺满，
    不拉伸变形（宁可裁掉边缘）。
    返回 {duration, path}；输入读不出来时抛 FFmpegError。
    """
    total = video.media_duration(src)
    if total <= 0:
        raise video.FFmpegError(f"读不出时长，素材可能损坏：{src.name}")
    start = max(0.0, float(src_in))
    avail = max(0.0, total - start)
    want = (float(src_out) - start) if float(src_out) > start else avail
    dur = min(want, avail, float(max_seconds))
    if dur < 1.0:
        raise video.FFmpegError(f"规范化后只剩 {dur:.2f}s（起点 {start:.2f}s / 总长 {total:.2f}s）")
    w, h, fps = int(spec.get("width", 1920)), int(spec.get("height", 1080)), int(spec.get("fps", 30))
    dest.parent.mkdir(parents=True, exist_ok=True)
    video.run_ffmpeg(
        ["-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", str(src), "-an",
         "-vf", (f"scale={w}:{h}:force_original_aspect_ratio=increase,"
                 f"crop={w}:{h},setsar=1,fps={fps}"),
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "18",
         "-movflags", "+faststart", str(dest)],
        cwd=dest.parent, desc="normalize_clip")
    got = probe(dest)
    if got.get("has_audio"):
        # 理论上 -an 之后不可能有音轨；真有就是 ffmpeg 参数被改坏了，必须拦下
        raise video.FFmpegError(f"规范化后仍带音轨，拒绝入库：{dest.name}")
    return {"duration": got.get("duration", dur), "path": dest,
            "width": got.get("width"), "height": got.get("height"), "fps": got.get("fps")}


def share_of(total_video: float, used: list[dict]) -> float:
    """算出「切片在成片里占的时长比例」（合规上限用）。"""
    if total_video <= 0:
        return 0.0
    return sum(float(u.get("dur") or 0.0) for u in used) / float(total_video)


def check_quota(cfg: Config, total_video: float, used: list[dict]) -> list[str]:
    """检查合规硬线，返回问题清单（空 = 通过）。"""
    problems: list[str] = []
    max_sec = float(cfg.clips.get("max_seconds", 10.0))
    for u in used:
        if float(u.get("dur") or 0.0) > max_sec + 0.01:
            problems.append(f"片段 {u.get('id')} 用了 {u.get('dur'):.1f}s，"
                            f"超过单段上限 {max_sec:.0f}s")
    share = share_of(total_video, used)
    limit = float(cfg.clips.get("max_share", 0.45))
    if share > limit + 0.001:
        problems.append(f"切片总时长占比 {share:.0%} 超过上限 {limit:.0%}")
    return problems
