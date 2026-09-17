"""T5/T6：把 EDL 的镜头表编成成片里的「分镜段」。

两条必须守住的（AGENTS.md 护栏 12 的落地）：

  ① **多镜头分镜绝不能把语音挂在第一个镜头上** —— 会被 `-t 镜头时长` 截断，
     旁白直接被吃掉。正确路径：所有镜头编成**无声段** → concat 成整场画面
     → 最后一次性贴回整条语音。
  ② 所有镜头（切片的和静态图的）必须**编码参数完全一致**
     （尺寸/帧率/codec/crf/pix_fmt）—— 否则 `concat -c copy` 拼出来会花屏或时长错乱。
     所以这里的编码参数全部从 `video.encode_segment` 的同一份配置读。

竖屏怎么放横屏素材（`clips.fit`）：

  · `cover`：裁满画布。16:9 素材进 9:16 只剩中间约 32% 宽度，人很容易被裁掉。
  · `blurpad`（默认）：画面按比例完整放进画布，四周用**同一帧放大模糊**当底。
    竖屏发横屏素材基本都得这么做，观感差一大截。

一次分镜段的三步：镜头（无声）→ concat → 贴语音 + 烧字幕 + 淡化（一遍编码完成）。
中间留文件不删，方便单个镜头重渲和排错（这也是本项目的习惯）。
"""
from __future__ import annotations

import logging
from pathlib import Path

from . import media, video
from .config import Config

log = logging.getLogger("hsg.shotvideo")


def _fit_chain(size: tuple[int, int], fps: int, fit: str) -> str:
    """把「任何画幅的素材铺满画布」的滤镜链写出来。"""
    w, h = size
    cover = (f"scale={w}:{h}:force_original_aspect_ratio=increase,"
             f"crop={w}:{h}")
    if str(fit) != "blurpad":
        return cover
    return (
        # 底：裁满 + 模糊 + 压暗（免得抢主体）
        f"split=2[bgsrc][fgsrc];"
        f"[bgsrc]{cover},boxblur=luma_radius=28:luma_power=2,"
        f"eq=brightness=-0.10:contrast=0.95[bg];"
        # 中：按比例完整放进来，尺寸取偶数（overlay 对奇数尺寸会偏 1px）
        f"[fgsrc]scale={w}:{h}:force_original_aspect_ratio=decrease,"
        f"crop=trunc(iw/2)*2:trunc(ih/2)*2[fgfit];"
        f"[bg][fgfit]overlay=(W-w)/2:(H-h)/2:format=auto"
    )


def encode_clip_shot(
    workdir: Path,
    clip_name: str,
    fg_name: str | None,
    out_name: str,
    size: tuple[int, int],
    duration: float,
    cfg: Config,
    *,
    src_in: float = 0.0,
    fade_in: float = 0.0,
    fade_out: float = 0.0,
    motion_mode: int = -1,
) -> Path:
    """用一段影视素材当背景编一个**无声**镜头（语音/字幕留到最后统一贴）。

    `motion_mode`：-1 = 不加额外位移（默认）。素材本身就在动，再叠一层推拉容易晕。
    只有导演点名要 `slow_push` 时才传 ≥0 —— 那时画布放大 `video.motion_scale` 倍、
    再按 `video.motion_expr` 缓慢平移裁切，观感是「镜头慢慢推过去」。
    要位移就**不能**用 blurpad（模糊底 + 位移会露馅），所以自动退回 cover。
    """
    v = cfg.video
    w, h = size
    fps = int(v.fps)
    fit = str(cfg.clips.get("fit", "blurpad"))
    if motion_mode >= 0:
        fit = "cover"          # 有位移时不能用模糊底

    tail = [f"fps={fps}", "format=yuv420p"]
    if fade_in > 0:
        tail.insert(0, f"fade=t=in:st=0:d={fade_in:.3f}")
    if fade_out > 0:
        tail.insert(0, f"fade=t=out:st={max(0.0, duration - fade_out):.3f}:d={fade_out:.3f}")

    if motion_mode >= 0 and bool(v.get("motion", True)):
        ms = float(v.get("motion_scale", 1.16))
        bw, bh = media._even(w * ms), media._even(h * ms)
        xe, ye = video.motion_expr(motion_mode, cfg, duration)
        # 先铺到「放大后的画布」，再缓慢平移裁到画布 → 就是一次缓推
        chain = (f"[0:v]{_fit_chain((bw, bh), fps, 'cover')}[big]"
                 f";[big]crop={w}:{h}:x='{xe}':y='{ye}',setsar=1[bgv]")
    else:
        chain = f"[0:v]{_fit_chain((w, h), fps, fit)}[bgv]"

    if fg_name:
        # 标注层（章节标题/大字/名条）叠在上面
        chain += (f";[1:v]format=rgba,setsar=1[fgo]"
                  f";[bgv][fgo]overlay=0:0:format=auto,{','.join(tail)}[v]")
    else:
        chain += f";[bgv]{','.join(tail)}[v]"

    args = ["-ss", f"{max(0.0, float(src_in)):.3f}", "-i", clip_name]
    if fg_name:
        args += ["-i", fg_name]
    args += ["-filter_complex", chain, "-map", "[v]", "-an",
             "-t", f"{duration:.3f}",
             "-c:v", str(v.video_codec), "-preset", str(v.preset), "-crf", str(v.crf),
             "-pix_fmt", "yuv420p", "-r", str(fps),
             "-movflags", "+faststart", out_name]
    video.run_ffmpeg(args, cwd=workdir, desc=out_name)
    return workdir / out_name


def encode_still_shot(
    workdir: Path,
    bg_name: str,
    fg_name: str,
    out_name: str,
    size: tuple[int, int],
    duration: float,
    cfg: Config,
    *,
    fade_in: float = 0.0,
    fade_out: float = 0.0,
    motion_mode: int = 0,
    fonts_dir: str = "C:/Windows/Fonts",
) -> Path:
    """静态图镜头（回退画面）：直接复用原来的 `video.encode_segment`，不重写。

    传 `audio=None` / `ass_name=None` —— 语音和字幕都留到最后统一贴（护栏 12）。
    """
    return video.encode_segment(workdir, bg_name, fg_name, None, None, out_name,
                                size, duration, cfg, fade_in=fade_in, fade_out=fade_out,
                                motion_mode=motion_mode, fonts_dir=fonts_dir)


def _rel(p: Path, base: Path) -> str:
    """相对路径 + 正斜杠（ffmpeg 输入用相对写法最省事，避开 Windows 的转义坑）。

    踩过：这里直接传 `bg.name`（文件名）→ ffmpeg 在 workdir 里找不到文件，
    因为底图/叠字层写在 slide_root 里，只有**相对 workdir 的路径**才对得上。
    """
    import os
    try:
        r = os.path.relpath(str(p), str(base))
    except ValueError:      # 不同盘符，只能给绝对路径
        r = str(p)
    return r.replace("\\", "/")


def encode_comic_shot(
    workdir: Path,
    sheet: Path,
    out_name: str,
    size: tuple[int, int],
    duration: float,
    cfg: Config,
    *,
    slide_root: Path,
    stem: str,
    kicker: str = "",
    title: str = "",
    caption: str = "",
    callout: str = "",
    nametag: str = "",
    credit: str = "",
    layout: str = "2x2",
    fade_in: float = 0.0,
    fade_out: float = 0.0,
    motion_mode: int = 0,
) -> Path:
    """四格漫画镜头：一张 2x2 切四格，**一格一格轮流上屏**（每格 duration/格数）。

    为什么不让整张一起上屏：一格在手机屏上只有半屏高的一半，四格同屏等于四张小图，
    谁也看不清；一格一格上（每格 1.5-2 秒）才有"翻连环画"的节奏，也把 7 秒用满了。

    每格走的还是**静态图那条链路**（`media.build_layers` 出底图+叠字 → `encode_still_shot`），
    所以缓移、字幕带、标注位置全都和别的镜头一致，不另起一套。

    章节大标题只在**第一格**上（四格都顶着标题就成了刷屏）。
    """
    from . import comic as comic_mod
    from . import media

    fit = str(cfg.comic.get("fit") or "blurpad")
    if str(cfg.comic.get("mode") or "panels").lower() == "sheet":
        # 整张四格一起展示（不做"一格格推进"）。四格同屏在手机上确实偏小，
        # 但讲"四格之间的呼应/对比"时它更合适 —— 留给配置，别在代码里一棍子打死。
        bg0, fg0 = media.build_layers(
            slide_root / f"{stem}_sheet_bg.jpg", slide_root / f"{stem}_sheet_fg.png",
            size, cfg, image_path=sheet, kicker=kicker, title=title, caption=caption,
            callout=callout, nametag=nametag, credit=credit, fit=fit)
        encode_still_shot(workdir, _rel(bg0, workdir), _rel(fg0, workdir), out_name, size,
                          duration, cfg, motion_mode=motion_mode,
                          fade_in=fade_in, fade_out=fade_out)
        return workdir / out_name

    panels = comic_mod.split_panels(sheet, layout)
    per = duration / max(1, len(panels))
    names: list[str] = []
    for j, panel in enumerate(panels, 1):
        pjpg = slide_root / f"{stem}_p{j}.jpg"
        panel.convert("RGB").save(pjpg, quality=95)
        bg, fg = media.build_layers(
            slide_root / f"{stem}_p{j}_bg.jpg", slide_root / f"{stem}_p{j}_fg.png",
            size, cfg, image_path=pjpg, kicker=kicker,
            title=title if j == 1 else "", caption=caption,
            callout=callout, nametag=nametag, credit=credit, fit=fit)
        pname = f"{stem}_p{j}.mp4"
        encode_still_shot(workdir, _rel(bg, workdir), _rel(fg, workdir), pname, size, per, cfg,
                          motion_mode=motion_mode + j,
                          fade_in=fade_in if j == 1 else 0.0,
                          fade_out=fade_out if j == len(panels) else 0.0)
        names.append(pname)
    return video.concat_segments(workdir, names, out_name)


def finish_scene(
    workdir: Path,
    video_name: str,
    audio_name: str | None,
    ass_name: str | None,
    out_name: str,
    size: tuple[int, int],
    duration: float,
    cfg: Config,
    *,
    fade_in: float = 0.0,
    fade_out: float = 0.0,
    fonts_dir: str = "C:/Windows/Fonts",
) -> Path:
    """整场画面拼好后：贴回整条语音 + 烧字幕 + 首尾淡化（一遍编码搞定）。

    `audio_name` / `ass_name` 都必须是**相对 workdir 的文件名**（跟 `encode_segment`
    的约定一致）。别传绝对路径：`subtitles=f=...` 里的反斜杠和冒号会被 ffmpeg
    的滤镜解析器吃掉，报出来的错还很不像人话（实测：
    `No option name near 'Resourcescodehistorical_story_gendataset...'`）。
    这里的断言就是为了让这个错误当场暴露，而不是等到 ffmpeg 那里。
    """
    for label, name in (("ass_name", ass_name), ("audio_name", audio_name)):
        if name and (Path(str(name)).is_absolute() or "/" in str(name) or "\\" in str(name)):
            raise ValueError(
                f"{label} 必须是相对 {workdir} 的文件名，收到 {name!r}。"
                f"先把文件拷进 {workdir.name} 再传文件名。")
    v = cfg.video
    w, h = size
    fps = int(v.fps)
    tail: list[str] = []
    if ass_name:
        fd = fonts_dir.replace("\\", "/").replace(":", "\\:")
        tail.append(f"subtitles=f={ass_name}:fontsdir='{fd}'")
    if fade_in > 0:
        tail.append(f"fade=t=in:st=0:d={fade_in:.3f}")
    if fade_out > 0:
        tail.append(f"fade=t=out:st={max(0.0, duration - fade_out):.3f}:d={fade_out:.3f}")
    tail.append("format=yuv420p")
    chain = "[0:v]" + ",".join(tail) + "[v]"

    args = ["-i", video_name]
    if audio_name:
        args += ["-i", str(audio_name)]
    else:
        # 没有语音也要补等长静音，否则成片音轨比视频短（片尾踩过这个坑）
        silence = video.make_silence(workdir / f"_{Path(out_name).stem}_silence.m4a",
                                     duration, cfg)
        args += ["-i", silence.name]
    args += ["-filter_complex", chain, "-map", "[v]", "-map", "1:a",
             "-t", f"{duration:.3f}",
             "-c:v", str(v.video_codec), "-preset", str(v.preset), "-crf", str(v.crf),
             "-pix_fmt", "yuv420p", "-r", str(fps)]
    acodec = str(v.get("audio_codec", "aac"))
    sr = str(v.get("audio_sample_rate", 48000))
    if acodec in ("libmp3lame", "ac3"):
        args += ["-c:a", acodec, "-b:a", str(v.audio_bitrate)]
    else:
        args += ["-c:a", "aac", "-b:a", str(v.audio_bitrate), "-profile:a", "aac_low"]
    args += ["-ar", sr, "-ac", "2", "-movflags", "+faststart", out_name]
    video.run_ffmpeg(args, cwd=workdir, desc=out_name)
    return workdir / out_name


def build_scene_image(workdir: Path, name: str, size: tuple[int, int], cfg: Config, *,
                      image_path: Path | None, kicker: str, title: str, caption: str,
                      credit: str = "") -> tuple[Path, Path]:
    """静态图镜头的背景层 + 标注层（走 media.build_layers，和原来一致）。"""
    return media.build_layers(workdir / f"{name}_bg.jpg", workdir / f"{name}_fg.png",
                              size, cfg, image_path=image_path, kicker=kicker,
                              title=title, caption=caption, credit=credit)
