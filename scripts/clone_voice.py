"""用一段参考音频克隆音色（MiniMax voice_clone），拿到可长期复用的 voice_id。

为什么放在项目里：换音色这件事会反复做（试不同主播音色、复刻自己的声音），
每次手敲 curl 容易漏掉「时长/体积门槛」和「voice_id 重名」这两个坑。

流程（两步，MiniMax 官方要求）：
    1. POST {base}/files/upload   multipart，purpose=voice_clone → 拿 file_id
    2. POST {base}/voice_clone    {"file_id":..., "voice_id":...} → 克隆完成

克隆出来的 voice_id 直接填进 config.yaml：
    tts.minimax.voice_id: "<voice_id>"
之后 `run.bat` 全流程、`run.bat probe-tts` 校准语速，都会用这个音色。

用法：
    python scripts/clone_voice.py --check                 # 只校验参考音频，不调接口（零成本）
    python scripts/clone_voice.py --list                  # 列出账号下已有的克隆音色
    python scripts/clone_voice.py --voice-id hist_story_v1
    python scripts/clone_voice.py --audio data/base_voice/xxx.mp3 --voice-id my_v2

注意：
  · 参考音频必须是**你有权使用**的声音（本人声音 / 已获授权）。
  · 克隆出来的音色挂在你的 MiniMax 账号下，换 key 就没了。
  · voice_id 在同一账号内必须唯一，重名会直接被接口拒掉。
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from hsg.config import ApiKeys, load_config  # noqa: E402

# MiniMax 对参考音频的硬性门槛
MIN_SECONDS = 10.0
MAX_SECONDS = 300.0            # 5 分钟
MAX_MB = 20.0
OK_SUFFIX = (".mp3", ".m4a", ".wav")


# ---------------------------------------------------------------- 校验
def probe_duration(path: Path) -> float:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(path)],
            capture_output=True, text=True, timeout=60, check=True,
        ).stdout.strip()
        return float(out)
    except Exception:  # noqa: BLE001
        return 0.0


def probe_streams(path: Path) -> dict:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=codec_name,sample_rate,channels,bit_rate",
             "-of", "json", str(path)],
            capture_output=True, text=True, timeout=60, check=True,
        ).stdout
        streams = (json.loads(out or "{}").get("streams") or [{}])
        return streams[0] if streams else {}
    except Exception:  # noqa: BLE001
        return {}


def validate(path: Path) -> tuple[bool, list[str]]:
    """返回 (是否通过, 问题列表)。问题会明确说该拿什么音频来换。"""
    problems: list[str] = []
    if not path.exists():
        return False, [f"文件不存在：{path}"]
    if path.suffix.lower() not in OK_SUFFIX:
        problems.append(f"格式 {path.suffix} 不在允许列表 {OK_SUFFIX} 内")
    size_mb = path.stat().st_size / 1048576
    if size_mb > MAX_MB:
        problems.append(f"体积 {size_mb:.1f}MB 超过 {MAX_MB:.0f}MB")
    dur = probe_duration(path)
    if dur <= 0:
        problems.append("读不出时长（文件损坏或 ffprobe 缺失）")
    elif dur < MIN_SECONDS:
        problems.append(f"时长 {dur:.1f}s 不足 {MIN_SECONDS:.0f}s —— "
                        f"建议拿一段 30-60 秒、单人、无背景音乐的干声")
    elif dur > MAX_SECONDS:
        problems.append(f"时长 {dur:.1f}s 超过 {MAX_SECONDS:.0f}s")
    return (not problems), problems


def describe(path: Path) -> None:
    dur = probe_duration(path)
    st = probe_streams(path)
    print(f"参考音频：{path}")
    print(f"  时长 {dur:.2f}s　体积 {path.stat().st_size / 1024:.0f}KB　"
          f"编码 {st.get('codec_name')}　{st.get('sample_rate')}Hz　"
          f"{st.get('channels')} 声道")
    print(f"  门槛：{MIN_SECONDS:.0f}s ~ {MAX_SECONDS:.0f}s，≤ {MAX_MB:.0f}MB，"
          f"{'/'.join(OK_SUFFIX)}")


# ---------------------------------------------------------------- 接口
def _base_resp(data: dict) -> tuple[int, str]:
    b = (data or {}).get("base_resp") or {}
    try:
        code = int(b.get("status_code", 0) or 0)
    except (TypeError, ValueError):
        code = 0
    return code, str(b.get("status_msg") or "")


def upload_file(cfg, keys: ApiKeys, path: Path) -> int:
    import httpx

    base = str(cfg.tts.minimax.base_url).rstrip("/")
    mime = mimetypes.guess_type(path.name)[0] or "audio/mpeg"
    with httpx.Client(timeout=180.0, follow_redirects=True) as c:
        r = c.post(
            f"{base}/files/upload",
            headers={"Authorization": f"Bearer {keys.for_provider('minimax')}"},
            data={"purpose": "voice_clone"},
            files={"file": (path.name, path.read_bytes(), mime)},
        )
        if r.status_code >= 400:
            raise RuntimeError(f"上传失败 HTTP {r.status_code}: {r.text[:400]}")
        data = r.json()
    code, msg = _base_resp(data)
    if code != 0:
        raise RuntimeError(f"上传失败 status_code={code} {msg}")
    fid = (data.get("file") or {}).get("file_id")
    if not fid:
        raise RuntimeError(f"上传成功但没拿到 file_id：{str(data)[:300]}")
    # ★ file_id 必须保持**整数**传给 voice_clone：转成字符串会被接口拒掉，
    #   报的却是 2013 invalid params（实测踩过，坑在「错误信息完全指不到点上」）。
    if isinstance(fid, str) and fid.isdigit():
        fid = int(fid)
    print(f"  上传完成：file_id={fid}（{type(fid).__name__}）")
    return fid


def list_cloned(cfg, keys: ApiKeys) -> list[dict]:
    import httpx

    base = str(cfg.tts.minimax.base_url).rstrip("/")
    with httpx.Client(timeout=60.0, follow_redirects=True) as c:
        r = c.post(
            f"{base}/get_voice",
            headers={"Authorization": f"Bearer {keys.for_provider('minimax')}",
                     "Content-Type": "application/json"},
            json={"voice_type": "voice_cloning"},
        )
        if r.status_code >= 400:
            raise RuntimeError(f"get_voice 失败 HTTP {r.status_code}: {r.text[:300]}")
        data = r.json()
    code, msg = _base_resp(data)
    if code != 0:
        raise RuntimeError(f"get_voice 失败 status_code={code} {msg}")
    return list(data.get("voice_cloning") or data.get("system_voice") or [])


def clone(cfg, keys: ApiKeys, file_id: int, voice_id: str) -> None:
    import httpx

    base = str(cfg.tts.minimax.base_url).rstrip("/")
    with httpx.Client(timeout=180.0, follow_redirects=True) as c:
        r = c.post(
            f"{base}/voice_clone",
            headers={"Authorization": f"Bearer {keys.for_provider('minimax')}",
                     "Content-Type": "application/json"},
            json={"file_id": file_id, "voice_id": voice_id},
        )
        if r.status_code >= 400:
            raise RuntimeError(f"克隆失败 HTTP {r.status_code}: {r.text[:400]}")
        data = r.json()
    code, msg = _base_resp(data)
    if code != 0:
        raise RuntimeError(
            f"克隆失败 status_code={code} {msg}\n"
            f"  常见原因：voice_id 重名（换一个）／账号未开通音色克隆／"
            f"参考音频不合规（时长/体积/多人声）"
        )


# ---------------------------------------------------------------- 主流程
def main() -> int:
    ap = argparse.ArgumentParser(description="用参考音频克隆 MiniMax 音色")
    ap.add_argument("--audio", help="参考音频路径（默认取 data/base_voice 下最新一个）")
    ap.add_argument("--voice-id", dest="voice_id", help="克隆出的音色 ID（唯一）")
    ap.add_argument("--check", action="store_true", help="只校验参考音频，不调接口")
    ap.add_argument("--list", action="store_true", help="列出账号下已有的克隆音色")
    args = ap.parse_args()

    cfg = load_config()
    keys = ApiKeys.from_env()
    print(f"MiniMax Key：{keys.sources.get('minimax')}"
          f"　base_url={cfg.tts.minimax.base_url}")

    if args.list:
        try:
            rows = list_cloned(cfg, keys)
        except Exception as exc:  # noqa: BLE001
            print(f"✗ {exc}")
            return 1
        if not rows:
            print("账号下还没有克隆音色。")
            return 0
        print(f"已有 {len(rows)} 个克隆音色：")
        for v in rows:
            print(f"  {v.get('voice_id')}　{v.get('voice_name') or ''}")
        return 0

    audio = Path(args.audio) if args.audio else None
    if audio is None:
        cand = sorted((ROOT / "data" / "base_voice").glob("*"),
                      key=lambda p: p.stat().st_mtime, reverse=True)
        cand = [p for p in cand if p.is_file()]
        if not cand:
            print("✗ data/base_voice 下没有文件，用 --audio 指定参考音频")
            return 1
        audio = cand[0]

    describe(audio)
    ok, problems = validate(audio)
    if not ok:
        print("✗ 参考音频不合格：")
        for p in problems:
            print(f"  · {p}")
        return 1
    print("  ✓ 符合 MiniMax 对参考音频的要求")
    if args.check:
        print("（--check 模式，未调用接口）")
        return 0

    voice_id = args.voice_id
    if not voice_id:
        print("✗ 需要 --voice-id（克隆音色 ID，同一账号内唯一，建议英文小写下划线）")
        return 1

    print("\n[1/2] 上传参考音频")
    try:
        file_id = upload_file(cfg, keys, audio)
    except Exception as exc:  # noqa: BLE001
        print(f"✗ {exc}")
        return 1

    print(f"[2/2] 克隆音色 → {voice_id}")
    try:
        clone(cfg, keys, file_id, voice_id)
    except Exception as exc:  # noqa: BLE001
        print(f"✗ {exc}")
        return 1

    print(f"\n✓ 音色克隆完成：{voice_id}")
    print("  填进 config.yaml：")
    print(f"    tts:\n      minimax:\n        voice_id: \"{voice_id}\"")
    print("  换音色后语速换算会变，记得重新校准：run.bat probe-tts")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
