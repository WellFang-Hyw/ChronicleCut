"""从「已生成期」的脚本反算流水线真实字/秒 —— 0 API 成本。

用途：换音色或改语速后，校准 config.yaml 的 story.chars_per_second。

为什么不用 probe-tts 的值
------------------------
probe-tts 读的是一整篇连续范文，一次合成；而流水线是**按分镜**逐段合成的，
每段都带 TTS 的首尾静音，所以实际产出比连续范文慢一截。实测差异：

    克隆音色 hsg_story_v2 @1.1 ：范文 3.91 字/秒，流水线实际 3.45 字/秒
    系统音色 audiobook_male_1 @1.1：范文 4.77 字/秒，流水线实际 4.59 字/秒

用偏快的值写稿会偏长，容易顶到区间上限并多触发一轮自适应改写。

本工具的数据来源
----------------
`data/output/*_脚本.md` 里每个分镜都记着正文与**实测语音时长**，
所以拿已生成的那几期直接反算，比任何范文探针都准，而且不花一分钱 API。

用法
----
    python scripts/calib_rate.py              # 全部期
    python scripts/calib_rate.py --last 3     # 只看最近 3 期
    python scripts/calib_rate.py --voice audiobook_male_1   # 只统计某个音色

方法自校验：拿一个已知 chars_per_second 的期来复算，算得出来才信这套方法。
（注意：plan 产物只写稿、没有语音记录，时长为 0，必须排除。）
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "data" / "output"

SCENE_RE = re.compile(r"^\*\*\[分镜\s*(\d+)\]\*\*\s*(.*)$")
DUR_RE = re.compile(r"时长\s*([\d.]+)\s*s")


def measure(md: Path) -> tuple[int, float, list[tuple[int, int, float]]]:
    """从脚本 md 抽出 (总字数, 总秒数, [(分镜号, 字数, 秒数)])。"""
    rows: list[tuple[int, int, float]] = []
    pending: tuple[int, str] | None = None
    for ln in md.read_text(encoding="utf-8").splitlines():
        m = SCENE_RE.match(ln.strip())
        if m:
            pending = (int(m.group(1)), m.group(2).strip())
            continue
        d = DUR_RE.search(ln)
        if d and pending:
            rows.append((pending[0], len(pending[1].replace("`", "")), float(d.group(1))))
            pending = None
    return sum(r[1] for r in rows), sum(r[2] for r in rows), rows


def tts_spec_of(md: Path) -> tuple[str, str]:
    """读同期 metadata 的 tts_spec（音色/语速）。早期几期没记，返回「未知」。"""
    meta = md.with_name(md.name.replace("_脚本.md", "_metadata.json"))
    if not meta.exists():
        return "未知", "-"
    try:
        spec = (json.loads(meta.read_text(encoding="utf-8")) or {}).get("tts_spec") or {}
        return str(spec.get("voice_id") or "未知"), str(spec.get("speed") or "-")
    except Exception:  # noqa: BLE001
        return "未知", "-"


def main() -> int:
    ap = argparse.ArgumentParser(description="从已生成期反算流水线真实字/秒")
    ap.add_argument("--last", type=int, default=0, help="只看最近 N 期（默认全部）")
    ap.add_argument("--voice", default="", help="只统计该音色")
    args = ap.parse_args()

    eps: list[tuple[str, str, str, int, float, float, list[tuple[int, float]]]] = []
    for md in sorted(OUT_DIR.glob("*_脚本.md")):
        chars, secs, rows = measure(md)
        if not rows or secs <= 0:      # plan 产物只写稿、时长为 0
            continue
        voice, speed = tts_spec_of(md)
        if args.voice and voice != args.voice:
            continue
        eps.append((md.stem.replace("_脚本", ""), voice, speed, chars, secs,
                    chars / secs, [(r[1], r[2]) for r in rows]))
    if args.last:
        eps = eps[-args.last:]
    if not eps:
        print("没有可用的期（脚本里要有分镜时长）。")
        return 1

    print(f"{'期':<40} {'音色':<18} {'速':>4} {'段':>3} {'字数':>6} {'秒':>7} {'字/秒':>6}")
    print("-" * 92)
    for name, voice, speed, chars, secs, rate, per in eps:
        print(f"{name[:38]:<40} {voice:<18} {speed:>4} {len(per):>3} {chars:>6} {secs:>7.1f} {rate:>6.2f}")

    groups: dict[tuple[str, str], list[tuple[int, float]]] = {}
    for _, voice, speed, _chars, _secs, _rate, per in eps:
        groups.setdefault((voice, speed), []).extend(per)

    print()
    for (voice, speed), per in sorted(groups.items()):
        c = sum(p[0] for p in per)
        s = sum(p[1] for p in per)
        rates = [p[0] / p[1] for p in per if p[1] > 0]
        print(f"音色 {voice} @ 语速 {speed}：{len(per)} 段 / {c} 字 / {s:.1f} 秒 "
              f"→ 总平均 {c/s:.2f} 字/秒（逐段中位数 {statistics.median(rates):.2f}，"
              f"范围 {min(rates):.2f}–{max(rates):.2f}）")

    print("\n把这个值填进 config.yaml 的 story.chars_per_second。")
    if any(v == "未知" for _, v, *_ in eps):
        print("注：「未知」= 该期 metadata 里没记 tts_spec（2026-09-14 之前生成的），"
              "音色靠外部记录确认。")
    print("⚠️ 别用 probe-tts 的范文值 —— 那是连续合成，比流水线的分镜合成快一截。")
    print("⚠️ 换音色/换语速后要重新跑一遍本工具，历史期不能混着平均。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
