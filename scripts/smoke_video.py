"""零 LLM 冒烟测试：验证「配图 → 画面合成 → ASS 字幕 → ffmpeg 编码 → 拼接」整条媒体链路。

不需要调用任何文本模型；只花一次很短的 TTS（可用 --no-tts 改成 ffmpeg 生成的静音轨，
那就完全不花钱）。媒体层出问题时先用它定位，不要拿整条流水线去试。

    python scripts/smoke_video.py
    python scripts/smoke_video.py --no-tts --orientation portrait
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from hsg import images as images_mod          # noqa: E402
from hsg import media, tts as tts_mod, video  # noqa: E402
from hsg.config import load_config            # noqa: E402
from hsg.subtitles import build_ass, split_cues  # noqa: E402
from hsg.textfmt import sanitize_narration      # noqa: E402


SAMPLE = (
    "东汉建安十三年，公元二百零八年冬天，曹操的水军把战船首尾相连，"
    "停在长江北岸。江面上起了东南风，黄盖的十艘蒙冲斗舰，"
    "满载薪草膏油，直冲曹营。这一夜之后，天下的格局就再也回不去了。"
)


def make_silence(path: Path, duration: float, cfg) -> Path:
    import subprocess
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", f"anullsrc=r={cfg.video.audio_sample_rate}:cl=stereo",
         "-t", f"{duration:.3f}", "-c:a", "aac", str(path)],
        check=True, capture_output=True)
    return path


def run_case(cfg, orient: str, use_tts: bool) -> dict:
    size = (int(cfg.video.orientations[orient].width), int(cfg.video.orientations[orient].height))
    work = cfg.paths.get_path("temp_dir") / "smoke" / orient
    if work.exists():
        shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    print(f"\n=== 冒烟测试 [{orient}] {size[0]}x{size[1]} → {work} ===")

    # ---- 1. 两张真实配图（零 LLM）
    used: list[int] = []
    paths = []
    for i, q in enumerate(["赤壁之战 古画", "宋代 山水画 真迹"], start=1):
        p, credit, src, _ = images_mod.fetch_for_scene(
            i, [q], cfg, work / "dl", used)
        print(f"  配图 {i}：{p.name if p else '未找到（走渐变底图）'}  来源={src}  出处={credit}")
        paths.append(p)

    # ---- 2. 语音
    audio_dir = work / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    text = sanitize_narration(SAMPLE)
    if use_tts:
        with tts_mod.TTS(cfg) as t:
            a1, d1 = t.synthesize(text, audio_dir / "a1.mp3")
            a2, d2 = t.synthesize(text[:40], audio_dir / "a2.mp3")
    else:
        a1, d1 = make_silence(audio_dir / "a1.m4a", 8.0, cfg), 8.0
        a2, d2 = make_silence(audio_dir / "a2.m4a", 5.0, cfg), 5.0
    print(f"  语音：{d1:.2f}s + {d2:.2f}s = {d1 + d2:.2f}s（{'真实 TTS' if use_tts else '静音轨道'}）")

    # ---- 3. 画面 + 字幕 + 编码
    tail = float(cfg.video.get("tail_padding", 0.55))
    fade = float(cfg.video.get("transition_seconds", 0.4))
    names = []
    for i, (img, audio, dur, cap) in enumerate(
            [(paths[0], a1, d1, "赤壁之战·火攻"), (paths[1], a2, d2, "长江上的战船")], start=1):
        bg, fg = media.build_layers(
            work / f"s{i}_bg.jpg", work / f"s{i}_fg.png", size, cfg,
            image_path=img, kicker=f"{cfg.video.channel_name} · 第{i}章 示例章节",
            title="示例章节标题" if i == 1 else "", caption=cap)
        total = dur + tail
        cues = split_cues(text if i == 1 else text[:40],
                          int(cfg.subtitle.max_chars_per_line) * int(cfg.subtitle.max_lines))
        ass = build_ass(cues, total, work / f"s{i}.ass", size, cfg)
        seg = video.encode_segment(work, bg.name, fg.name, audio.relative_to(work).as_posix(),
                                   ass.name, f"s{i}.mp4", size, total, cfg,
                                   fade_in=fade if i == 1 else 0.0,
                                   fade_out=fade if i == 2 else 0.0, motion_mode=i)
        print(f"  片段 {i}：{seg.name}  {video.media_duration(seg):.2f}s")
        names.append(seg.name)

    master = video.concat_segments(work, names, "smoke.mp4")
    info = video.probe_streams(master)
    vinfo, ainfo = info.get("video") or {}, info.get("audio") or {}
    print(f"  拼接结果：{master}")
    print(f"   时长 {info.get('duration', 0):.2f}s  视频 {vinfo.get('width')}x{vinfo.get('height')}"
          f" @{vinfo.get('fps')} {vinfo.get('codec')}")
    print(f"   音轨 {ainfo.get('codec')} {ainfo.get('sample_rate')}Hz {ainfo.get('channels')}ch")
    return {"orient": orient, "master": str(master), "duration": info.get("duration", 0),
            "video": vinfo, "audio": ainfo, "images": [p is not None for p in paths],
            "fps": str(cfg.video.fps)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--orientation", default="both", choices=["both", "portrait", "landscape"])
    ap.add_argument("--no-tts", action="store_true", help="用静音轨代替真实 TTS（零成本）")
    args = ap.parse_args()

    cfg = load_config()
    video.check_ffmpeg()
    orients = list(cfg.video.orientations.keys()) if args.orientation == "both" else [args.orientation]

    results = []
    failed = []
    for o in orients:
        try:
            results.append(run_case(cfg, o, not args.no_tts))
        except Exception as exc:  # noqa: BLE001
            failed.append((o, exc))
            print(f"  ✗ {o} 失败：{exc}")

    print("\n" + "=" * 70)
    for r in results:
        ok_v = r["video"].get("width") and r["video"].get("height")
        ok_a = bool(r["audio"].get("codec"))
        ok_d = r["duration"] > 1.0
        verdict = "PASS" if (ok_v and ok_a and ok_d) else "FAIL"
        print(f"[{verdict}] {r['orient']:9s} {r['duration']:6.2f}s  "
              f"{r['video'].get('width')}x{r['video'].get('height')}  "
              f"音轨={r['audio'].get('codec')}  配图={'有' if all(r['images']) else '部分缺失'}")
        print(f"         {r['master']}")
    for o, exc in failed:
        print(f"[FAIL] {o}: {exc}")
    print("=" * 70)
    print("结论：" + ("媒体链路正常，可以跑正式流程。" if results and not failed
                    else "有问题，先修上面 FAIL 的项。"))
    return 0 if results and not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
