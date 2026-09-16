"""导入影视切片素材：规范化 + 剥音轨 + 登记进素材库索引。

用法（在项目根目录跑）：

    # 导入一段切片（从 12:30 起取 8 秒）
    python scripts\\import_clip.py --file D:\\raw\\sanguo94_ep12.mp4 ^
        --id sg_caocao_01 --title "三国演义" --year 1994 ^
        --people 曹操 --era 东汉末 --topic 三国 ^
        --desc "横槊赋诗前的特写" --in 12:30 --out 12:38

    # 可选：绑定槽位（需求清单里的 s04_sh1 这种），一个片段可以绑多个槽位
    ... --slots s04_sh1,s11_sh2

    # 看库里有什么 / 体检
    python scripts\\import_clip.py --list
    python scripts\\import_clip.py --check

做三件事（每一件都有对应的坑）：
  ① **规范化**：统一到 config.clips.spec（默认 1920x1080@30），铺满不拉伸
  ② **剥音轨**：`-an`。不用原声（版权），且我们有自己的配音；
     入库前会**再验一次**，带音轨直接拒绝（不靠"我记得加了 -an"）
  ③ **登记**：写进 data/clips/index.json，记全来源字段（发布前要出出处清单）
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from hsg import clips  # noqa: E402
from hsg.config import load_config  # noqa: E402


def to_seconds(t: str) -> float:
    """把 "12:30" / "1:02:03" / "750" / "750.5" 都转成秒。"""
    s = str(t or "").strip()
    if not s:
        return 0.0
    if ":" in s:
        parts = [float(x) for x in s.split(":")]
        while len(parts) < 3:
            parts.insert(0, 0.0)
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    return float(s)


def split_tags(v: str) -> list[str]:
    return [x.strip() for x in str(v or "").replace("，", ",").split(",") if x.strip()]


def clip_path(cfg, rec: dict) -> Path:
    """一条素材的规范化产物的绝对路径（走 clips.clip_dir，别自己拼）。"""
    return (clips.clip_dir(cfg) / str(rec.get("file") or "")).resolve()


def do_import(a, cfg) -> int:
    src = Path(a.file).expanduser().resolve()
    if not src.exists():
        print(f"✗ 找不到素材文件：{src}")
        return 2
    idx_path = clips.index_path(cfg)
    idx = clips.load_index(idx_path)
    if any(str(c.get("id")) == a.id for c in idx.get("clips", [])):
        if not a.force:
            print(f"✗ id 已存在：{a.id}（要覆盖请加 --force）")
            return 3
        clips.remove_clip(idx, a.id)
        print(f"· 已移除同 id 的旧记录（--force）")

    spec = cfg.clips.get("spec") or {}
    max_sec = float(cfg.clips.get("max_seconds", 10.0))
    dest = idx_path.parent / "norm" / f"{a.id}.mp4"
    print(f"导入 {src.name} → {dest.relative_to(ROOT)}")
    ct, cb = float(getattr(a, "crop_top", 0.0) or 0.0), float(getattr(a, "crop_bottom", 0.0) or 0.0)
    cr = float(getattr(a, "crop_right", 0.0) or 0.0)
    tc = (f"　取源片 {a.in_ or '0:00'}–{a.out or '末'}" if (a.in_ or a.out) else "　取源片全段")
    print(f"  目标规格 {spec.get('width')}x{spec.get('height')}@{spec.get('fps')}"
          f"　单段上限 {max_sec:.0f}s　去音轨 ✓{tc}"
          + (f"　裁上 {ct:.0%}/裁下 {cb:.0%}/裁右 {cr:.0%}" if (ct or cb or cr) else ""))
    try:
        got = clips.normalize_clip(src, dest, spec, max_sec,
                                  src_in=to_seconds(a.in_), src_out=to_seconds(a.out),
                                  preset=str(getattr(a, "preset", "") or ""),
                                  crop_top=float(getattr(a, "crop_top", 0.0) or 0.0),
                                  crop_bottom=float(getattr(a, "crop_bottom", 0.0) or 0.0),
                                  crop_left=float(getattr(a, "crop_left", 0.0) or 0.0),
                                  crop_right=float(getattr(a, "crop_right", 0.0) or 0.0))
    except Exception as exc:  # noqa: BLE001
        print(f"✗ 规范化失败：{exc}")
        return 4

    rec = {
        "id": a.id,
        "file": str(dest.relative_to(idx_path.parent)).replace("\\", "/"),
        "dur": round(float(got["duration"]), 2),
        "width": got.get("width"), "height": got.get("height"), "fps": got.get("fps"),
        "title": a.title or "", "year": a.year or "",
        "people": split_tags(a.people), "era": a.era or "",
        "topic": split_tags(a.topic),
        "slots": split_tags(a.slots),
        "desc": a.desc or "", "note": a.note or "",
        "src_file": str(src),
        # 举证用：用了源片的哪一段（这是「合理引用」能说清范围的关键）
        "src_in_sec": got.get("src_in"), "src_out_sec": got.get("src_out"),
        "crop_top": got.get("crop_top"), "crop_bottom": got.get("crop_bottom"),
        "crop_left": got.get("crop_left"), "crop_right": got.get("crop_right"),
        "rights": a.rights or (
            f"{a.title or '?'}（{a.year or '?'}）· 合理引用，单段≤{max_sec:.0f}s，已去原声"
            + (f"，取源片 {_tc(got.get('src_in'))}–{_tc(got.get('src_out'))}"
               if a.in_ or a.out else "")
            + (f"，已裁去底部 {cb:.0%}（源片硬字幕）" if cb else "")
            + (f"，已裁去顶部 {ct:.0%}（源片台标/水印）" if ct else "")),
    }
    if clips.add_clip(idx, rec) is None:
        print("✗ 登记失败（id 或 file 缺失）")
        return 5
    clips.save_index(idx_path, idx)
    print(f"✓ 已入库：{a.id}　{rec['dur']}s　"
          f"{rec['width']}x{rec['height']}　无音轨　人物={'/'.join(rec['people']) or '—'}"
          f"　槽位={'/'.join(rec['slots']) or '—'}")
    print(f"  索引：{idx_path.relative_to(ROOT)}　（现有 {len(idx['clips'])} 条）")
    print("\n下一步：把这一条填进素材需求清单对应的槽位，或直接靠标签检索使用。")
    return 0


def _tc(sec) -> str:
    """秒 → mm:ss（举证清单里给人看的时间码）。"""
    try:
        s = int(round(float(sec or 0)))
    except (TypeError, ValueError):
        return "?"
    return f"{s // 60:d}:{s % 60:02d}"


def do_list(cfg) -> int:
    idx_path = clips.index_path(cfg)
    idx = clips.load_index(idx_path)
    items = idx.get("clips", [])
    if not items:
        print(f"素材库是空的（{idx_path.relative_to(ROOT)}）")
        print("先剪好切片，再用 --file ... --id ... 导入。")
        return 0
    total = sum(float(c.get("dur") or 0) for c in items)
    print(f"素材库 {len(items)} 条，合计 {total:.1f}s\n")
    print(f"{'id':<20}{'时长':>6}  {'人物':<14}{'年代':<10}{'片源':<14}描述")
    for c in sorted(items, key=lambda x: str(x.get("id"))):
        print(f"{str(c.get('id'))[:19]:<20}{float(c.get('dur') or 0):>5.1f}s  "
              f"{'/'.join(c.get('people') or [])[:13]:<14}{str(c.get('era'))[:9]:<10}"
              f"{str(c.get('title'))[:13]:<14}{str(c.get('desc'))[:24]}")
    return 0


def do_remove(cfg, clip_id: str, keep_file: bool) -> int:
    """撤销一次导入（记录 + 规范化产物）。原始素材不动。"""
    idx_path = clips.index_path(cfg)
    idx = clips.load_index(idx_path)
    rec = next((c for c in idx.get("clips", []) if str(c.get("id")) == clip_id), None)
    if rec is None:
        print(f"✗ 素材库里没有 {clip_id}")
        return 3
    clips.remove_clip(idx, clip_id)
    clips.save_index(idx_path, idx)
    print(f"✓ 已从索引移除：{clip_id}（剩 {len(idx['clips'])} 条）")
    if not keep_file:
        p = clip_path(cfg, rec)
        if p.exists():
            p.unlink()
            print(f"· 规范化产物已删除：{p.relative_to(ROOT)}")
    return 0


def do_check(cfg) -> int:
    idx_path = clips.index_path(cfg)
    idx = clips.load_index(idx_path)
    items = idx.get("clips", [])
    print(f"体检 {len(items)} 条…\n")
    problems = 0
    for c in items:
        p = clip_path(cfg, c)
        if not p.exists():
            print(f"  ✗ {c.get('id')}：文件不在（{c.get('file')}）")
            problems += 1
            continue
        info = clips.probe(p)
        bad = []
        if info.get("has_audio"):
            bad.append("还带音轨")
        if float(c.get("dur") or 0) > float(cfg.clips.get("max_seconds", 10)) + 0.01:
            bad.append(f"时长 {c.get('dur')}s 超上限")
        if not str(c.get("title") or "").strip():
            bad.append("缺片源名（发布举证要用）")
        if bad:
            print(f"  ✗ {c.get('id')}：{'；'.join(bad)}")
            problems += 1
    print(f"\n{'✓ 全部合格' if not problems else f'共 {problems} 条有问题'}")
    return 1 if problems else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="导入影视切片素材（规范化 + 去音轨 + 登记）")
    ap.add_argument("--file", help="源文件（人工剪好的 mp4）")
    ap.add_argument("--id", help="素材 id，检索与槽位绑定的键，如 sg_caocao_01")
    ap.add_argument("--title", default="", help="片源名，如 三国演义")
    ap.add_argument("--year", default="", help="片源年份，如 1994")
    ap.add_argument("--people", default="", help="人物，逗号分隔，如 曹操,荀彧")
    ap.add_argument("--era", default="", help="年代，如 东汉末")
    ap.add_argument("--topic", default="", help="题材，逗号分隔，如 三国,官渡")
    ap.add_argument("--desc", default="", help="一句话描述，如 横槊赋诗前的特写")
    ap.add_argument("--note", default="", help="备注")
    ap.add_argument("--slots", default="", help="绑定的槽位，逗号分隔，如 s04_sh1,s11_sh2")
    ap.add_argument("--rights", default="", help="引用说明（留档用，可留空自动生成）")
    ap.add_argument("--in", dest="in_", default="", help="起点时间码，如 12:30")
    ap.add_argument("--out", default="", help="终点时间码，如 12:38")
    ap.add_argument("--force", action="store_true", help="同 id 覆盖")
    ap.add_argument("--list", action="store_true", help="列出素材库")
    ap.add_argument("--check", action="store_true", help="体检素材库")
    ap.add_argument("--remove", metavar="ID", help="撤销一次导入（删记录 + 规范化产物）")
    ap.add_argument("--keep-file", action="store_true", help="配合 --remove：保留规范化产物")
    ap.add_argument("--crop-bottom", dest="crop_bottom", type=float, default=0.0,
                    help="裁掉底部一条带（比例，0.14 = 裁掉 14%%）。用于去掉烧死在画面里的"
                         "对白字幕；裁完不出黑边（之后按 increase+crop 铺满画幅）")
    ap.add_argument("--crop-top", dest="crop_top", type=float, default=0.0,
                    help="裁掉顶部一条带（比例，0.06 = 6%%）。用于去掉台标/水印")
    ap.add_argument("--crop-right", dest="crop_right", type=float, default=0.0,
                    help="裁掉右侧一条带（比例）。用于去掉右侧竖排剧名水印")
    ap.add_argument("--crop-left", dest="crop_left", type=float, default=0.0,
                    help="裁掉左侧一条带（比例）")
    ap.add_argument("--preset", default="",
                    help="x264 preset（留空=x264 默认 medium；批量导入可用 ultrafast 提速）")
    a = ap.parse_args()
    cfg = load_config()
    if a.list:
        return do_list(cfg)
    if a.check:
        return do_check(cfg)
    if a.remove:
        return do_remove(cfg, a.remove, a.keep_file)
    if not a.file or not a.id:
        ap.print_help()
        print("\n至少要给 --file 和 --id（或 --list / --check）")
        return 2
    return do_import(a, cfg)


if __name__ == "__main__":
    sys.exit(main())
