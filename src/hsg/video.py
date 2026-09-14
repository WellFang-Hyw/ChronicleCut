"""用 ffmpeg 合成视频。

设计要点：
  · 一个片段 = 背景层（缓移）+ 前景层（文字，不动）+ 一段语音 + 一条 ASS 字幕
  · 片段时长严格 = 语音时长 + tail_padding，绝不让语音被切掉
  · 转场用「淡入淡出到黑」，章节之间用；章节内部硬切。
    不用 xfade：那需要全片重编码、滤镜链脆弱，而淡黑在片段编码阶段就完成了，
    之后 concat -c copy 是秒级拼接。
  · ffmpeg 一律以「片段目录」为 cwd、只用相对文件名，
    规避 Windows 路径转义地狱（详见 skill: tts-video-pipeline）。
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

from .config import Config

log = logging.getLogger("hsg.video")


class FFmpegError(RuntimeError):
    pass


def check_ffmpeg() -> None:
    for exe in ("ffmpeg", "ffprobe"):
        if shutil.which(exe) is None:
            raise FFmpegError(f"找不到 {exe}，请先安装并加入 PATH")


def run_ffmpeg(args: list[str], cwd: Path, desc: str = "") -> None:
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args]
    log.debug("ffmpeg(%s): %s", desc, " ".join(cmd))
    proc = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True)
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip()[-1600:]
        raise FFmpegError(f"ffmpeg 失败（{desc}）：\n{tail}")


def media_duration(path: Path) -> float:
    p = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, text=True,
    )
    try:
        return float((p.stdout or "0").strip())
    except ValueError:
        return 0.0


def make_silence(path: Path, duration: float, cfg: Config) -> Path:
    """生成一段静音轨。

    为什么需要：片尾卡片没有口播。如果那一段干脆不带音轨，
    拼接出来的成片音轨会比视频短（实测 443.1s vs 447.1s）——
    有些播放器会在音轨结束时停住，最后几秒的片尾就看不到了。
    补一条等长静音轨，两条流长度一致。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i",
         f"anullsrc=r={cfg.video.get('audio_sample_rate', 48000)}:cl=stereo",
         "-t", f"{duration:.3f}", "-c:a",
         "aac" if str(cfg.video.get("audio_codec", "aac")) == "aac" else "libmp3lame",
         str(path)],
        check=True, capture_output=True,
    )
    return path


def motion_expr(mode: int, cfg: Config, duration: float) -> tuple[str, str]:
    """背景缓移表达式。mode 轮换四种方向，画面不会显得单调。

    背景层比画布大 motion_scale 倍，所以 x∈[0, iw-ow]、y∈[0, ih-oh] 有富余空间。
    """
    d = max(0.1, duration)
    if mode % 4 == 0:
        return f"(iw-ow)*t/{d:.3f}", "(ih-oh)/2"
    if mode % 4 == 1:
        return f"(iw-ow)*(1-t/{d:.3f})", "(ih-oh)/2"
    if mode % 4 == 2:
        return "(iw-ow)/2", f"(ih-oh)*t/{d:.3f}"
    return "(iw-ow)/2", f"(ih-oh)*(1-t/{d:.3f})"


def encode_segment(
    workdir: Path,
    bg_name: str,
    fg_name: str,
    audio_name: str | None,
    ass_name: str | None,
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
    """把一个分镜（背景层 + 前景层 + 语音 + 字幕）编成 mp4。"""
    v = cfg.video
    w, h = size
    fps = int(v.fps)

    xexpr, yexpr = ("0", "0")
    if bool(v.get("motion", True)):
        xexpr, yexpr = motion_expr(motion_mode, cfg, duration)

    chain = [f"[0:v]crop={w}:{h}:x='{xexpr}':y='{yexpr}',setsar=1,fps={fps}[bgv]",
             "[1:v]format=rgba,setsar=1[fgo]",
             "[bgv][fgo]overlay=0:0:format=auto"]
    tail: list[str] = []
    if ass_name:
        fd = fonts_dir.replace("\\", "/").replace(":", "\\:")
        tail.append(f"subtitles=f={ass_name}:fontsdir='{fd}'")
    if fade_in > 0:
        tail.append(f"fade=t=in:st=0:d={fade_in:.3f}")
    if fade_out > 0:
        tail.append(f"fade=t=out:st={max(0.0, duration - fade_out):.3f}:d={fade_out:.3f}")
    tail.append("format=yuv420p")
    # 注意：tail 必须接到上一条链的**同一条链**里（用逗号），
    # 不能当成新元素 append —— 那样会被 ";" 分隔成一条孤立滤镜链，
    # ffmpeg 会报 “Cannot find an unused video input stream to feed
    # the unlabeled input pad subtitles:default”。
    chain[-1] = chain[-1] + "," + ",".join(tail) + "[v]"
    filter_complex = ";".join(chain)

    args = ["-loop", "1", "-framerate", str(fps), "-i", bg_name,
            "-loop", "1", "-framerate", str(fps), "-i", fg_name]
    if audio_name:
        args += ["-i", audio_name]

    af: list[str] = []
    if audio_name:
        if fade_in > 0:
            af.append(f"afade=t=in:st=0:d={fade_in:.3f}")
        if fade_out > 0:
            af.append(f"afade=t=out:st={max(0.0, duration - fade_out):.3f}:d={fade_out:.3f}")
        af.append("apad")
        filter_complex += f";[2:a]{','.join(af)}[a]"

    args += ["-filter_complex", filter_complex, "-map", "[v]"]
    if audio_name:
        args += ["-map", "[a]"]
    args += ["-t", f"{duration:.3f}",
             "-c:v", str(v.video_codec), "-preset", str(v.preset), "-crf", str(v.crf),
             "-pix_fmt", "yuv420p", "-r", str(fps)]
    if audio_name:
        acodec = str(v.get("audio_codec", "aac"))
        sr = str(v.get("audio_sample_rate", 48000))
        if acodec in ("libmp3lame", "ac3"):
            args += ["-c:a", acodec, "-b:a", str(v.audio_bitrate)]
        else:
            args += ["-c:a", "aac", "-b:a", str(v.audio_bitrate), "-profile:a", "aac_low"]
        args += ["-ar", sr, "-ac", "2"]
    args += ["-movflags", "+faststart", out_name]

    run_ffmpeg(args, cwd=workdir, desc=out_name)
    return workdir / out_name


def concat_segments(workdir: Path, segment_names: list[str], out_name: str) -> Path:
    """无损拼接（要求所有片段编码参数一致）。"""
    if not segment_names:
        raise FFmpegError("没有可拼接的片段")
    if len(segment_names) == 1:
        src, dst = workdir / segment_names[0], workdir / out_name
        if src.resolve() != dst.resolve():
            shutil.copyfile(src, dst)
        return dst
    lst = workdir / "_concat_list.txt"
    lst.write_text("\n".join(f"file '{n}'" for n in segment_names) + "\n", encoding="utf-8")
    run_ffmpeg(["-f", "concat", "-safe", "0", "-i", lst.name, "-c", "copy",
                "-movflags", "+faststart", out_name], cwd=workdir, desc="concat")
    return workdir / out_name


def mix_bgm(
    workdir: Path,
    video_name: str,
    bgm_path: Path,
    out_name: str,
    cfg: Config,
    duration: float,
    start_at: float = 0.0,
) -> Path:
    """把背景音乐混进成片（人声为主、音乐垫底）。

    要点（每一条都是踩过的坑）：
      · `-stream_loop -1` 让音乐不够长时自动循环铺满
      · `adelay` 把音乐整体后移 → 实现「开场白念完再进 BGM」
      · `amix=duration=first` 以人声轨为准，音乐不会撑长视频
      · `normalize=0` 必须有 —— amix 默认按输入数归一化，
        会把**人声也压掉一半**，听感是「配音变小了」
      · 视频流 `-c:v copy`，不重编码，几十秒完成
    """
    bgm = cfg.bgm
    vol = float(bgm.get("volume", 0.08))
    fi = float(bgm.get("fade_in", 2.5))
    fo = float(bgm.get("fade_out", 4.0))
    loop = bool(bgm.get("loop", True))
    acodec = str(cfg.video.get("audio_codec", "aac"))
    sr = str(cfg.video.get("audio_sample_rate", 48000))

    start_at = max(0.0, min(float(start_at), max(0.0, duration - 1.0)))
    fade_out_start = max(start_at, duration - fo)

    # 输入顺序：BGM 是 input 0（-stream_loop 是 per-input 选项），视频是 input 1
    chain = [f"volume={vol:.4f}"]
    if start_at > 0:
        chain.append(f"adelay=delays={int(round(start_at * 1000))}:all=1")
    if fi > 0:
        chain.append(f"afade=t=in:st={start_at:.2f}:d={fi:.2f}")
    if fo > 0:
        chain.append(f"afade=t=out:st={fade_out_start:.2f}:d={fo:.2f}")
    chain += ["apad", f"atrim=0:{duration:.3f}"]
    fg = (f"[0:a]{','.join(chain)}[bgm];"
          "[1:a][bgm]amix=inputs=2:duration=first:normalize=0[a]")
    log.debug("BGM filtergraph: %s", fg)

    args: list[str] = []
    if loop:
        args += ["-stream_loop", "-1"]
    args += ["-i", str(bgm_path), "-i", video_name,
             "-filter_complex", fg, "-map", "1:v:0", "-map", "[a]", "-c:v", "copy"]
    if acodec in ("libmp3lame", "ac3"):
        args += ["-c:a", acodec, "-b:a", str(cfg.video.audio_bitrate)]
    else:
        args += ["-c:a", "aac", "-b:a", str(cfg.video.audio_bitrate), "-profile:a", "aac_low"]
    args += ["-ar", sr, "-ac", "2", "-t", f"{duration:.3f}",
             "-movflags", "+faststart", out_name]

    run_ffmpeg(args, cwd=workdir, desc="mix_bgm")
    log.info("背景音乐已混入：%s（音量 %.2f，%s，淡出 %.1fs）", bgm_path.name, vol,
             f"{start_at:.1f}s 处淡入 {fi:.1f}s" if start_at > 0 else f"开头淡入 {fi:.1f}s", fo)
    return workdir / out_name


def probe_streams(path: Path) -> dict:
    """读回成片的视频/音频参数（自检用：确认分辨率、帧率、音轨都存在）。"""
    p = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json",
         "-show_streams", "-show_format", str(path)],
        capture_output=True, text=True,
    )
    import json

    try:
        data = json.loads(p.stdout or "{}")
    except ValueError:
        return {}
    out = {"duration": float((data.get("format") or {}).get("duration") or 0)}
    for s in data.get("streams") or []:
        kind = s.get("codec_type")
        if kind == "video":
            out["video"] = {"codec": s.get("codec_name"), "width": s.get("width"),
                            "height": s.get("height"), "fps": s.get("r_frame_rate"),
                            "frames": s.get("nb_frames")}
        elif kind == "audio":
            out["audio"] = {"codec": s.get("codec_name"),
                            "sample_rate": s.get("sample_rate"),
                            "channels": s.get("channels")}
    return out
