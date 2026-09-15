"""零 API 冒烟测试：影视切片 + EDL 剪辑链路。

验的是「阶段 2」真正会跑的那条路，全程不调任何 API：

    造 story → 需求清单（先出清单）→ 合成素材入库并**按清单绑槽位**
    → 排镜头（走规则兜底，LLM 传 None）→ EDL 校验
    → 按镜头渲染（切片背景 + 定格放大 + 慢推 + 大字标注 + 回退画面）
    → 拼接 → 自检（时长/画幅/音轨/切片占比/镜头段是否无声）

为什么必需：素材库在人工导入之前是空的，整条剪辑链路没有真实素材可测；
而它最容易「看起来跑通了、其实画面是错的」（时长错位、语音被截断、
切片占比超限、没绑素材的分镜被塞进无关切片）。所以用 ffmpeg 合成的假素材钉死。

素材库用**独立的临时目录**，不碰 data/clips 里你剪好的东西。

用法：
    python scripts/smoke_clips.py                  # 横竖两版
    python scripts/smoke_clips.py -o landscape     # 只跑一版（快一倍）
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from hsg import clips as clips_mod            # noqa: E402
from hsg import edl as edl_mod                # noqa: E402
from hsg import frames, needs as needs_mod    # noqa: E402
from hsg import pipeline, video               # noqa: E402
from hsg.config import load_config            # noqa: E402
from hsg.models import Chapter, Scene, Story  # noqa: E402

PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  {'✓' if ok else '✗'} {name}{('  ' + detail) if detail else ''}")


def check_true(name: str, cond: bool, detail: str = "") -> None:
    """断言「条件成立」——用 check(name, cond, True) 会把布尔值当「得到」去比字符串。"""
    if cond:
        check(name, True, detail)
    else:
        check(name, f"不成立 {detail}", True)


def make_clip(dst: Path, seconds: float, size: str, rate: int = 30) -> Path:
    """合成一段「像素材」的片子：有运动、带音频（考验导入工具去音轨）。"""
    dst.parent.mkdir(parents=True, exist_ok=True)
    video.run_ffmpeg(["-f", "lavfi", "-i", f"testsrc2=size={size}:rate={rate}",
                      "-f", "lavfi", "-i", "sine=frequency=440",
                      "-t", f"{seconds:.2f}", "-c:v", "libx264", "-preset", "ultrafast",
                      "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", "-y", dst.name],
                     cwd=dst.parent, desc=f"合成素材 {dst.name}")
    return dst


def band_energy(frame: Path, y0: float, y1: float) -> float:
    """这一横条的「边缘能量」= 横向相邻像素亮度差的平均值。

    用来按像素判定「模糊衬底 + 完整画面」（blurpad）到底有没有生效：
    中间那条保留源素材细节 → 能量高；上下两条被 boxblur 过 → 能量低。
    光看图看不出来 —— 如果素材本身是大色块，模糊与否长得一样（踩过）。
    """
    from PIL import Image
    with Image.open(frame) as im:
        g = im.convert("L")
        w, h = g.size
        y = int(h * (y0 + y1) / 2)
        px = g.crop((0, y, w, y + 1)).load()
    return round(sum(abs(px[i, 0] - px[i - 1, 0]) for i in range(1, w)) / (w - 1), 3)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-o", "--orientation", default="both",
                    choices=["both", "portrait", "landscape"])
    args = ap.parse_args()

    cfg = load_config()
    video.check_ffmpeg()
    work = cfg.paths.get_path("temp_dir") / "smoke_clips"
    if work.exists():
        shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)

    # ---- 素材库指向临时目录（绝不碰 data/clips 里的人工素材）
    cfg.clips["dir"] = str(work / "clips")
    cfg.clips["index"] = str(work / "clips" / "index.json")
    cfg.story["intro_speak"] = False          # 片头不合成语音 → 零 API
    cfg.bgm["enabled"] = False                # 不混 BGM，跑得快
    cfg.video["cover"] = False
    spec = {k: int(cfg.clips.spec.get(k, d)) for k, d in
            (("width", 1920), ("height", 1080), ("fps", 30))}

    print(f"=== 冒烟：切片 + EDL 剪辑链路 → {work} ===")

    # ---- 1. 造 story（6 个分镜 / 2 章；只有前两场会给素材，其余走回退）
    print("\n[1] 造一个 6 分镜的小 story（静音轨代替配音，零 API）")
    durs = [9.0, 9.0, 7.0, 7.0, 7.0, 7.0]
    audio_dir = work / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    chapters = [Chapter(index=1, heading="第一章 示例", scenes=[]),
                Chapter(index=2, heading="第二章 示例", scenes=[])]
    for i, d in enumerate(durs, start=1):
        a = video.make_silence(audio_dir / f"scene_{i:03d}.mp3", d, cfg)
        ch = 1 if i <= 3 else 2
        chapters[ch - 1].scenes.append(Scene(
            index=i, text=f"这是第{i}个分镜的旁白，用来验收画面与语音是否对得上。" * 2,
            image_query=f"示例 检索词 {i}", caption=f"图注 {i}", chapter_index=ch,
            is_chapter_start=(i in (1, 4)), audio_path=a, duration=d))
    story = Story(topic="冒烟测试", title="切片链路冒烟", period="东汉末", chapters=chapters)
    check("静音轨生成（.mp3 + libmp3lame）",
          all((audio_dir / f"scene_{i:03d}.mp3").exists() for i in range(1, 7)))

    # ---- 2. 需求清单（真实流程是「先出清单，再照清单剪素材」）
    print("\n[2] 需求清单（LLM=None → 规则兜底）")
    index = clips_mod.empty_index()
    need_obj = needs_mod.build_needs(story, cfg, None)
    cov0 = needs_mod.coverage(need_obj, index)
    check("清单出了槽位", cov0["slots_total"] > 0, f"{cov0['slots_total']} 个槽位")
    check("空库时全部算缺口（复用槽位除外）",
          len(cov0["missing"]) + len(cov0["reuse"]) == cov0["slots_total"])
    slots_of = {}
    for r in need_obj["slots"]:
        slots_of.setdefault(int(r["scene"]), []).append(r["slot"])

    # ---- 3. 合成素材并按清单绑槽位
    print("\n[3] 合成素材并规范化入库（按清单绑槽位）")
    made = []
    for i, (size, scene) in enumerate([("1920x1080", 1), ("1440x1080", 2)], start=1):
        raw = make_clip(work / "raw" / f"{i}.mp4", 9.0, size)
        dest = clips_mod.clip_dir(cfg) / "norm" / f"smoke_{i:02d}.mp4"
        info = clips_mod.normalize_clip(raw, dest, spec, float(cfg.clips.max_seconds))
        bind = slots_of.get(scene, [])
        rec = clips_mod.add_clip(index, {
            "id": f"smoke_{i:02d}", "file": "norm/" + dest.name, "title": f"合成素材{i}",
            "year": 1994, "people": ["曹操"], "era": "东汉末", "topic": ["三国"],
            "desc": f"{size} 合成素材", "dur": info["duration"], "slots": bind})
        assert rec is not None
        made.append((info, bind))
        check(f"素材 {i} 入库并绑到第 {scene} 场的 {len(bind)} 个槽位",
              info["duration"] > 5.0 and bool(bind),
              f"{info['duration']:.2f}s  slots={bind}")
    clips_mod.save_index(clips_mod.index_path(cfg), index)

    probe = video.probe_streams(clips_mod.clip_file(cfg, "smoke_01"))
    check("入库后的素材真的没有音轨", not (probe.get("audio") or {}).get("codec"),
          f"audio={probe.get('audio')}")

    cov = needs_mod.coverage(need_obj, index)
    check("覆盖度认得出已绑定的槽位", len(cov["bound"]) == len(slots_of[1]) + len(slots_of[2]),
          f"已绑定 {cov['bound']}")

    # ---- 4. 排镜头 + 校验
    print("\n[4] 排镜头（LLM=None → 规则兜底）+ EDL 校验")
    edl = edl_mod.build_edl(story, need_obj, index, cfg, None)
    plans = {int(p["scene"]): p for p in edl["scenes"]}
    check("每个分镜都有镜头", all(p.get("shots") for p in edl["scenes"]),
          f"{len(edl['scenes'])} 场")
    clip_scenes = {int(p["scene"]) for p in edl["scenes"]
                   if any(s.get("kind") == "clip" for s in p["shots"])}
    fall_scenes = {int(p["scene"]) for p in edl["scenes"]
                   if any(s.get("kind") in ("still", "generate") for s in p["shots"])}
    check("绑了素材的场次用真切片", clip_scenes == {1, 2}, f"{sorted(clip_scenes)}")
    check("没绑素材的场次走回退画面（不许自动拿同年代素材顶替）",
          fall_scenes == {3, 4, 5, 6}, f"{sorted(fall_scenes)}")
    check("同场多个镜头复用同一条素材",
          all(len(s["shots"]) >= 2 for s in (plans[1], plans[2])),
          f"第1场 {len(plans[1]['shots'])} 镜 / 第2场 {len(plans[2]['shots'])} 镜")
    problems = edl_mod.validate_edl(edl, cfg, index)
    check("EDL 通过合规校验", not problems, f"问题：{problems}")
    print("    " + edl_mod.describe(edl))

    # ---- 5. 把「定格放大」「慢推」「大字标注」真跑一遍
    print("\n[5] 按镜头渲染（含定格放大 / 慢推 / 大字标注 / 回退画面）")
    plans[1]["shots"][0].update({"treatment": "freeze_zoom", "callout": "定格",
                                 "nametag": "曹操"})
    plans[1]["shots"][1]["treatment"] = "slow_push"
    plans[2]["shots"][0]["treatment"] = "freeze_zoom"
    problems = edl_mod.validate_edl(edl, cfg, index)
    check("改过手法之后仍然合规", not problems, f"{problems}")

    orients = list(cfg.video.orientations.keys()) if args.orientation == "both" \
        else [args.orientation]
    fins: dict[str, Path] = {}
    for orient in orients:
        out, _cover = pipeline.render_orientation(story, cfg, orient, edl=edl)
        fins[orient] = out
        print(f"    渲染完成 [{orient}] {out.name}")

    # ---- 6. 自检
    print("\n[6] 自检")
    tail = float(cfg.video.get("tail_padding", 0.55))
    want_body = sum(durs) + tail * len(durs)
    share = edl_mod.clip_share(edl)
    check(f"切片占比在合规上限内（{share:.1%} ≤ {float(cfg.clips.max_share):.0%}）",
          share <= float(cfg.clips.max_share))
    for orient, out in fins.items():
        info = video.probe_streams(out)
        v = info.get("video") or {}
        size = (int(cfg.video.orientations[orient].width),
                int(cfg.video.orientations[orient].height))
        dur = float(info.get("duration") or 0)
        check(f"[{orient}] 画幅正确 {size[0]}x{size[1]}",
              (v.get("width"), v.get("height")) == size,
              f"{v.get('width')}x{v.get('height')}")
        check(f"[{orient}] 有音轨（旁白听得到）",
              bool((info.get("audio") or {}).get("codec")),
              f"audio={(info.get('audio') or {}).get('codec')}")
        check(f"[{orient}] 成片时长覆盖正文", dur >= want_body * 0.9,
              f"{dur:.1f}s 正文应 {want_body:.1f}s")
        check(f"[{orient}] 正文字幕烧进去了", bool(fins[orient].exists()))

    # 镜头段必须无声（语音最后才贴：护栏 12）
    seg_root = cfg.paths.get_path("segment_dir") / orients[0]
    shot1 = seg_root / "seg_001" / "sh01.mp4"
    if shot1.exists():
        p = video.probe_streams(shot1)
        check("镜头段是无音的（语音最后才贴，护栏 12）",
              not (p.get("audio") or {}).get("codec"), f"audio={p.get('audio')}")
        check("镜头段时长 = EDL 里写的时长",
              abs(float(p.get("duration") or 0) - float(plans[1]["shots"][0]["dur"])) < 0.2,
              f"{float(p.get('duration') or 0):.2f}s vs {plans[1]['shots'][0]['dur']:.2f}s")
        scene_seg = seg_root / "seg_001" / "seg_001.mp4"
        if scene_seg.exists():
            ps = video.probe_streams(scene_seg)
            check("整场段贴回了语音（有音轨）", bool((ps.get("audio") or {}).get("codec")))
            check("整场段时长 = 旁白 + 尾垫",
                  abs(float(ps.get("duration") or 0) - float(plans[1]["dur"])) < 0.3,
                  f"{float(ps.get('duration') or 0):.2f}s vs {plans[1]['dur']:.2f}s")
    else:
        check("镜头段存在", False, str(shot1))

    slide_root = cfg.paths.get_path("slide_dir")
    zoomed = list(slide_root.rglob("seg_001_sh01_zoom.jpg"))
    check("定格放大真的抽帧并放大（slide 目录有放大后的帧）",
          bool(zoomed), zoomed[0].name if zoomed else "没找到")

    fg = next(iter(slide_root.rglob("seg_001_sh01_fg.png")), None)
    if fg:
        ink_callout = frames.ink_band(fg, 0.40, 0.52)
        ink_nametag = frames.ink_band(fg, 0.57, 0.67)
        ink_top = frames.ink_band(fg, 0.04, 0.09)
        check("大字标注落在中部带（0.40-0.52）", ink_callout > 200, f"{ink_callout} 像素")
        check("人名条落在左侧带（0.57-0.67）", ink_nametag > 200, f"{ink_nametag} 像素")
        check("栏目小字在顶部带（0.04-0.09）", ink_top > 50, f"{ink_top} 像素")
    else:
        check("标注层存在", False, "没找到 fg.png")

    if "portrait" in fins:
        # 竖屏放横屏素材：必须「完整画面居中 + 上下模糊衬底」，不能硬裁掉两边
        frame = work / "_portrait_check.jpg"
        video.run_ffmpeg(["-ss", "6", "-i", str(fins["portrait"]), "-frames:v", "1",
                          "-q:v", "1", "-y", frame.name], cwd=work, desc="抽帧验收")
        mid = band_energy(frame, 0.44, 0.56)
        edge = max(band_energy(frame, 0.02, 0.12), band_energy(frame, 0.88, 0.98))
        check_true("竖屏：中间是完整画面、上下是模糊衬底（blurpad 生效）",
                   mid > edge * 2.5, f"中间能量 {mid} / 衬底 {edge}")

    print("\n" + "=" * 70)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    for f in FAIL:
        print(f"  ✗ {f}")
    print("=" * 70)
    print("结论：" + ("切片 + EDL 剪辑链路正常。" if not FAIL else "有问题，先修上面 FAIL 的项。"))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
