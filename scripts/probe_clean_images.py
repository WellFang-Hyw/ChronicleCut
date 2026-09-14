"""零成本探测：clean 版权策略下，三家博物馆到底能覆盖几个分镜。

不调 LLM、不调 TTS。用一期典型历史题材的分镜级检索词（中英各一路），
真实跑一遍配图，报告：命中率、版权构成、年代贴合度、有没有踩到离题作品。

用法（在项目根目录）：
    env -u PYTHONHOME -u UV_INTERNAL__PYTHONHOME \
        .venv/Scripts/python.exe scripts/probe_clean_images.py
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hsg import images as I  # noqa: E402
from hsg.config import load_config, ensure_dirs  # noqa: E402

# 模拟一期「明末城破」的分镜（英文检索词是博物馆能命中的写法）
SCENES = [
    ("城破那一刻", ["Ming dynasty painting city gate", "Chinese city wall painting"]),
    ("士绅官员的选择", ["Ming dynasty painting scholar official", "Chinese hanging scroll scholar"]),
    ("商人与货栈", ["Chinese painting market town", "Ming dynasty album commerce"]),
    ("僧人与寺庙", ["Chinese Buddhist painting temple", "Ming dynasty temple painting"]),
    ("工匠与匠户", ["Chinese painting craftsman workshop", "Ming dynasty album workshop"]),
    ("同一座城，不同的命", ["Ming dynasty landscape painting city", "Chinese handscroll city"]),
]


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    log = logging.getLogger("probe")
    cfg = load_config()
    ensure_dirs(cfg)
    cfg.images["license_policy"] = "clean"

    log.info("图源：%s", I.active_providers(cfg))
    log.info("")

    period = (1600, 1644)          # 明末
    used: list[int] = []
    cache = cfg.paths.get_path("image_dir") / "probe_clean"
    hit = clean = 0
    era0 = era1 = 0
    for idx, (title, queries) in enumerate(SCENES, start=1):
        p, credit, src, attempts = I.fetch_for_scene(
            idx, queries, cfg, cache, used, queries_en=queries, period=period)
        if p:
            hit += 1
            cands = attempts[0].get("candidates", 0) if attempts else 0
            e0 = attempts[0].get("era0", 0) if attempts else 0
            lic = (attempts[-1].get("license") or "⚠️") if attempts else "⚠️"
            if lic and "⚠️" not in lic:
                clean += 1
            if e0:
                era0 += 1
            log.info("[%d] ✓ %-14s %s | %s | 候选 %d（年代贴合 %d）",
                     idx, title, src, lic, cands, e0)
            log.info("      %s", credit[:88])
        else:
            log.info("[%d] ✗ %-14s 没找到图（会退化成渐变底图）", idx, title)
    log.info("")
    log.info("命中 %d/%d，版权已标注 %d 张，有年代贴合候选 %d 个",
             hit, len(SCENES), clean, era0)
    log.info("图源明细已写入 %s", cache / "_sources.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
