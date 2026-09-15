"""从「已生成期」的脚本反算流水线真实字/秒 —— 0 API 成本。

用途：换音色或改语速后，校准 config.yaml 的 story.chars_per_second。

为什么不用 probe-tts 的值
------------------------
probe-tts 读的是一整篇连续范文，一次合成；而流水线是**按分镜**逐段合成的，
每段都带 TTS 的首尾静音，所以实际产出比连续范文慢一截。实测差异：

    克隆音色 hsg_story_v2 @1.1 ：范文 3.91 字/秒，流水线实际 3.45 字/秒
    系统音色 audiobook_male_1 @1.1：范文 4.77 字/秒，流水线实际 4.70 字/秒

用偏快的值写稿会偏长，容易顶到区间上限并多触发一轮自适应改写。

本工具的数据来源
----------------
`data/output/*_脚本.md` 里每个分镜都记着正文与**实测语音时长**，
所以拿已生成的那几期直接反算，比任何范文探针都准，而且不花一分钱 API。

⚠️ 音色必须验证，不能靠"期号相近"猜
-----------------------------------
不同音色的字/秒差得很远（克隆音色 3.45 vs 系统音色 4.70，差 36%）。
把不同音色的期混在一起平均，算出来的值对谁都不对 —— 这是踩过的坑：
早期 metadata 不记 `tts_spec`，于是几期全被当成"未知"混着平均，得出了 4.59。

现在改用**音频缓存文件名反查**验证：缓存名里的 tag = hash(文本 + 音色 + 语速)，
所以拿候选组合逐个试，全部命中才算验证通过（不依赖 metadata 有没有记）。
验证不过的期会被**排除**并标注，不再污染平均值。

用法
----
    python scripts/calib_rate.py              # 全部期（自动排除未验证的）
    python scripts/calib_rate.py --last 5
    python scripts/calib_rate.py --voice audiobook_male_1
    python scripts/calib_rate.py --include-unverified   # 连没验证过的期也算进来（不推荐）

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
sys.path.insert(0, str(ROOT / "src"))

OUT_DIR = ROOT / "data" / "output"
AUDIO_DIR = ROOT / "data" / "audio"

SCENE_RE = re.compile(r"^\*\*\[分镜\s*(\d+)\]\*\*\s*(.*)$")
DUR_RE = re.compile(r"时长\s*([\d.]+)\s*s")


def measure(md: Path) -> tuple[int, float, list[tuple[int, str, float]]]:
    """从脚本 md 抽出 (总字数, 总秒数, [(分镜号, 正文, 秒数)])。"""
    rows: list[tuple[int, str, float]] = []
    pending: tuple[int, str] | None = None
    for ln in md.read_text(encoding="utf-8").splitlines():
        m = SCENE_RE.match(ln.strip())
        if m:
            pending = (int(m.group(1)), m.group(2).strip())
            continue
        d = DUR_RE.search(ln)
        if d and pending:
            text = pending[1].replace("`", "")
            rows.append((pending[0], text, float(d.group(1))))
            pending = None
    return sum(len(r[1]) for r in rows), sum(r[2] for r in rows), rows


def recorded_spec(md: Path) -> tuple[str, str]:
    """读同期 metadata 的 tts_spec（音色/语速）。2026-09-14 之前的期没记，返回「未知」。"""
    meta = md.with_name(md.name.replace("_脚本.md", "_metadata.json"))
    if not meta.exists():
        return "", ""
    try:
        spec = (json.loads(meta.read_text(encoding="utf-8")) or {}).get("tts_spec") or {}
        return str(spec.get("voice_id") or ""), str(spec.get("speed") or "")
    except Exception:  # noqa: BLE001
        return "", ""


def candidate_specs() -> list[tuple[str, float]]:
    """候选（音色, 语速）组合：配置里的那个 + 历史用过的音色 × 常见语速。"""
    voices: list[str] = []
    try:
        from hsg.config import load_config
        cfg = load_config()
        sub = cfg.tts[str(cfg.tts.provider)]
        if sub.get("voice_id"):
            voices.append(str(sub["voice_id"]))
    except Exception:  # noqa: BLE001
        pass
    for extra in ("audiobook_male_1", "hsg_story_v2", "male-qn-qingse",
                  "zh-CN-YunxiNeural"):
        if extra not in voices:
            voices.append(extra)
    speeds = [1.0, 1.05, 1.1, 1.15, 1.2, 1.25, 1.3, 1.4, 1.5, 0.9, 0.95]
    return [(v, s) for v in voices for s in speeds]


def probe_spec(scenes: list[tuple[int, str]],
               cands: list[tuple[str, float]]) -> tuple[str, float, int]:
    """用音频缓存名反查该期当初的音色/语速，返回 (音色, 语速, 命中段数)。

    tag = hash(文本 + 音色 + 语速)，所以这是**精确验证**，不是猜。
    """
    from hsg import tts as tts_mod
    best: tuple[str, float] = ("", 0.0)
    best_n = 0
    for voice, speed in cands:
        n = 0
        for idx, text in scenes:
            if (AUDIO_DIR / f"scene_{int(idx):03d}_{tts_mod.audio_tag(text, voice, speed)}.mp3").exists():
                n += 1
        if n > best_n:
            best, best_n = (voice, speed), n
    return best[0], best[1], best_n


def main() -> int:
    ap = argparse.ArgumentParser(description="从已生成期反算流水线真实字/秒")
    ap.add_argument("--last", type=int, default=0, help="只看最近 N 期（默认全部）")
    ap.add_argument("--voice", default="", help="只统计该音色")
    ap.add_argument("--include-unverified", action="store_true",
                    help="把音色验证不通过的期也算进来（不推荐，会污染平均值）")
    args = ap.parse_args()

    cands = candidate_specs()
    eps: list[dict] = []
    for md in sorted(OUT_DIR.glob("*_脚本.md")):
        chars, secs, rows = measure(md)
        if not rows or secs <= 0:      # plan 产物只写稿、时长为 0
            continue
        voice, speed = recorded_spec(md)
        verified = ""
        if voice and speed:
            verified = f"metadata ✓{len(rows)}/{len(rows)}"
        else:
            pv, ps, hit = probe_spec([(r[0], r[1]) for r in rows], cands)
            if hit == len(rows) and hit > 0:
                voice, speed, verified = pv, str(ps), f"音频key ✓{hit}/{len(rows)}"
            elif hit > 0:
                voice, speed = pv, str(ps)
                verified = f"⚠ 只命中 {hit}/{len(rows)}"
        eps.append({"name": md.stem.replace("_脚本", ""), "voice": voice or "未知",
                    "speed": speed or "-", "chars": chars, "secs": secs,
                    "rows": [(len(r[1]), r[2]) for r in rows],
                    "verified": verified, "ok": verified.endswith(f"✓{len(rows)}/{len(rows)}")})
    if args.last:
        eps = eps[-args.last:]
    if not eps:
        print("没有可用的期（脚本里要有分镜时长）。")
        return 1

    print(f"{'期':<38} {'音色':<18} {'速':>5} {'段':>3} {'字数':>6} {'秒':>7} "
          f"{'字/秒':>6}  验证")
    print("-" * 104)
    for e in eps:
        rate = e["chars"] / e["secs"]
        print(f"{e['name'][:36]:<38} {e['voice']:<18} {e['speed']:>5} "
              f"{len(e['rows']):>3} {e['chars']:>6} {e['secs']:>7.1f} {rate:>6.2f}  "
              f"{e['verified'] or '未验证'}")

    usable = [e for e in eps if e["ok"] or args.include_unverified]
    skipped = [e for e in eps if not (e["ok"] or args.include_unverified)]
    if args.voice:
        usable = [e for e in usable if e["voice"] == args.voice]

    print()
    if not usable:
        print("⚠️ 没有一期通过音色验证 —— 别据此改配置。")
        print("   metadata 没记 tts_spec 且音频缓存也找不到，就无法确认当初用的音色。")
        return 1

    groups: dict[tuple[str, str], list[tuple[int, float]]] = {}
    for e in usable:
        groups.setdefault((e["voice"], e["speed"]), []).extend(e["rows"])
    for (voice, speed), per in sorted(groups.items()):
        c = sum(p[0] for p in per)
        s = sum(p[1] for p in per)
        rates = [p[0] / p[1] for p in per if p[1] > 0]
        print(f"音色 {voice} @ 语速 {speed}：{len(per)} 段 / {c} 字 / {s:.1f} 秒 "
              f"→ 总平均 {c/s:.2f} 字/秒（逐段中位数 {statistics.median(rates):.2f}，"
              f"范围 {min(rates):.2f}–{max(rates):.2f}）")

    if skipped:
        print()
        print(f"已排除 {len(skipped)} 期（音色没验证通过，混进来会污染平均值）：")
        for e in skipped:
            print(f"  · {e['name'][:44]}　{e['verified'] or '无法确认音色'}")

    print("\n把这个值填进 config.yaml 的 story.chars_per_second。")
    print("⚠️ 别用 probe-tts 的范文值 —— 那是连续合成，比流水线的分镜合成快一截。")
    print("⚠️ 换音色/换语速后要重新跑一遍本工具，不同音色的期不能混着平均。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
