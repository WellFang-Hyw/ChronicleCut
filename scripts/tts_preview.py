"""试听工具：把一段文稿用指定音色（可多个）合成成 mp3，方便 A/B 对比音色。

为什么要它：音色、语速这类「耳朵决定」的参数，用整条流水线试太贵太慢
（要跑 LLM + 大纲 + 校验 + 配图 + 渲染）。这里只取文稿里的口播正文，
按分镜逐段合成再拼成一条 mp3 —— 和正式流程同一套 TTS 代码路径、
同一套缓存 key，所以试听听到的就是成片里的声音。

用法：
    # 用当前 config 里的音色
    python scripts/tts_preview.py --script data/output/xxx_脚本.md

    # 多音色 A/B（克隆音色 vs 原音色）
    python scripts/tts_preview.py --script data/output/xxx_脚本.md \\
        --voice hsg_story_v2 --voice audiobook_male_1 --tag voice_ab

    # 不用文稿，直接给一句话
    python scripts/tts_preview.py --text "今天讲一段历史。" --voice hsg_story_v2

产物：data/preview/<tag>_<音色>.mp3（--no-concat 时给的是逐分镜片段）
"""

from __future__ import annotations

import argparse
import logging
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from hsg import tts as tts_mod            # noqa: E402
from hsg.config import load_config        # noqa: E402
from hsg.textfmt import sanitize_narration  # noqa: E402

log = logging.getLogger("hsg.preview")

_SCENE_RE = re.compile(r"^\*\*\[分镜\s*(\d+)\]\*\*\s*(.+?)\s*$", re.M)


def narration_from_script(path: Path, *, with_hook: bool = True) -> list[str]:
    """从生成的「_脚本.md」里抽出可朗读的正文。

    只取 **[分镜 N]** 那几行（这就是口播稿本身）；开篇 hook 可选。
    章节标题、要点、史实锚点、配图检索词、版权清单都不进音频。
    """
    txt = path.read_text(encoding="utf-8")
    parts: list[str] = []
    if with_hook:
        m = re.search(r"^##\s*开篇\s*$(.*?)(?=^##\s|\Z)", txt, re.M | re.S)
        if m:
            hook = " ".join(ln.strip() for ln in m.group(1).splitlines() if ln.strip())
            hook = re.sub(r"^>.*$", "", hook, flags=re.M).strip()
            if hook:
                parts.append(sanitize_narration(hook))
    for m in _SCENE_RE.finditer(txt):
        s = sanitize_narration(m.group(2))
        if s:
            parts.append(s)
    return parts


def concat_mp3(chunks: list[Path], out: Path) -> Path:
    """同参数 mp3 无损拼接（concat demuxer + -c copy）。

    清单里必须写**绝对路径**：cwd 是 out.parent，而片段通常在子目录里，
    只写文件名会让 ffmpeg 报 "Impossible to open ..."（实测踩过）。
    """
    if not chunks:
        raise ValueError("没有可拼接的片段")
    if len(chunks) == 1:
        out.write_bytes(chunks[0].read_bytes())
        return out
    lst = out.parent / "_preview_list.txt"
    lst.write_text(
        "".join(f"file '{c.resolve().as_posix()}'\n" for c in chunks), encoding="utf-8")
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
           "-f", "concat", "-safe", "0", "-i", lst.name, "-c", "copy", out.name]
    p = subprocess.run(cmd, cwd=str(out.parent), capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"拼接失败：{(p.stderr or '').strip()[-400:]}")
    lst.unlink(missing_ok=True)
    return out


def voices_for(cfg, args) -> list[str]:
    if args.voice:
        return list(dict.fromkeys(args.voice))
    return [str(cfg.tts.minimax.voice_id)]


def main() -> int:
    ap = argparse.ArgumentParser(description="用指定音色合成试听 mp3")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--script", help="稿件 md 路径（取里面的分镜正文）")
    src.add_argument("--text", help="直接给一段文本")
    ap.add_argument("--voice", action="append",
                    help="音色 ID，可给多次做 A/B；不给则用 config 里的")
    ap.add_argument("--speed", type=float, help="语速（默认取 config）")
    ap.add_argument("--tag", default="preview", help="产物文件名前缀")
    ap.add_argument("--announce", action="store_true",
                    help="每段开头先念一遍音色名（A/B 时不用看文件名）")
    ap.add_argument("--no-hook", action="store_true", help="不要开篇 hook")
    ap.add_argument("--no-concat", action="store_true", help="保留逐分镜片段，不拼成一条")
    ap.add_argument("--out-dir", default="data/preview")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)-12s %(message)s",
                        datefmt="%H:%M:%S", stream=sys.stdout)
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    cfg = load_config()
    if args.speed:
        cfg.tts["minimax"]["speed"] = float(args.speed)

    if args.script:
        p = Path(args.script)
        if not p.is_absolute():
            p = ROOT / p
        if not p.exists():
            print(f"✗ 找不到稿件：{p}")
            return 1
        chunks = narration_from_script(p, with_hook=not args.no_hook)
        src_label = p.name
    else:
        chunks = [sanitize_narration(args.text)]
        src_label = "（直接输入文本）"
    if not chunks:
        print("✗ 文稿里没抽到任何可朗读正文")
        return 1
    total_chars = sum(len(c) for c in chunks)
    print(f"文稿：{src_label}　{len(chunks)} 段 / {total_chars} 字")

    out_dir = ROOT / args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    speed_cfg = cfg.tts.minimax.get("speed")
    results: list[tuple[str, Path, float, float]] = []

    for v in voices_for(cfg, args):
        sub = cfg.tts["minimax"]
        sub["voice_id"] = v
        label = v
        seg_dir = out_dir / f"{args.tag}_{label}"
        seg_dir.mkdir(parents=True, exist_ok=True)
        print(f"\n=== 音色 {label}（语速 {sub.get('speed')}）===")
        pieces: list[Path] = []
        with tts_mod.TTS(cfg) as t:
            for i, text in enumerate(chunks, start=1):
                if args.announce and i == 1:
                    text = f"这是音色 {label}。{text}"
                tag = tts_mod.audio_tag(text, v, sub.get("speed"))
                out = seg_dir / f"{i:02d}_{tag}.mp3"
                try:
                    path, dur = t.synthesize(text, out)
                except Exception as exc:  # noqa: BLE001
                    print(f"  ✗ 第 {i} 段合成失败：{exc}")
                    continue
                pieces.append(path)
        if not pieces:
            print(f"  ✗ {label} 一段都没合成出来，跳过")
            continue
        if args.no_concat:
            final = pieces[0]
            total = sum(tts_mod.probe_duration(p) for p in pieces)
        else:
            final = concat_mp3(pieces, out_dir / f"{args.tag}_{label}.mp3")
            total = tts_mod.probe_duration(final)
        rate = total_chars / max(0.01, total)
        results.append((label, final, total, rate))
        print(f"  → {final}　{total:.1f}s = {total / 60:.2f} 分钟　"
              f"实测 {rate:.2f} 字/秒")

    print("\n" + "=" * 66)
    for label, path, total, rate in results:
        print(f"{label:24s} {total:7.1f}s  {rate:5.2f} 字/秒  {path}")
    if len(results) > 1:
        a = results[0]
        print(f"\n对比：{results[0][0]} {a[2]:.1f}s vs {results[1][0]} {results[1][2]:.1f}s"
              f"　（同一篇稿子，时长差反映语速差）")
    print("=" * 66)
    if results:
        print(f"参考：若要把这个音色定为项目默认，填 config.yaml → "
              f"tts.minimax.voice_id: \"{results[0][0]}\"")
        print(f"      并且换音色后必须重新校准 story.chars_per_second"
              f"（本次实测 {results[0][3]:.2f}）")
    return 0 if results else 1


if __name__ == "__main__":
    raise SystemExit(main())
