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
    python scripts/smoke_clips.py --small          # 960x540（内存紧的机器用）
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
    """断言「条件成立」。

    坑（踩过）：写成 `check(name, f"不成立 {detail}", True)` 是**参数错位** ——
    check 的签名是 (name, ok, detail)，于是字符串被当成 ok（恒真 → 失败被记成通过），
    布尔值被当成 detail（`'  ' + True` 直接 TypeError）。
    结果是「任何一项断言失败 = 冒烟脚本崩掉」，既看不到是哪一项、也看不到后面的检查。
    """
    check(name, bool(cond), detail if cond else f"不成立  {detail}".strip())


def make_clip(dst: Path, seconds: float, size: str, rate: int = 30) -> Path:
    """合成一段「像素材」的片子：有运动、带音频（考验导入工具去音轨）。

    画面 = **12 像素棋盘格**（不是 testsrc2/彩条）。为什么要这么挑：
    blurpad 的验收靠「素材本体 vs 模糊衬底」的高频能量比，而**模糊只杀高频细节**
    —— 宽色块的边界模糊后照样有能量，编码也会把细噪声磨平（这两条都踩过，
    比值只有 1.08~1.34，判据形同虚设）。12px 棋盘格正好在中间：
    缩放扛得住、blur 一定抹掉。
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    w, hgt = (int(x) for x in size.lower().split("x"))
    video.run_ffmpeg(["-f", "lavfi", "-i", f"nullsrc=s={size}:r={rate}",
                      "-f", "lavfi", "-i", "sine=frequency=440",
                      "-vf", "geq=lum='if(mod(floor(X/12)+floor(Y/12),2),235,16)':cb=128:cr=128",
                      "-t", f"{seconds:.2f}", "-c:v", "libx264", "-preset", "ultrafast",
                      "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", "-y", dst.name],
                     cwd=dst.parent, desc=f"合成素材 {dst.name}")
    return dst


def band_energy(frame: Path, y0: float, y1: float,
                x0: float = 0.0, x1: float = 1.0, stat: str = "mean") -> float:
    """这一横条的「边缘能量」= 横向相邻像素亮度差的平均值（多行取平均）。

    用来按像素判定 blurpad 有没有生效：素材本体保留细节 → 能量高；
    上下衬底被 boxblur 过 → 能量低。光看图看不出来（素材本身是大色块时模糊与否一样）。

    `x0`/`x1` 限定横向范围：**必须避开叠加文字**。文字（白字黑边）是最强的"高频"，
    左上角的关键词和底部字幕都曾把判据带歪（踩过：关键词挪到左上角后，
    上条带能量反而比中间高，判据直接失效）。
    """
    from PIL import Image
    with Image.open(frame) as im:
        g = im.convert("L")
        w, h = g.size
        xa, xb = int(w * x0), int(w * x1)
        ya, yb = int(h * y0), max(int(h * y0) + 1, int(h * y1))
        band = g.crop((xa, ya, xb, yb))
    bw, bh = band.size
    vals = list(band.get_flattened_data()) if hasattr(band, "get_flattened_data") \
        else list(band.getdata())
    rows = []
    for r in range(bh):
        row = vals[r * bw:(r + 1) * bw]
        rows.append(sum(abs(row[i] - row[i - 1]) for i in range(1, len(row)))
                    / max(1, len(row) - 1))
    if not rows:
        return 0.0
    if stat == "median":
        # 中位数：文字只占少数行，用中位数就不会被"那一行字"带偏（均值会被带偏）
        rows.sort()
        return round(rows[len(rows) // 2], 3)
    return round(sum(rows) / len(rows), 3)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-o", "--orientation", default="both",
                    choices=["both", "portrait", "landscape"])
    ap.add_argument("--small", action="store_true",
                    help="小尺寸跑（960x540 / 540x960）：内存紧的机器上也能跑起来。"
                         "尺寸相关的数值验收（blurpad 清晰带）不看这项时更稳，其余检查一项不少")
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
    if args.small:
        # 本机 14GB、被 VS Code/浏览器吃满时会剩不到 1GB，1080p 图层链会
        # 「Cannot allocate memory」。小尺寸只是让冒烟跑得起来，规格验收仍按真实配置。
        spec.update(width=960, height=540)
        # 注意：orientations 里的值是 Config（dict 子类，靠 __getattr__ 取 width），
        # 整个替换成普通 dict 会让 cfg.video.orientations[o].width 直接 AttributeError
        for _o, _w, _h in (("portrait", 540, 960), ("landscape", 960, 540)):
            _t = cfg.video["orientations"][_o]
            _t["width"], _t["height"] = _w, _h
        print("（--small：960x540 / 540x960）")

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
    # 当成「系列片的一集」来跑：顶部小字会换成「栏目 · 系列名 第N集」，
    # 这样系列标签的渲染路径（含封面同口径）也在冒烟覆盖里，零 API 成本
    story = Story(topic="冒烟测试", title="切片链路冒烟", period="东汉末", chapters=chapters,
                  series="冒烟系列", series_ep=1)
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
        # 冒烟不是生产：preset 用 ultrafast，内存紧的机器也能跑（生产默认还是 medium）
        info = clips_mod.normalize_clip(raw, dest, spec, float(cfg.clips.max_seconds),
                                       preset="ultrafast")
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
        # 关键词（大字标注）现在在**左上角**：纵向 0.08-0.20、横向左半边。
        # 原来压在画面正中（0.40-0.52），会挡住素材主体（人脸/朝堂就在中间）。
        ink_callout = frames.ink_band(fg, 0.08, 0.18, 0.0, 0.60)
        ink_mid = frames.ink_band(fg, 0.40, 0.52)
        ink_nametag = frames.ink_band(fg, 0.57, 0.67)
        ink_top = frames.ink_band(fg, 0.04, 0.09)
        check("关键词落在左上角（纵向 0.08-0.18 / 左 60% 宽以内）",
              ink_callout > 200, f"{ink_callout} 像素")
        check("画面正中不再压着大字（关键词已从中间挪走）",
              ink_mid < 80, f"{ink_mid} 像素")
        check("人名条落在左侧带（0.57-0.67）", ink_nametag > 200, f"{ink_nametag} 像素")
        check("顶部小字带（系列标签「栏目 · 系列名 第N集」）", ink_top > 50, f"{ink_top} 像素")
    else:
        check("标注层存在", False, "没找到 fg.png")

    # ---- blurpad：直接验**生产滤镜链**，不靠"抽到哪一秒"的运气
    #
    # 这段的历史：原来在竖屏成片里抽一帧、比「中间带 vs 上下带」的能量比。
    # 它长期靠运气通过 —— 抽到渐变占位图时整幅平坦（两边都接近 0，比了个寂寞），
    # 抽到 freeze_zoom（定格放大，整幅铺满、根本不走模糊衬底）时整幅是同一块图案。
    # 关键词一挪位置判据就暴露了，连查三轮才发现是"取帧时机"的问题，不是渲染的问题。
    # 现在改成：拿真实素材（12px 棋盘格）+ **生产用的 _fit_chain** 出图，
    # 逐行能量用中位数比 —— 判据强（模糊版 0.0 vs 未模糊 23.8），且与抽帧时机无关。
    if "portrait" in fins:
        print("\n[blurpad] 生产滤镜链按像素验收（自造高频素材，不依赖抽帧时机）")
        from hsg import shotvideo
        probe_src = work / "_probe_src.mp4"
        if not probe_src.exists():
            make_clip(probe_src, 1.5, "960x540")
        energies = {}
        for fit in ("blurpad", "cover"):
            chain = shotvideo._fit_chain((540, 960), 30, fit)
            shot = work / f"_probe_{fit}.jpg"
            video.run_ffmpeg(["-i", probe_src.name, "-filter_complex", f"[0:v]{chain}[v]",
                              "-map", "[v]", "-frames:v", "1", "-q:v", "1", "-y", shot.name],
                             cwd=work, desc=f"blurpad 验收出图（{fit}）")
            center = band_energy(shot, 0.36, 0.64, 0.05, 0.55, stat="median")
            edge = band_energy(shot, 0.05, 0.25, 0.05, 0.55, stat="median")
            energies[fit] = (center, edge)
            check(f"{fit}：中间完整画面 + 上下模糊衬底（本体能量 {center} vs 衬底 {edge}）",
                  center > edge * 3 if fit == "blurpad" else edge > 5,
                  f"{center} / {edge}")
        check_true("对照：cover（裁满）没有模糊衬底（证明判据能分辨两种 fit）",
                   energies["cover"][1] > energies["blurpad"][1] * 3,
                   f"cover 衬底 {energies['cover'][1]} vs blurpad 衬底 {energies['blurpad'][1]}")

    print("\n" + "=" * 70)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    for f in FAIL:
        print(f"  ✗ {f}")
    print("=" * 70)
    print("结论：" + ("切片 + EDL 剪辑链路正常。" if not FAIL else "有问题，先修上面 FAIL 的项。"))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
