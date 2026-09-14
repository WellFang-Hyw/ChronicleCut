"""配置审计：把「代码真的读了哪些键」和「config.yaml 里写了哪些键」对一遍。

为什么需要：项目里有 10 个「写在配置里、代码从来不读」的假开关
（per_scene / granularity / outro_card / keep_intermediate …）。它们比没有配置更糟 ——
使用者照着注释调了半天，其实一点作用都没有。这个工具用来把这类键找出来，
以及反向找出「代码会读、但配置里没写」的键（这些依赖代码里的默认值）。

用法：
    python scripts/audit_config.py            # 全部小节
    python scripts/audit_config.py images     # 只看某一节

两个已知的误报来源（都不是 bug，是名字撞车）：
  · ① 栏会把「子段」也算成配置键（如 llm.deepseek）—— 它们是
    `cfg.llm[provider]` 动态取的，静态扫不到。所以 ① 会显式跳过 dict 值。
  · ② 栏会漏进一些同名局部变量的属性（images.py 里 `im` 既指 cfg.images、
    又指接口返回的图片字典，于是 `im.get("print")` 被当成 images.print）。
    看 ② 时要留意这类同形名，别急着往配置里加键。

实现上的一个坑：`story` 既是配置小节名、又是代码里到处用的 Story 对象变量名，
所以不能简单扫 `X.key` —— 必须先把「从 cfg 赋出来的别名」（im = cfg.images 这种）
**逐文件**识别出来，只认 cfg 本体和这些别名。否则 Story 的 .title/.chapters
全会被当成配置键；而别名表如果跨文件共用，别的文件里的本地 list / 导入模块
（paths、images）也会串进来。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from hsg.config import load_config  # noqa: E402

# dict/Config 自身的方法，不是配置键
_NOT_KEYS = {
    "items", "keys", "values", "get", "get_path", "as_dict", "save", "pop", "update",
    "setdefault", "strip", "lower", "upper", "split", "replace", "copy", "clear",
}
# 别名赋值：im = cfg.images / st = cfg.story / v = cfg.video …
_ALIAS_RE = re.compile(r"^\s*(\w+)\s*=\s*cfg\.(\w+)\s*(?:#.*)?$", re.M)


def _source_files() -> list[Path]:
    return sorted((ROOT / "src" / "hsg").glob("*.py")) + sorted((ROOT / "scripts").glob("*.py"))


def _alias_map(txt: str, sections: dict) -> dict[str, str]:
    """逐文件识别别名 —— 必须逐文件，跨文件会串味：

    别的文件里一个本地变量叫 `paths`（本地 list）或 `images`（导入的模块），
    一旦全项目共用别名表，`paths.append(p)`、`images.convert(...)` 都会被
    当成配置键读，②那栏会冒出几十条垃圾。
    """
    out: dict[str, str] = {}
    for m in _ALIAS_RE.finditer(txt):
        if m.group(2) in sections:
            out[m.group(1)] = m.group(2)
    return out


def collect(cfg) -> tuple[dict[str, set[str]], dict[str, str]]:
    sections = {k: v for k, v in cfg.items() if isinstance(v, dict)}
    read: dict[str, set[str]] = {s: set() for s in sections}
    seen_aliases: dict[str, str] = {}

    for f in _source_files():
        txt = f.read_text(encoding="utf-8", errors="replace")
        aliases = _alias_map(txt, sections)
        seen_aliases.update(aliases)

        def add(section: str, key: str) -> None:
            if section in read and key and key not in _NOT_KEYS and not key.startswith("__"):
                read[section].add(key)

        # cfg.<sec>.get("k") / cfg["<sec>"].get("k") / cfg.paths.get_path("k")
        for m in re.finditer(r"""cfg(?:\.|\[\s*["'])(\w+)["']?\s*\]?\s*\.(?:get|get_path)\(\s*["'](\w+)["']""", txt):
            add(m.group(1), m.group(2))
        # cfg.<sec>.<k> / cfg["<sec>"]["<k>"]
        for m in re.finditer(r"""cfg\.(\w+)\.(\w+)|cfg\[\s*["'](\w+)["']\s*\]\[\s*["'](\w+)["']\s*\]""", txt):
            add(m.group(1) or m.group(3), m.group(2) or m.group(4))
        # 别名：im.get("k") / im.get_path("k") / st.<k>
        for alias, sec in aliases.items():
            for m in re.finditer(rf"""\b{alias}\.(?:get|get_path)\(\s*["'](\w+)["']""", txt):
                add(sec, m.group(1))
            # 只认属性访问，跳过方法调用形态（X.foo( ）—— 那多半是 PIL/dict 的方法
            for m in re.finditer(rf"\b{alias}\.(\w+)\b(?!\s*\()", txt):
                add(sec, m.group(1))
    return read, seen_aliases


def main() -> int:
    only = sys.argv[1] if len(sys.argv) > 1 else None
    cfg = load_config()
    sections = {k: v for k, v in cfg.items() if isinstance(v, dict)}
    read, aliases = collect(cfg)

    print(f"配置小节：{', '.join(sections)}")
    print(f"识别到 {len(aliases)} 个 cfg 别名：{', '.join(f'{a}={s}' for a, s in sorted(aliases.items()))}")

    print("\n" + "=" * 76)
    print("① 死配置：写在 config.yaml，代码从不读（假开关，会误导）")
    print("=" * 76)
    dead: list[str] = []
    for sec, kv in sections.items():
        if only and sec != only:
            continue
        for k, val in kv.items():
            if isinstance(val, dict):
                # 子段（llm.deepseek 这种）是 cfg.llm[provider] 动态取的，静态扫不到，
                # 不算死配置。它们同时也在「子段」名册里，供 tts.minimax 这类使用。
                continue
            if k not in read.get(sec, set()):
                dead.append(f"{sec}.{k}")
    print("  " + ("\n  ".join(dead) if dead else "（无）"))
    print(f"  共 {len(dead)} 项")

    print("\n" + "=" * 76)
    print("② 依赖默认值：代码会读，但 config.yaml 没写（改成显式更清楚）")
    print("=" * 76)
    missing: list[str] = []
    for sec, keys in sorted(read.items()):
        if only and sec != only:
            continue
        for k in sorted(keys):
            if k not in sections.get(sec, {}):
                missing.append(f"{sec}.{k}")
    print("  " + ("\n  ".join(missing) if missing else "（无）"))
    print(f"  共 {len(missing)} 项")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
