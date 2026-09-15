"""出素材需求清单：脚本 → 「要去剪什么片段」的工作单。

这是「脚本先行」工作流里的那一环（顺序不能颠倒）：

    run.bat plan -t "题材"            ← ① 先出脚本（还没有配音、配图、成片）
    python scripts\\make_needs.py      ← ② 出需求清单（本工具）
    （你按清单去剪素材：单段 ≤10s、无音轨）
    python scripts\\import_clip.py …   ← ③ 导入并绑到槽位
    python scripts\\rerender.py        ← ④ 渲染

为什么清单必须排在剪素材**之前**：剪素材是最慢的人工环节。先剪后配会剪一堆用不上的，
按单剪才能一次剪对。

本工具能直接跑在 plan 产物上 —— 那时还没有语音，分镜时长按字数估
（换算值取 config 里实测校准过的 story.chars_per_second，不另拍一个数）。

用法：
    python scripts\\make_needs.py                        # 取 data/output 下最新的 metadata
    python scripts\\make_needs.py --metadata <路径>       # 指定某一期
    python scripts\\make_needs.py --no-llm               # 不调 LLM（没人物/标注词，只剩规则兜底）
    python scripts\\make_needs.py --print                # 只打到屏幕上，不写文件

产物：
    data/needs/<日期>_<题材>_素材需求.md     ← 照着这份剪
    data/needs/<日期>_<题材>_素材需求.json   ← 渲染时装配用
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from hsg import clips as clips_mod            # noqa: E402
from hsg import needs as needs_mod            # noqa: E402
from hsg.config import ApiKeys, ensure_dirs, load_config  # noqa: E402
from rerender import load_story               # noqa: E402

log = logging.getLogger("hsg.make_needs")


def pick_latest(cfg) -> Path | None:
    """取 data/output 下最新的 metadata（plan 产物也算 —— 脚本也能出清单）。"""
    out_dir = cfg.paths.get_path("output_dir")
    cands = sorted(out_dir.glob("*_metadata.json"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    return cands[0] if cands else None


def main() -> int:
    ap = argparse.ArgumentParser(description="出素材需求清单（剪素材的工作单）")
    ap.add_argument("--metadata", help="metadata.json 路径（默认取 data/output 下最新的）")
    ap.add_argument("--no-llm", action="store_true",
                    help="不调 LLM：只出规则兜底的清单（没有人物、没有标注词建议）")
    ap.add_argument("--index", help="素材索引路径（默认取 clips.index 配置）")
    ap.add_argument("--print", dest="print_only", action="store_true",
                    help="只打到屏幕上，不写文件")
    a = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    cfg = load_config()
    ensure_dirs(cfg)

    meta = Path(a.metadata) if a.metadata else pick_latest(cfg)
    if meta is None or not meta.exists():
        log.error("找不到 metadata：%s", meta or "data/output 下没有 *_metadata.json")
        return 1

    story, raw = load_story(meta, cfg, cfg.paths.get_path("audio_dir"), log)

    # metadata 里记着当初每一镜的实测秒数 —— 比按字数估准得多，有就用它
    recorded: dict[int, float] = {}
    for c in raw.get("chapters") or []:
        for s in c.get("scenes") or []:
            try:
                recorded[int(s.get("index"))] = float(s.get("seconds") or 0)
            except (TypeError, ValueError):
                continue

    index = clips_mod.load_index(Path(a.index) if a.index else clips_mod.index_path(cfg))

    def _build(llm):
        return needs_mod.build_needs(story, cfg, llm, recorded=recorded)

    need_obj = None
    if a.no_llm:
        need_obj = _build(None)
    else:
        keys = ApiKeys.from_env()
        if not keys.deepseek:
            log.warning("没有 DeepSeek key → 退回规则兜底（不会有「人物」和「标注词」建议）")
            need_obj = _build(None)
        else:
            from hsg.llm import LLM
            with LLM(cfg, keys) as llm:
                need_obj = _build(llm)

    src = need_obj.get("duration_source") or {}
    log.info("分镜时长来源：实测音频 %d 个 / metadata 记录 %d 个 / 按字数估 %d 个"
             "（%.2f 字/秒）→ 旁白共 %.0f 秒",
             src.get("audio", 0), src.get("metadata", 0), src.get("estimated", 0),
             float(cfg.story.get("chars_per_second", 4.6)),
             float(need_obj.get("narration_seconds") or 0))
    if src.get("metadata") or src.get("audio"):
        log.info("  （这一期是已执行过的，时长取的是当初实测值，不是估算）")
    else:
        log.info("  （plan 产物：还没配音，槽位时长是按字数估的 —— 真正剪之前跑一次 TTS 会更准）")

    if a.print_only:
        sys.stdout.write(needs_mod.to_markdown(need_obj, cfg, index))
        return 0

    md, js = needs_mod.save(need_obj, cfg, index)
    cov = needs_mod.coverage(need_obj, index)

    print()
    print("=" * 62)
    print(f"需求清单：《{need_obj.get('title') or need_obj.get('topic')}》")
    print(f"  槽位 {cov['slots_total']} 个，铺满画面需素材 {cov['need_seconds']:.0f} 秒")
    print(f"  其中真要剪的约 {cov['fresh_seconds']:.0f} 秒，"
          f"{len(cov['reuse'])} 个槽位复用本场素材")
    print(f"  素材库现有 {len(index.get('clips') or [])} 条"
          f"（{cov['have_seconds']:.0f} 秒）")
    print(f"  已绑定 {len(cov['bound'])}　有候选 {len(cov['suggested'])}　"
          f"还缺 {len(cov['missing'])}（关键位置 {len(cov['missing_must'])}）")
    print()
    print("  工作单：" + str(md))
    print("  机器用：" + str(js))
    if cov["missing_must"]:
        print()
        print("  先剪这几条（关键位置，找不到素材就只能用 AI 生成顶）：")
        for slot in cov["missing_must"][:8]:
            row = next(r for r in need_obj["slots"] if r["slot"] == slot)
            print(f"    `{slot}`  {float(row['dur']):.1f}s  {row['need']}")
    print("=" * 62)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
