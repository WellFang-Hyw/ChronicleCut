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

import array
import hashlib
import json
import logging
import re
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

    ⚠️ 编码器跟**扩展名**走，不跟 `video.audio_codec` 走：
    把 AAC 塞进 `.mp3` 容器会直接失败，而 ffmpeg 的报错会被吞掉，
    只剩一个看不懂的退出码（冒烟测试里踩到过）。所以：
    `.mp3` → libmp3lame，其余（`.m4a`/`.aac`）→ aac。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    codec = "libmp3lame" if path.suffix.lower() == ".mp3" else "aac"
    # 用 run_ffmpeg 而不是裸 subprocess：失败时会把 ffmpeg 的 stderr 带出来
    run_ffmpeg(["-f", "lavfi", "-i",
                f"anullsrc=r={cfg.video.get('audio_sample_rate', 48000)}:cl=stereo",
                "-t", f"{duration:.3f}", "-c:a", codec, path.name],
               cwd=path.parent, desc=f"静音轨 {path.name}")
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


# ---------------------------------------------------------------- 多曲交替 BGM
# 需求：一期视频里按章节轮换多首 BGM（用户 2026-09-15 指定）。
# 有两件事必须做，否则听感直接崩：
#   ① **响度归一化**：曲目录制电平能差 20 dB（实测候选 3 Relax Beat 整轨
#      mean_volume -33.0 dB、候选 8 Voxscape -12.8 dB）。不归一化，低电平那首
#      在 0.08 音量下等于静音 —— 听感就是「音乐到那一章突然消失了」。
#   ② **章与章之间交叉淡化**：硬切在「436Hz 闷垫」切到「1248Hz」时会咔一下。


def _loudness(path: Path) -> dict:
    """EBU R128 第一遍：只分析不编码，读回输入响度（给两遍法的第二遍用）。"""
    p = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", str(path),
         "-af", "loudnorm=I=-18:TP=-1.5:LRA=11:print_format=json", "-f", "null", "-"],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    m = re.search(r"\{[^{}]*\"input_i\"[\s\S]*?\}",
                  (p.stdout or "") + (p.stderr or ""))
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except ValueError:
        return {}


def normalize_loudness(src: Path, dest: Path, target_i: float = -18.0,
                       target_tp: float = -1.5) -> dict:
    """两遍法把一首曲子归一到统一响度，返回测量记录（留档用）。"""
    first = _loudness(src)
    if first.get("input_i"):
        af = ("loudnorm=I={i}:TP={tp}:LRA=11:measured_I={mi}:measured_TP={mtp}:"
              "measured_LRA={mlra}:measured_thresh={mth}:offset={off}:linear=true"
              ).format(i=target_i, tp=target_tp, mi=first["input_i"],
                       mtp=first["input_tp"], mlra=first["input_lra"],
                       mth=first.get("input_thresh", -70),
                       off=first.get("target_offset", 0))
    else:
        af = f"loudnorm=I={target_i}:TP={target_tp}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    run_ffmpeg(["-i", str(src), "-af", af, "-c:a", "libmp3lame", "-b:a", "192k",
                str(dest)], cwd=dest.parent, desc="loudnorm")
    in_i = float(first.get("input_i") or 0.0)
    rec = {"src": src.name, "out": dest.name, "in_i": first.get("input_i"),
           "in_tp": first.get("input_tp"), "gain_db": round(target_i - in_i, 1)}
    log.info("BGM 响度归一化：%s → %s（输入 %.1f LUFS，增益 %+.1f dB → %.0f LUFS）",
             src.name, dest.name, in_i, rec["gain_db"], target_i)
    return rec


def ensure_normalized(src: Path, norm_dir: Path, target_i: float = -18.0) -> Path:
    """归一化并**缓存**（同一首 + 同一目标只算一次，结果落在 norm_dir）。"""
    dest = norm_dir / f"{src.stem}_norm.mp3"
    if not dest.exists() or dest.stat().st_size < 100_000:
        normalize_loudness(src, dest, target_i)
    return dest


def rms_windows(path: Path, sr: int = 8000, win_s: float = 1.0) -> list[float]:
    """整轨的 1 秒窗 RMS 包络（用来找"哪里真的有声音"）。"""
    p = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(path),
                        "-f", "s16le", "-ac", "1", "-ar", str(sr), "-"],
                       capture_output=True)
    data = array.array("h")
    data.frombytes(p.stdout[:len(p.stdout) // 2 * 2])
    win = int(sr * win_s)
    out: list[float] = []
    for i in range(0, len(data) - win, win):
        blk = data[i:i + win]
        out.append((sum(float(x) * x for x in blk) / win) ** 0.5)
    return out


def safe_window(path: Path) -> tuple[float, float]:
    """一首曲子里「真的有声音」的区间 [起, 止]（秒），避开开头/结尾的静音铺垫。

    为什么必须算：实测候选 8 Voxscape 开头十几秒近乎无声（波形从 0 缓慢淡入）。
    按章节切片时若从 0 开始取，用到它的那一章前 15 秒就是静音 ——
    听感上正是用户要避免的「音乐突然消失」。实测踩到过：
    交替轨第 3 章（Voxscape）开头扫出 8 个以上静音窗。

    判据：1 秒窗 RMS 首次/末次达到中位数 60% 的位置，右端再留 1 秒。
    阈值取 0.6 而不是 0.5：0.5 会把曲子已经明显衰减的尾段也算进"有声区间"，
    切片蹭到那段，交叉淡化处会出现一个 -10 dB 的软塌（实测踩到过）。
    """
    env = rms_windows(path)
    dur = media_duration(path)
    if not env:
        return 0.0, dur
    med = sorted(env)[len(env) // 2]
    hit = [i for i, v in enumerate(env) if v >= med * 0.6]
    if not hit:
        return 0.0, dur
    lo = float(hit[0])
    hi = max(lo + 1.0, float(hit[-1] + 1) - 1.0)
    return lo, min(hi, dur)


def build_playlist_bed(out_dir: Path, playlist: list[Path],
                       spans: list[tuple[float, float]], total: float,
                       cfg: Config) -> Path | None:
    """按章节把多首曲子拼成一条铺满全片的 BGM 轨（章间交叉淡化）。

    spans：每章在 **BGM 时间轴**上的 (起, 止)，0 = 音乐开始那一刻
    （由 pipeline.chapter_spans 算出来）。第 i 章用 playlist[i % N]。
    拼不出来（文件不足 / 时长为 0）就返回 None，让调用方退回单曲模式。
    """
    pl = [p for p in (playlist or []) if p and Path(p).exists()]
    usable = [(float(s), float(e)) for s, e in (spans or []) if e - s > 1.0]
    if len(pl) < 2 or len(usable) < 2:
        return None
    pc = cfg.bgm.get("playlist") or {}
    xf = max(0.0, float(pc.get("crossfade", 2.5)))
    target_i = float(pc.get("loudness", -18.0))
    norm_dir = out_dir / "norm"
    tracks = [ensure_normalized(p, norm_dir, target_i) for p in pl]
    durs = [media_duration(t) for t in tracks]
    if any(d <= 5.0 for d in durs):
        log.warning("BGM 交替：有曲子读不出时长，退回单曲")
        return None
    # 每首的"有声区间"：切片只用这段，避开开头/结尾的静音铺垫
    wins = [safe_window(t) for t in tracks]
    for t, (lo, hi) in zip(tracks, wins):
        if lo > 1.0 or hi < media_duration(t) - 1.0:
            log.info("BGM 交替：%s 的有效区间 %.1fs–%.1fs（头尾静音已避开）",
                     t.name, lo, hi)

    key = hashlib.md5(json.dumps(
        {"tracks": [t.name for t in tracks],
         "spans": [[round(s, 2), round(e, 2)] for s, e in usable],
         "safe": [[round(a, 2), round(b, 2)] for a, b in wins],
         "xf": xf, "total": round(total, 2), "v": 2}).encode()).hexdigest()[:12]
    bed = out_dir / f"_bed_{key}.mp3"
    if bed.exists() and bed.stat().st_size > 100_000:
        log.debug("复用已拼好的 BGM 交替轨：%s", bed.name)
        return bed

    args: list[str] = []
    parts: list[str] = []
    for i, (s, e) in enumerate(usable):
        idx = i % len(pl)
        seg_len = (e - s) + (xf if i < len(usable) - 1 else 0.0)
        # 同一首被轮到第二次时接着上一段往后取，避免每次都从头放同一段；
        # 但起点必须落在"有声区间"内，且整段不能超出区间右端
        # （超了就贴到边界，宁可有重叠也不去取静音头/静音尾）
        lo, hi = wins[idx]
        want = (i // len(pl)) * (e - s)
        off = lo + min(want, max(0.0, (hi - lo) - seg_len - 0.5))
        args += ["-ss", f"{off:.3f}", "-t", f"{seg_len:.3f}", "-i", str(tracks[idx])]
        parts.append(f"[{i}:a]")

    if len(parts) == 1:
        fg = f"{parts[0]}atrim=0:{total:.3f}[out]"
    else:
        fg = (parts[0] + parts[1] +
              f"acrossfade=d={xf:.2f}:c1=tri:c2=tri[a1]")
        for i in range(2, len(parts)):
            fg += f";[a{i - 1}]{parts[i]}acrossfade=d={xf:.2f}:c1=tri:c2=tri[a{i}]"
        fg += f";[a{len(parts) - 1}]atrim=0:{total:.3f}[out]"

    run_ffmpeg([*args, "-filter_complex", fg, "-map", "[out]",
                "-c:a", "libmp3lame", "-b:a", "192k", str(bed)],
               cwd=out_dir, desc="playlist_bed")
    log.info("BGM 交替轨：%s（%d 章 × %d 首，交叉淡化 %.1fs，目标 %.0f LUFS）",
             bed.name, len(usable), len(pl), xf, target_i)
    return bed


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
