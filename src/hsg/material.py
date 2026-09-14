"""素材采集：给写稿提供史实锚点。

实测可用性（2026-09，国内网络）：
  · zh.wikipedia.org / commons.wikimedia.org  → 超时不可用
  · baike.baidu.com                          → 403（人机校验）
  · cn.bing.com（网页检索 / 图片检索）        → 可用 ← 用这个

所以素材源 = Bing 检索摘要。摘要质量一般（会有游戏站、诗文站噪声），
定位是「人名/年份的参考锚点」，不要求模型照抄；写稿仍以模型自身知识为主，
再由 verify.py 做史实审校。
"""

from __future__ import annotations

import html
import logging
import re
from pathlib import Path

import httpx

from .config import Config

log = logging.getLogger("hsg.material")

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

_ALGO_RE = re.compile(r'<li class="b_algo".*?</li>', re.S)
_TAG_RE = re.compile(r"<[^>]+>")
_HREF_RE = re.compile(r'<h2[^>]*>\s*<a[^>]*href="([^"]+)"', re.S)
_TEXT_RE = re.compile(r"<p[^>]*>(.*?)</p>", re.S)


def _clean_html(frag: str) -> str:
    txt = _TAG_RE.sub("", frag)
    txt = html.unescape(txt)
    txt = txt.replace("\u2002", " ").replace("\xa0", " ").replace("\u200b", "")
    return re.sub(r"\s+", " ", txt).strip()


def search_snippets(query: str, cfg: Config, limit: int = 8) -> list[dict]:
    """Bing 网页检索 → [{title, url, snippet}]。零成本、无需 key。"""
    m = cfg.material
    timeout = float(m.get("timeout", 20))
    drop = list(m.get("drop_if_contains", []) or [])
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as c:
            r = c.get(
                "https://cn.bing.com/search",
                params={"q": query, "ensearch": "0", "count": "20"},
                headers={
                    "User-Agent": _UA,
                    "Accept-Language": "zh-CN,zh;q=0.9",
                    "Accept": "text/html,application/xhtml+xml",
                },
            )
            r.raise_for_status()
            page = r.text
    except Exception as exc:  # noqa: BLE001
        log.warning("检索失败 %r：%s", query, exc)
        return []

    out: list[dict] = []
    for block in _ALGO_RE.findall(page):
        title = ""
        hm = re.search(r"<h2[^>]*>(.*?)</h2>", block, re.S)
        if hm:
            title = _clean_html(hm.group(1))
        url = ""
        um = _HREF_RE.search(block)
        if um:
            url = html.unescape(um.group(1))
        body = " ".join(_clean_html(p) for p in _TEXT_RE.findall(block))
        body = body or _clean_html(block)
        text = f"{title}｜{body}".strip("｜")
        if len(text) < 20:
            continue
        if any(k in text for k in drop):
            continue
        out.append({"title": title, "url": url, "snippet": body, "query": query})
        if len(out) >= limit:
            break
    return out


def fetch_material(topic: str, queries: list[str], cfg: Config, cache_dir: Path) -> tuple[str, dict]:
    """抓一批检索摘要，拼成给写稿用的素材块。

    返回 (素材文本, 元信息)。缓存到 cache_dir，同一主题重跑不再打网络。
    """
    meta = {"topic": topic, "queries": [], "items": 0, "provider": cfg.material.get("provider")}
    if not bool(cfg.material.get("enabled", True)) or str(cfg.material.get("provider")) == "none":
        return "", meta

    cache_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^\w\u4e00-\u9fa5]+", "_", topic)[:40] or "topic"
    cache_file = cache_dir / f"{safe}.txt"
    if cache_file.exists() and cache_file.stat().st_size > 200:
        log.info("复用素材缓存 %s", cache_file.name)
        text = cache_file.read_text(encoding="utf-8")
        meta["items"] = text.count("\n- ")
        meta["cached"] = True
        return text, meta

    per = int(cfg.material.get("snippets_per_query", 8))
    lines: list[str] = [f"# 参考素材（Bing 检索摘要，仅作人名/年份锚点，可能有噪声）", f"主题：{topic}", ""]
    total = 0
    for q in [topic, *queries][: int(cfg.material.get("max_queries", 6))]:
        snips = search_snippets(q, cfg, limit=per)
        meta["queries"].append({"query": q, "hits": len(snips)})
        if not snips:
            continue
        lines.append(f"## 检索：{q}")
        for s in snips:
            lines.append(f"- {s['snippet']}")
            total += 1
        lines.append("")

    if total == 0:
        log.warning("素材检索零命中（网络或反爬），继续用模型自身知识写稿")
        return "", meta

    text = "\n".join(lines)
    cache_file.write_text(text, encoding="utf-8")
    meta["items"] = total
    log.info("素材已获取：%d 路检索 / %d 条摘要 → %s", len(meta["queries"]), total, cache_file.name)
    return text, meta
