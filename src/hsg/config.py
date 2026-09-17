"""配置加载：合并 config.yaml + .env + 系统环境变量（含注册表兜底）。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------- .env
def load_dotenv(path: Path | None = None) -> dict[str, str]:
    """极简 .env 解析（不覆盖已存在的真实环境变量）。"""
    path = path or (PROJECT_ROOT / ".env")
    result: dict[str, str] = {}
    if not path.exists():
        return result
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val
        result[key] = val
    return result


# ------------------------------------------------- 系统环境变量（注册表兜底）
# 为什么需要这个：Hermes / 部分从 GUI 启动的终端会把子进程环境清洗一遍，
# 于是 python 里 os.environ 拿不到系统级变量（实测 DEEPSEEK_API_KEY 长度为 0，
# 但注册表里是有的）。直接读注册表可以绕开这个问题。
_REG_ROOTS = (
    ("HKEY_LOCAL_MACHINE", r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
    ("HKEY_CURRENT_USER", "Environment"),
)


def _registry_env(name: str) -> str:
    try:
        import winreg  # noqa: PLC0415
    except ImportError:
        return ""
    for root_name, sub in _REG_ROOTS:
        root = getattr(winreg, root_name, None)
        if root is None:
            continue
        try:
            with winreg.OpenKey(root, sub) as key:
                val, _ = winreg.QueryValueEx(key, name)
        except OSError:
            continue
        val = str(val or "").strip()
        if val:
            return val
    return ""


def env_get(name: str, default: str = "") -> str:
    """按 环境变量 → .env(已注入 os.environ) → 注册表 的顺序取值。"""
    val = (os.environ.get(name) or "").strip()
    if val:
        return val
    val = _registry_env(name)
    return val or default


# ---------------------------------------------------------------- config
class Config(dict):
    """支持 cfg.llm.provider / cfg["llm"]["provider"] 的配置对象。

    关键：dict 值在**加载时**就递归转成 Config，
    所以 cfg["story"]["chapters"] = 8 是就地修改，CLI 覆盖才真正生效。
    （在 __getitem__ 里临时包装会创建副本，写入就丢了。）
    """

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def get_path(self, key: str) -> Path:
        p = Path(str(self.get(key, "")))
        return p if p.is_absolute() else (PROJECT_ROOT / p)

    def as_dict(self) -> dict:
        def _unwrap(v: Any) -> Any:
            if isinstance(v, dict):
                return {k: _unwrap(x) for k, x in v.items()}
            if isinstance(v, list):
                return [_unwrap(x) for x in v]
            return v

        return {k: _unwrap(v) for k, v in self.items()}

    def save(self, path: Path | None = None) -> None:
        path = path or (PROJECT_ROOT / "config.yaml")
        path.write_text(
            yaml.safe_dump(self.as_dict(), allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )


def _wrap_deep(obj: Any) -> Any:
    if isinstance(obj, dict):
        return Config({k: _wrap_deep(v) for k, v in obj.items()})
    if isinstance(obj, list):
        return [_wrap_deep(v) for v in obj]
    return obj


def load_config(path: Path | None = None) -> Config:
    load_dotenv()
    path = path or (PROJECT_ROOT / "config.yaml")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return _wrap_deep(raw)


# ---------------------------------------------------------------- helpers
@dataclass
class ApiKeys:
    """从环境变量 / .env / 注册表里取各家的 key。"""

    minimax: str = ""
    deepseek: str = ""
    sources: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> "ApiKeys":
        keys = cls(
            minimax=env_get("MINIMAX_API_KEY"),
            deepseek=env_get("DEEPSEEK_API_KEY"),
        )
        for attr in ("deepseek", "minimax"):
            keys.sources[attr] = "已读取" if getattr(keys, attr) else "缺失"
        return keys

    def for_provider(self, provider: str) -> str:
        val = {"minimax": self.minimax, "deepseek": self.deepseek}.get(provider, "")
        if not val:
            raise RuntimeError(
                f"缺少 provider={provider!r} 的 API Key。三种途径任选其一：\n"
                f"  · 系统环境变量（推荐）：setx {provider.upper()}_API_KEY sk-xxx\n"
                f"  · 项目 .env 文件（参考 .env.example）\n"
                f"  · Windows 注册表里的用户/系统环境变量"
            )
        return val

    def describe(self) -> str:
        return "  ".join(f"{k}={v}" for k, v in sorted(self.sources.items()))


def ensure_dirs(cfg: Config) -> None:
    for key in cfg.paths:
        if key.endswith("_dir") or key == "data_dir":
            cfg.paths.get_path(key).mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------- 模型锁定
# 文本模型锁定为 DeepSeek：MiniMax 只允许用于 TTS。
# 想换文本模型 → 改这里的常量 + config.yaml 的 llm.provider，两处必须一致。
# 文本模型：默认 deepseek；允许的 provider 白名单。
# 2026-09-17 用户要求「写稿用 MiniMax、校验用 DeepSeek」——锁从"只许 deepseek"
# 改成"显式开关 + 醒目横幅"：默认不变，要用别的必须同时打开 llm.allow_nondeepseek_text。
TEXT_PROVIDER_DEFAULT = "deepseek"
TEXT_PROVIDERS_ALLOWED = ("deepseek", "minimax")

DEFAULT_CHANNEL_NAME = "历史小故事"


def get_channel(cfg: Config) -> str:
    return str(cfg.video.get("channel_name") or DEFAULT_CHANNEL_NAME)


def assert_text_provider(cfg: Config) -> str:
    """校验文本模型 provider。返回实际使用的 provider。

    这道校验是硬性的：即使有人直接编辑 config.yaml 把 llm.provider 改成别的，
    也会在这里被拦下，而不是悄悄换掉文本模型。
    """
    provider = str(cfg.llm.provider)
    if provider == TEXT_PROVIDER_DEFAULT:
        return provider
    if provider not in TEXT_PROVIDERS_ALLOWED:
        raise RuntimeError(
            f"文本模型只支持 {TEXT_PROVIDERS_ALLOWED}，当前配置是 {provider!r}。")
    if not bool(cfg.llm.get("allow_nondeepseek_text", False)):
        raise RuntimeError(
            f"文本模型当前是 {provider!r}，不是默认的 {TEXT_PROVIDER_DEFAULT!r}。\n"
            f"  · 默认只用 DeepSeek 写稿（2026-09-15 定的规矩），要用别的必须显式确认：\n"
            f"    在 config.yaml 的 llm 段写 allow_nondeepseek_text: true\n"
            f"  · 校验模型是另一条线（verify.provider），默认仍是 DeepSeek ——\n"
            f"    写稿与校验用不同模型，才能互相挑错。"
        )
    return provider


def verify_provider(cfg: Config) -> str:
    """校验（史实审校/复检）用哪个模型：默认 deepseek，与写稿模型相互独立。"""
    p = str(cfg.verify.get("provider") or TEXT_PROVIDER_DEFAULT)
    if p not in TEXT_PROVIDERS_ALLOWED:
        raise RuntimeError(f"verify.provider 只支持 {TEXT_PROVIDERS_ALLOWED}，收到 {p!r}")
    return p


def provider_banner(cfg: Config) -> str:
    """每次运行都打印的分工横幅 —— 一眼看清谁在干活。"""
    llm_sub = cfg.llm[str(cfg.llm.provider)]
    tts_sub = cfg.tts[str(cfg.tts.provider)]
    return (
        f"写稿模型 (选题/大纲/写稿)      : {cfg.llm.provider} / {llm_sub.get('model')}\n"
        f"校验模型 (史实审校/复检)        : {verify_provider(cfg)}\n"
        f"语音合成 (TTS)                : {cfg.tts.provider} / {tts_sub.get('voice_id')}"
    )
