"""配图复核（agent / 人工在环）。

为什么是这个设计：项目文档里写着「关键词检索没法保证年代准确，要真正解决只有两条路：
① 限制图源 ② 接视觉模型复核（需要视觉模型 key）」——实测本机**没有任何视觉模型 key**
（只有 DeepSeek 文本 + MiniMax TTS），所以做不成「程序自动调模型看图」。

改成两步的在环流程，零 API 成本，效果不打折：

    # 1. 生成拼版图：每格一张缩略图 + 编号 + 该分镜的检索词（判断离题要有依据）
    python scripts/review_images.py --sheet

    # 2. 看图的人（或 agent）把裁决写进 verdicts.json，然后只重取被否掉的那几张
    python scripts/review_images.py --template          # 生成空白裁决文件模板
    python scripts/review_images.py --apply data\\images\\_review_verdicts.json

裁决字段：verdict = ok | reject；reject 时必须给 reason 和 expect（期望什么画面），
重取时会把 expect 当成检索词之一 —— 只说「不对」不给方向，重取还是同一张。

拼版图在 data/images/_review_sheet.jpg，裁决文件在 data/images/_review_verdicts.json。
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from hsg import images as images_mod            # noqa: E402
from hsg.config import ensure_dirs, load_config  # noqa: E402
from hsg.media import _font, _wrap_cjk          # noqa: E402  （复用项目的字体与折行工具）

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)-7s %(name)-12s %(message)s",
                    datefmt="%H:%M:%S", stream=sys.stdout)
for n in ("httpx", "httpcore", "PIL"):
    logging.getLogger(n).setLevel(logging.WARNING)
log = logging.getLogger("hsg.review")

CELL_W, CELL_H = 380, 330     # 每格：图 + 两行说明


# ---------------------------------------------------------------- 载入当期
def load_episode(cfg, meta_path: Path | None) -> tuple[dict, Path]:
    out_dir = cfg.paths.get_path("output_dir")
    if meta_path is None:
        cands = sorted(out_dir.glob("*_metadata.json"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
        for p in cands:
            d = json.loads(p.read_text(encoding="utf-8"))
            if not d.get("plan_only"):
                meta_path = p
                break
    if meta_path is None or not meta_path.exists():
        raise SystemExit("找不到可复核的 metadata.json（plan 产物没有配图记录）")
    return json.loads(meta_path.read_text(encoding="utf-8")), meta_path


def scene_rows(meta: dict) -> list[dict]:
    """把 metadata 里的分镜摊平成列表，带上检索词（复核时要看「本来要找什么」）。"""
    rows: list[dict] = []
    for c in meta.get("chapters") or []:
        qs = [str(q) for q in (c.get("image_queries") or [])]
        for s in c.get("scenes") or []:
            rows.append({
                "index": int(s.get("index") or 0),
                "query": str(s.get("image_query") or ""),
                "chapter": int(c.get("index") or 0),
                "chapter_heading": str(c.get("heading") or ""),
                "chapter_queries": qs,
                "image": str(s.get("image") or ""),
                "credit": str(s.get("image_credit") or ""),
                "license": str(s.get("image_license") or ""),
                "text": str(s.get("text") or ""),
            })
    return rows


# ---------------------------------------------------------------- 拼版图
def build_sheet_impl(rows: list[dict], img_dir: Path, out: Path, cfg) -> Path:
    from PIL import Image, ImageDraw

    items = [r for r in rows if (img_dir / f"scene_{r['index']:03d}.jpg").exists()]
    if not items:
        raise SystemExit(f"没找到任何配图（{img_dir}/scene_XXX.jpg）")
    cols = 6 if len(items) > 12 else 4
    rows_n = (len(items) + cols - 1) // cols
    pad = 8
    sheet = Image.new("RGB", (cols * (CELL_W + pad) + pad, rows_n * (CELL_H + pad) + pad),
                      (18, 20, 24))
    draw = ImageDraw.Draw(sheet)
    f_idx = _font(30, bold=True)
    f_txt = _font(19)

    for i, r in enumerate(items):
        cx = pad + (i % cols) * (CELL_W + pad)
        cy = pad + (i // cols) * (CELL_H + pad)
        # 缩略图：contain 进 (CELL_W, CELL_H-92) 的区域，不裁切（裁切会看不出离题）
        try:
            with Image.open(img_dir / f"scene_{r['index']:03d}.jpg") as im:
                im = im.convert("RGB")
                box_w, box_h = CELL_W, CELL_H - 92
                scale = min(box_w / im.width, box_h / im.height)
                th = im.resize((max(1, int(im.width * scale)), max(1, int(im.height * scale))),
                               Image.LANCZOS)
                sheet.paste(th, (cx + (box_w - th.width) // 2, cy + (box_h - th.height) // 2))
        except Exception as exc:  # noqa: BLE001
            log.warning("分镜 %d 缩略图失败：%s", r["index"], exc)
        # 编号（左上角，压在图上）+ 检索词两行
        label = f"{r['index']}"
        draw.text((cx + 8, cy + 4), label, font=f_idx, fill=(255, 214, 82),
                  stroke_width=3, stroke_fill=(0, 0, 0))
        ty = cy + CELL_H - 86
        q = f"找：{r['query']}" if r["query"] else "找：（无检索词）"
        for ln in _wrap_cjk(q, f_txt, CELL_W)[:2]:
            draw.text((cx, ty), ln, font=f_txt, fill=(210, 216, 226))
            ty += 26
        cred = (r["credit"] or r["license"] or "⚠️版权未明").split("｜")[-1][:34]
        draw.text((cx, ty), cred, font=_font(16), fill=(150, 156, 168))

    out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out, "JPEG", quality=90)
    log.info("拼版图：%s（%d 张，%d 列 × %d 行）", out, len(items), cols, rows_n)
    log.info("  下一句：让会看图的模型/人看这张图，把裁决写进 %s",
             out.parent / "_review_verdicts.json")
    return out


# ---------------------------------------------------------------- 裁决文件
def write_template(rows: list[dict], img_dir: Path, out: Path, ep: str) -> Path:
    data = {
        "episode": ep,
        "howto": "verdict: ok | reject。reject 必须给 reason（错在哪）和 expect（应该是什么画面），"
                 "expect 会被当成重取的检索词之一。改完跑 --apply。",
        "verdicts": [{"scene": r["index"], "verdict": "", "reason": "", "expect": "",
                      "query": r["query"]} for r in rows
                     if (img_dir / f"scene_{r['index']:03d}.jpg").exists()],
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("裁决模板：%s（%d 条待填）", out, len(data["verdicts"]))
    return out


def apply_verdicts(cfg, meta: dict, meta_path: Path, rows: list[dict], img_dir: Path,
                   verdict_file: Path) -> int:
    data = json.loads(verdict_file.read_text(encoding="utf-8"))
    by_idx = {r["index"]: r for r in rows}
    rejects = [v for v in (data.get("verdicts") or [])
               if str(v.get("verdict") or "").strip().lower() == "reject"]
    if not rejects:
        log.info("裁决里没有 reject，无需重取。")
        return 0

    # 去重表用「保留下来的图」的哈希播种，避免重取又选到已用的图
    used: list[int] = []
    reject_idx = {int(v.get("scene") or 0) for v in rejects}
    for r in rows:
        if r["index"] in reject_idx:
            continue
        p = img_dir / f"scene_{r['index']:03d}.jpg"
        if p.exists():
            h = images_mod._avg_hash(p)
            if h:
                used.append(h)

    period = ((int(meta.get("period_start") or 0), int(meta.get("period_end") or 0))
              if meta.get("period_start") and meta.get("period_end") else None)
    changed = 0
    for v in rejects:
        idx = int(v.get("scene") or 0)
        r = by_idx.get(idx)
        if r is None:
            log.warning("裁决里的分镜 %s 不在本期里，跳过", idx)
            continue
        expect = str(v.get("expect") or "").strip()
        reason = str(v.get("reason") or "").strip()
        queries = [q for q in ([expect] + [r["query"]] + r["chapter_queries"]) if q]
        log.info("分镜 %d 重取：原因「%s」→ 期望「%s」", idx, reason or "(未填)", expect or "(未填)")
        p, credit, src, attempts = images_mod.fetch_for_scene(
            idx, queries, cfg, img_dir, used, queries_en=[], period=period)
        if not p:
            log.warning("  分镜 %d 重取没找到图 —— 原图保留", idx)
            continue
        pick = next((a for a in reversed(attempts) if a.get("picked")), {}) or {}
        log.info("  新图：%s｜%s｜%s", p.name, src, (credit or "⚠️版权未明")[:70])
        # 同步更新 metadata，否则记录里写的还是旧图（记录必须跟文件一致）
        for c in meta.get("chapters") or []:
            for s in c.get("scenes") or []:
                if int(s.get("index") or 0) == idx:
                    s["image"] = p.name
                    s["image_source"] = src
                    s["image_credit"] = credit
                    s["image_license"] = str(pick.get("license") or "")
        changed += 1

    if changed:
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        log.info("metadata 已同步更新：%s", meta_path.name)
        log.info("重取了 %d 张。下一步重渲染即可用上新图：", changed)
        log.info("  python scripts\\rerender.py --voice %s --speed %s --no-images -o portrait",
                 cfg.tts[str(cfg.tts.provider)].get("voice_id"),
                 cfg.tts[str(cfg.tts.provider)].get("speed"))
    return changed


# ---------------------------------------------------------------- 入口
def main() -> int:
    ap = argparse.ArgumentParser(description="配图复核（拼版 → 裁决 → 只重取被否的）")
    ap.add_argument("--metadata", help="指定的 metadata.json（默认取最新的非 plan 产物）")
    ap.add_argument("--sheet", action="store_true", help="生成拼版图")
    ap.add_argument("--template", action="store_true", help="生成裁决文件模板")
    ap.add_argument("--apply", metavar="VERDICTS_JSON", help="按裁决重取被否的图")
    args = ap.parse_args()

    cfg = load_config()
    ensure_dirs(cfg)
    img_dir = cfg.paths.get_path("image_dir")
    meta, meta_path = load_episode(cfg, Path(args.metadata) if args.metadata else None)
    rows = scene_rows(meta)
    log.info("本期：《%s》%d 个分镜（%s）", meta.get("title"), len(rows), meta_path.name)

    if args.template:
        return 0 if write_template(rows, img_dir, img_dir / "_review_verdicts.json",
                                   str(meta.get("title") or "")) else 1
    if args.apply:
        apply_verdicts(cfg, meta, meta_path, rows, img_dir, Path(args.apply))
        return 0
    build_sheet_impl(rows, img_dir, img_dir / "_review_sheet.jpg", cfg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
