"""语音合成。主用 MiniMax TTS（/v1/t2a_v2，返回 hex 编码音频），
失败时可按配置回退到免费的 edge-tts。

粒度是「分镜」：一个分镜一段文字一次合成，画面切换点天然落在语音停顿上。
"""

from __future__ import annotations

import hashlib
import logging
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from .config import ApiKeys, Config

log = logging.getLogger("hsg.tts")


def probe_duration(path: Path) -> float:
    """用 ffprobe 精确取音频时长（秒）。"""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(path)],
            capture_output=True, text=True, timeout=60, check=True,
        ).stdout.strip()
        return float(out)
    except Exception as exc:  # noqa: BLE001
        log.warning("ffprobe 取时长失败(%s)，按 16000B/s 估算：%s", path.name, exc)
        try:
            return round(path.stat().st_size / 16000.0, 2)
        except OSError:
            return 0.0


class TTSFailed(RuntimeError):
    """所有分镜的语音都合成失败（余额不足 / 限流 / 没装兜底语音）。"""


class MiniMaxTTS:
    def __init__(self, cfg: Config, keys: ApiKeys | None = None):
        self.cfg = cfg
        self.keys = keys or ApiKeys.from_env()
        sub = cfg.tts.minimax
        self.base_url = str(sub.base_url).rstrip("/")
        self.model = str(sub.model)
        self.voice_id = str(sub.voice_id)
        self.speed = float(sub.get("speed", 1.0))
        self.vol = float(sub.get("vol", 1.0))
        self.pitch = int(sub.get("pitch", 0))
        self.sample_rate = int(sub.get("sample_rate", 32000))
        self.bitrate = int(sub.get("bitrate", 128000))
        self.audio_format = str(sub.get("format", "mp3"))
        self.language_boost = str(sub.get("language_boost", "Chinese"))
        self.timeout = float(cfg.tts.get("timeout", 180))
        self.api_key = self.keys.for_provider("minimax")
        self._client = httpx.Client(timeout=self.timeout)

    def close(self) -> None:
        self._client.close()

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, max=20), reraise=True)
    def synth(self, text: str, out_path: Path, *, speed: float | None = None) -> Path:
        payload = {
            "model": self.model,
            "text": text,
            "stream": False,
            "language_boost": self.language_boost,
            "voice_setting": {
                "voice_id": self.voice_id,
                "speed": self.speed if speed is None else speed,
                "vol": self.vol,
                "pitch": self.pitch,
            },
            "audio_setting": {
                "sample_rate": self.sample_rate,
                "bitrate": self.bitrate,
                "format": self.audio_format,
                "channel": 1,
            },
        }
        resp = self._client.post(
            f"{self.base_url}/t2a_v2",
            json=payload,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
        )
        resp.raise_for_status()
        data = resp.json()
        base = data.get("base_resp") or {}
        if base.get("status_code") not in (0, None):
            raise RuntimeError(
                f"MiniMax TTS 失败: {base.get('status_code')} {base.get('status_msg')}"
            )
        audio_hex = (data.get("data") or {}).get("audio") or ""
        if not audio_hex:
            raise RuntimeError(f"MiniMax TTS 未返回音频: {str(data)[:300]}")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(bytes.fromhex(audio_hex))
        return out_path

    def voices(self) -> list[dict]:
        """列出可用音色（换音色时用它确认 voice_id 拼写）。"""
        try:
            r = self._client.post(
                f"{self.base_url}/get_voice",
                json={"voice_type": "all"},
                headers={"Authorization": f"Bearer {self.api_key}",
                         "Content-Type": "application/json"},
            )
            r.raise_for_status()
            return (r.json().get("system_voice") or [])
        except Exception as exc:  # noqa: BLE001
            log.warning("获取音色列表失败：%s", exc)
            return []


def edge_synth(text: str, out_path: Path, voice: str = "zh-CN-YunxiNeural",
               speed: float | None = None) -> Path:
    """免费兜底方案（需要 pip install edge-tts）。

    speed 会换算成 edge 的 rate 百分比 —— 不传的话兜底语音就固定按常速念，
    和 MiniMax 侧的语速设定不一致（实测会让时长预估偏掉一截）。
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    import asyncio

    import edge_tts

    rate = None
    if speed and abs(float(speed) - 1.0) > 0.01:
        rate = f"{round((float(speed) - 1.0) * 100):+d}%"

    async def _run() -> None:
        kwargs = {"rate": rate} if rate else {}
        await edge_tts.Communicate(text, voice, **kwargs).save(str(out_path))

    asyncio.run(_run())
    return out_path


class TTS:
    def __init__(self, cfg: Config, keys: ApiKeys | None = None):
        self.cfg = cfg
        self.provider = str(cfg.tts.provider)
        self.fallback = bool(cfg.tts.get("fallback_edge_tts", True))
        self.edge_voice = str(
            (cfg.tts.get("edge") or {}).get("voice_id")
            or cfg.tts.get("edge_voice", "zh-CN-YunxiNeural"))
        # 当前生效 provider 的语速。必须在这里取出来 —— 否则 edge 分支永远拿不到语速，
        # `--tts-provider edge --speed 1.1` 会静默按常速念（实测踩过）。
        sub = cfg.tts.get(self.provider) or {}
        self.speed = sub.get("speed")
        self._mm: MiniMaxTTS | None = None
        if self.provider == "minimax":
            self._mm = MiniMaxTTS(cfg, keys)

    def close(self) -> None:
        if self._mm:
            self._mm.close()

    def __enter__(self) -> "TTS":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def synthesize(self, text: str, out_path: Path, *, speed: float | None = None) -> tuple[Path, float]:
        """合成一段语音，返回 (路径, 时长秒)。

        speed 不传时用当前 provider 配置里的语速（minimax 走 voice_setting.speed，
        edge 换算成 rate 百分比）。两条分支统一在这里落定，避免一边生效一边失效。
        """
        text = (text or "").strip()
        if not text:
            raise ValueError("TTS 文本为空")
        eff = self.speed if speed is None else speed
        if out_path.exists() and out_path.stat().st_size > 2048:
            return out_path, probe_duration(out_path)
        try:
            if self.provider == "minimax" and self._mm:
                self._mm.synth(text, out_path, speed=eff)
            else:
                edge_synth(text, out_path, self.edge_voice, speed=eff)
        except Exception as exc:  # noqa: BLE001
            if not (self.fallback and self.provider == "minimax"):
                raise
            log.warning("MiniMax TTS 失败（%s），回退 edge-tts（免费，音色 %s）",
                        exc, self.edge_voice)
            out_path = out_path.with_suffix(".edge.mp3")
            edge_synth(text, out_path, self.edge_voice, speed=eff)
        dur = probe_duration(out_path)
        log.info("语音 %s：%.2fs / %d 字（%.2f 字/秒）",
                 out_path.name, dur, len(text), len(text) / max(0.01, dur))
        return out_path, dur


def audio_tag(text: str, voice_id: str = "", speed: float | None = None) -> str:
    """音频缓存的 key：**文本 + 音色 + 语速** 一起哈希。

    只按文本命名会出事：改了语速或换了音色再跑，文字没变就会命中旧音频，
    听起来还是上个设定的声音（实测踩过 —— probe-tts --speed 1.1 量回来的
    还是 1.0 倍速的时长，因为它读到了缓存文件）。
    """
    key = f"{voice_id}|{'' if speed is None else f'{float(speed):.3f}'}|{text or ''}"
    return hashlib.md5(key.encode("utf-8")).hexdigest()[:8]


def synthesize_all(scenes: list, cfg: Config, audio_dir: Path, tts: TTS) -> tuple[float, int]:
    """并发合成所有分镜语音，把结果写回 scene。返回 (总时长, 成功数)。

    缓存 key 带文本哈希与音色/语速：改写后分镜号不变但文字变了，
    只按分镜号命名会让新稿复用到旧音频（字幕和语音对不上）。
    """
    audio_dir.mkdir(parents=True, exist_ok=True)
    workers = max(1, int(cfg.tts.get("concurrency", 4)))
    sub = cfg.tts[str(cfg.tts.provider)]
    voice = str(sub.get("voice_id") or "")
    speed = sub.get("speed")

    def _one(scene) -> float:
        tag = audio_tag(scene.text, voice, speed)
        out = audio_dir / f"scene_{scene.index:03d}_{tag}.mp3"
        try:
            path, dur = tts.synthesize(scene.text, out)
            scene.audio_path = path
            scene.duration = dur
            # 兜底产物文件名带 .edge → 记下来，最后统一告警
            if path.name.endswith(".edge.mp3"):
                fallback_used.append(scene.index)
            return dur
        except Exception as exc:  # noqa: BLE001
            scene.error = str(exc)
            scene.duration = 0.0
            log.error("分镜 %d 合成失败：%s", scene.index, exc)
            return 0.0

    fallback_used: list[int] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(_one, scenes))

    ok = sum(1 for s in scenes if s.duration > 0)
    total = sum(s.duration for s in scenes)
    log.info("语音合成完成：%d/%d 段成功，总时长 %.1fs（%.1f 分钟）",
             ok, len(scenes), total, total / 60)
    if fallback_used:
        # 这条必须显眼：兜底音色**不是**配置里的音色，成片听起来会是另一个人。
        # 2026-09 实测踩过两次（MiniMax 余额耗尽 → 18 段全走 edge，静默换声）。
        log.warning("⚠️ 有 %d/%d 段用了 edge 兜底音色（%s）——成片配音不是配置里的 "
                    "「%s」。看到这条请检查 MiniMax 余额/限流，充值后加 --force 重跑语音。",
                    len(fallback_used), len(scenes), tts.edge_voice, voice)
    return total, ok
