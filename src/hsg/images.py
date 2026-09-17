"""配图获取（版权安全优先）。

## 设计原则

用户要求「从根本上避免侵权风险」（防止视觉中国式索赔），所以配图分两类：

  **版权干净的图库**（默认只用这些）：
    · cleveland  克利夫兰艺术博物馆开放接口 —— CC0，**带 creation_date**，
                 可以按年代匹配排序（实测国内可直连、无需 key）
    · met        大都会博物馆开放接口 —— CC0，带 objectBeginDate/EndDate
    · artic      芝加哥艺术博物馆开放接口 —— 公共领域，带 date_start/date_end
    · unsplash   Unsplash —— 免费商用、免署名（需 UNSPLASH_ACCESS_KEY，现代摄影）
    · pexels     Pexels   —— 免费商用、免署名（需 PEXELS_API_KEY，现代摄影）
    · si         Smithsonian Open Access —— CC0（需 SMITHSONIAN_API_KEY）
    · rijks      Rijksmuseum —— 公共领域（需 RIJKSMUSEUM_API_KEY）

  **版权未明的关键词检索**（bing，默认关闭）：
    图是全网搜来的，来源杂、版权不明。只有 `images.license_policy: mixed`
    时才参与，且只在干净图库找不到图时兜底。

## 实测可用性（2026-09，国内网络，真实拉过图测过速度）

  · cleveland  克利夫兰艺术博物馆  搜索正常 + 下载 317 KB/s  ← 默认唯一图源
  · pexels     Pexels             下载 233 KB/s，但需 key；现代摄影
  · unsplash   Unsplash           下载 33 KB/s，需 key；现代摄影
  · met        大都会             搜索正常，但图服务器只有 **20 KB/s**（一张要几分钟）✗
  · artic      芝加哥艺术馆        搜索正常，但图床一律 **403**                    ✗
  · api.openverse.org             连接超时                                       ✗
  · www.loc.gov                   403（Cloudflare 人机校验）                      ✗
  · commons.wikimedia.org         连接超时                                       ✗
  · theme.npm.edu.tw（台北故宫）   连接超时                                       ✗
  · cn.bing.com 图片检索           可用，但版权未明（仅 mixed 模式兜底）

  ⚠️ 教训：「搜得到」不等于「下得动」。met / artic 的搜索结果完全正常 ——
  只有真去下载才发现一个 20 KB/s、一个 403。所以改图源之后必须跑
  `probe-images --download` 量一遍真实速度，别只看候选条数。

## 选图顺序

  ① 年代匹配（博物馆接口有年代字段，明末故事优先选明代作品）
  ② 馆藏/档案馆来源 > 其他
  ③ 干净版权 > 版权未明
  ④ 平均哈希去重：同一张图不会在片子里出现两次
"""

from __future__ import annotations

import logging
import re
import threading
import time
from pathlib import Path

import httpx
from PIL import Image

from .config import Config, env_get

log = logging.getLogger("hsg.images")

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

# 明显不适合当背景的链接（图标、表情、二维码、logo 之类）
_BAD_URL_HINTS = (
    "logo", "icon", "sprite", "avatar", "qrcode", "qr_", "emoji", "water",
    "banner_ad", "placeholder", "default_", ".svg", ".gif", ".tif",
    "aigc", "ai_images", "ai-image", "ai_img",
)

# 标题/页面里出现这些词，说明更像「美术作品/文物」而不是游客照
_ART_MARKERS = (
    "画", "卷", "壁画", "绢本", "纸本", "拓", "文物", "藏品", "馆藏", "博物馆",
    "青铜", "陶", "玉", "简牍", "俑", "像", "图卷", "长卷", "刻本", "卷轴",
    "painting", "hanging scroll", "album", "handscroll", "vase", "bronze",
)

# 年代解析：从 creation_date / date_display 里抠出年份区间
_YEAR_RE = re.compile(r"(?<!\d)(\d{3,4})(?!\d)")
_BCE_RE = re.compile(r"(\d{1,4})\s*(?:BCE|BC|bce|bc)", re.I)
_CENTURY_RE = re.compile(r"(\d{1,2})\s*(?:st|nd|rd|th)\s+century", re.I)


def _client(cfg: Config) -> httpx.Client:
    return httpx.Client(
        timeout=float(cfg.images.get("timeout", 25)),
        follow_redirects=True,
        headers={"User-Agent": _UA, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"},
    )


# ---------------------------------------------------------------- 年代
def parse_years(text: str) -> tuple[int, int] | None:
    """把 "1130" / "1598–1652" / "Ming dynasty, 1368-1644" / "2nd century BCE"
    解析成 (起, 止) 公元年（公元前为负）。解析不出来返回 None。
    """
    s = str(text or "").strip()
    if not s:
        return None
    bce = _BCE_RE.search(s)
    if bce:
        y = -int(bce.group(1))
        return (y, y)
    cen = _CENTURY_RE.search(s)
    if cen:
        n = int(cen.group(1))
        y = (n - 1) * 100 + 1          # 1 世纪 = 公元 1-100 年
        return (y, y + 99)
    years = [int(m.group(1)) for m in _YEAR_RE.finditer(s)]
    years = [y for y in years if 1 <= y <= 2100]
    if not years:
        return None
    return (min(years), max(years))


def era_tier(art_years: tuple[int, int] | None, period: tuple[int, int] | None) -> int:
    """作品年代与故事年代的贴合度：0 最贴、1 尚可、2 差/未知。

    这是博物馆接口独有的好处 —— 明末的故事优先挑明代作品，
    而不是一幅宋代山水（实测前者观感好得多）。
    """
    if art_years is None or period is None:
        return 2
    lo, hi = min(period), max(period)
    a_lo, a_hi = art_years
    span = max(60, hi - lo)
    # 有重叠，或相差不到一个故事跨度
    if a_lo <= hi + span and a_hi >= lo - span:
        return 0
    gap = max(lo, a_lo) - min(hi, a_hi)
    if gap <= 400:
        return 1
    return 2


# ---------------------------------------------------------------- 图源
def _culture_ok(blob: str, cfg: Config) -> bool:
    """作品是否属于目标文化圈（默认中国）。

    为什么必须过滤：博物馆接口是**关键词**匹配，版权再干净也可能离题 ——
    实测搜 "Ming dynasty painting city wall" 时，芝加哥艺术馆返回了
    《大碗岛的星期天》和伦敦议会大厦，大都会返回了波斯手抄本。
    在讲明代城破的片子里放一张莫奈，比放一张现代照片还糟。
    """
    if not bool(cfg.images.get("culture_filter", True)):
        return True
    words = [str(w) for w in (cfg.images.get("culture_keywords") or [])]
    if not words:
        return True
    b = (blob or "").lower()
    return any(w.lower() in b for w in words)


def search_cleveland(query: str, cfg: Config, limit: int = 12) -> list[dict]:
    """克利夫兰艺术博物馆开放接口（CC0，含年代字段）。"""
    try:
        with _client(cfg) as c:
            r = c.get(
                "https://openaccess-api.clevelandart.org/api/artworks/",
                params={"q": query, "limit": str(limit * 3), "has_image": "1", "cc0": "1"},
            )
            r.raise_for_status()
            data = r.json().get("data") or []
    except Exception as exc:  # noqa: BLE001
        log.debug("cleveland 检索失败 %r：%s", query, exc)
        return []
    out: list[dict] = []
    for a in data:
        if str(a.get("share_license_status") or "").upper() != "CC0":
            continue
        im = a.get("images") or {}
        url = ((im.get("print") or {}).get("url") or (im.get("web") or {}).get("url")
               or (im.get("full") or {}).get("url"))
        if not url:
            continue
        creators = a.get("creators") or []
        who = (creators[0].get("description") if creators else "") or "佚名"
        blob = " ".join([who, str(a.get("title") or ""), " ".join(a.get("culture") or []),
                         " ".join(a.get("technique") or []), str(a.get("type") or "")])
        if not _culture_ok(blob, cfg):
            continue
        out.append({
            "url": url,
            "page": str(a.get("url") or ""),
            "title": str(a.get("title") or "")[:80],
            "credit": f"克利夫兰艺术博物馆 · {who}（{a.get('creation_date') or '年代不详'}）",
            "license": "CC0 公共领域",
            "years": parse_years(a.get("creation_date")),
            "source": "cleveland",
        })
        if len(out) >= limit:
            break
    return out


def search_met(query: str, cfg: Config, limit: int = 3) -> list[dict]:
    """大都会博物馆开放接口（CC0 公共领域，带 culture 与年代字段）。

    注意性能：Met 的搜索只返回 objectID，每件要再请求一次详情。
    所以既要限定部门（默认 6=亚洲艺术部，直接砍掉大量无关藏品），
    也要把详情请求数压住（否则一个检索词能拖掉几分钟）。
    """
    try:
        params = {"q": query, "hasImages": "true"}
        if bool(cfg.images.get("culture_filter", True)) and \
                any("chin" in str(w).lower() for w in (cfg.images.get("culture_keywords") or [])):
            params["departmentId"] = str(cfg.images.get("met_department_id", 6))
        cap = int(cfg.images.get("met_max_objects", 9))
        want = min(int(limit), int(cfg.images.get("met_max_results", 6)))
        with _client(cfg) as c:
            r = c.get(
                "https://collectionapi.metmuseum.org/public/collection/v1/search",
                params=params,
            )
            r.raise_for_status()
            ids = (r.json().get("objectIDs") or [])[: min(want * 3, cap)]
        if not ids:
            return []
        detail_timeout = float(cfg.images.get("timeout_detail", 12))
        out: list[dict] = []
        with httpx.Client(timeout=detail_timeout, follow_redirects=True,
                          headers={"User-Agent": _UA}) as c:
            for oid in ids:
                if len(out) >= want:
                    break
                try:
                    d = c.get(
                        f"https://collectionapi.metmuseum.org/public/collection/v1/objects/{oid}"
                    ).json()
                except Exception:  # noqa: BLE001
                    continue
                if not d.get("isPublicDomain") or not d.get("primaryImage"):
                    continue
                blob = " ".join([str(d.get("culture") or ""), str(d.get("department") or ""),
                                 str(d.get("artistNationality") or ""),
                                 str(d.get("artistDisplayName") or ""),
                                 str(d.get("objectName") or ""), str(d.get("medium") or "")])
                if not _culture_ok(blob, cfg):
                    continue
                years = parse_years(d.get("objectDate"))
                if years is None and d.get("objectBeginDate"):
                    years = (int(d["objectBeginDate"]),
                             int(d.get("objectEndDate") or d["objectBeginDate"]))
                out.append({
                    "url": d["primaryImage"],
                    "page": str(d.get("objectURL") or ""),
                    "title": str(d.get("title") or "")[:80],
                    "credit": f"大都会艺术博物馆 · {d.get('artistDisplayName') or '佚名'}"
                              f"（{d.get('objectDate') or '年代不详'}）",
                    "license": "CC0 公共领域",
                    "years": years,
                    "source": "met",
                })
        return out
    except Exception as exc:  # noqa: BLE001
        log.debug("met 检索失败 %r：%s", query, exc)
        return []


def search_artic(query: str, cfg: Config, limit: int = 12) -> list[dict]:
    """芝加哥艺术博物馆开放接口（公共领域，带 place_of_origin 与年代字段）。"""
    try:
        with _client(cfg) as c:
            r = c.get(
                "https://api.artic.edu/api/v1/artworks/search",
                params={
                    "q": query,
                    "limit": str(limit * 3),
                    "fields": "id,title,image_id,is_public_domain,artist_display,"
                              "date_display,date_start,date_end,place_of_origin,medium_display",
                },
            )
            r.raise_for_status()
            data = r.json().get("data") or []
    except Exception as exc:  # noqa: BLE001
        log.debug("artic 检索失败 %r：%s", query, exc)
        return []
    out: list[dict] = []
    for it in data:
        if not it.get("is_public_domain") or not it.get("image_id"):
            continue
        blob = " ".join([str(it.get("artist_display") or ""), str(it.get("title") or ""),
                         str(it.get("place_of_origin") or ""), str(it.get("medium_display") or "")])
        if not _culture_ok(blob, cfg):
            continue
        years = None
        if it.get("date_start") is not None:
            years = (int(it["date_start"]), int(it.get("date_end") or it["date_start"]))
        out.append({
            "url": f"https://www.artic.edu/iiif/2/{it['image_id']}/full/1686,/0/default.jpg",
            "page": f"https://www.artic.edu/artworks/{it['id']}",
            "title": str(it.get("title") or "")[:80],
            "credit": f"芝加哥艺术博物馆 · {it.get('artist_display') or '佚名'}"
                      f"（{it.get('date_display') or '年代不详'}）",
            "license": "公共领域（CC0）",
            "years": years or parse_years(it.get("date_display")),
            "source": "artic",
        })
        if len(out) >= limit:
            break
    return out


def search_unsplash(query: str, cfg: Config, limit: int = 10) -> list[dict]:
    """Unsplash（免费商用、免署名；需 UNSPLASH_ACCESS_KEY）。现代摄影，非历史素材。"""
    key = env_get("UNSPLASH_ACCESS_KEY")
    if not key:
        return []
    try:
        with _client(cfg) as c:
            r = c.get("https://api.unsplash.com/search/photos",
                      params={"query": query, "per_page": str(limit), "orientation": "landscape"},
                      headers={"Authorization": f"Client-ID {key}"})
            r.raise_for_status()
            data = r.json().get("results") or []
    except Exception as exc:  # noqa: BLE001
        log.debug("unsplash 检索失败 %r：%s", query, exc)
        return []
    return [{
        "url": (it.get("urls") or {}).get("regular") or "",
        "page": str((it.get("links") or {}).get("html") or ""),
        "title": str(it.get("alt_description") or it.get("description") or "")[:80],
        "credit": f"Unsplash · {(it.get('user') or {}).get('name') or '佚名'}",
        "license": "Unsplash License（免费商用）",
        "years": None,
        "source": "unsplash",
    } for it in data if (it.get("urls") or {}).get("regular")]


def search_pexels(query: str, cfg: Config, limit: int = 10) -> list[dict]:
    """Pexels（免费商用、免署名；需 PEXELS_API_KEY）。现代摄影，非历史素材。"""
    key = env_get("PEXELS_API_KEY")
    if not key:
        return []
    try:
        with _client(cfg) as c:
            r = c.get("https://api.pexels.com/v1/search",
                      params={"query": query, "per_page": str(limit), "orientation": "landscape"},
                      headers={"Authorization": key})
            r.raise_for_status()
            data = r.json().get("photos") or []
    except Exception as exc:  # noqa: BLE001
        log.debug("pexels 检索失败 %r：%s", query, exc)
        return []
    return [{
        "url": (it.get("src") or {}).get("large2x") or (it.get("src") or {}).get("large") or "",
        "page": str(it.get("url") or ""),
        "title": str(it.get("alt") or "")[:80],
        "credit": f"Pexels · {it.get('photographer') or '佚名'}",
        "license": "Pexels License（免费商用）",
        "years": None,
        "source": "pexels",
    } for it in data if (it.get("src") or {}).get("large")]


def search_si(query: str, cfg: Config, limit: int = 10) -> list[dict]:
    """Smithsonian Open Access（CC0；需 SMITHSONIAN_API_KEY）。"""
    key = env_get("SMITHSONIAN_API_KEY")
    if not key:
        return []
    try:
        with _client(cfg) as c:
            r = c.get("https://api.si.edu/openaccess/api/v1.0/search",
                      params={"q": query, "api_key": key, "rows": str(limit)})
            r.raise_for_status()
            rows = ((r.json().get("response") or {}).get("rows")) or []
    except Exception as exc:  # noqa: BLE001
        log.debug("si 检索失败 %r：%s", query, exc)
        return []
    out: list[dict] = []
    for it in rows:
        media = (((it.get("content") or {}).get("descriptiveNonRepeating") or {})
                 .get("online_media") or {}).get("media") or []
        url = next((m.get("content") for m in media if m.get("content")), "")
        if not url:
            continue
        out.append({
            "url": url,
            "page": str((it.get("content") or {}).get("descriptiveNonRepeating", {})
                        .get("record_link") or ""),
            "title": str(it.get("title") or "")[:80],
            "credit": "Smithsonian Open Access",
            "license": "CC0 公共领域",
            "years": parse_years((it.get("content") or {}).get("freetext", {}).get("date", [{}])[0]
                                 .get("content", "") if isinstance(
                                     (it.get("content") or {}).get("freetext", {}).get("date"),
                                     list) else ""),
            "source": "si",
        })
    return out


def search_rijks(query: str, cfg: Config, limit: int = 10) -> list[dict]:
    """Rijksmuseum（公共领域；需 RIJKSMUSEUM_API_KEY）。"""
    key = env_get("RIJKSMUSEUM_API_KEY")
    if not key:
        return []
    try:
        with _client(cfg) as c:
            r = c.get(f"https://www.rijksmuseum.nl/api/en/collection",
                      params={"q": query, "key": key, "ps": str(limit),
                              "imgonly": "True", "format": "json"})
            r.raise_for_status()
            arts = (r.json().get("artObjects")) or []
    except Exception as exc:  # noqa: BLE001
        log.debug("rijks 检索失败 %r：%s", query, exc)
        return []
    out: list[dict] = []
    for it in arts:
        if not it.get("webImage"):
            continue
        out.append({
            "url": (it.get("webImage") or {}).get("url") or "",
            "page": str(it.get("links", {}).get("web") or ""),
            "title": str(it.get("title") or "")[:80],
            "credit": f"Rijksmuseum · {it.get('principalOrFirstMaker') or '佚名'}",
            "license": "公共领域",
            "years": parse_years(it.get("dating") or ""),
            "source": "rijks",
        })
    return out


def search_bing(query: str, cfg: Config, limit: int = 30) -> list[dict]:
    """关键词图片检索。**版权未明**，只在 license_policy=mixed 时兜底参与。"""
    try:
        with _client(cfg) as c:
            r = c.get("https://cn.bing.com/images/async",
                      params={"q": query, "first": "1", "count": str(limit), "mmasync": "1"})
            r.raise_for_status()
            page = r.text.replace("&quot;", '"').replace("\\/", "/")
    except Exception as exc:  # noqa: BLE001
        log.warning("Bing 图片检索失败 %r：%s", query, exc)
        return []

    out: list[dict] = []
    for m in re.finditer(r'"murl":"(.*?)"', page):
        url = m.group(1).strip()
        if not url.startswith("http"):
            continue
        if any(h in url.lower() for h in _BAD_URL_HINTS):
            continue
        seg = page[max(0, m.start() - 700): m.end() + 700]
        pm = re.search(r'"purl":"(.*?)"', seg)
        tm = re.search(r'"t":"(.*?)"', seg)
        out.append({
            "url": url,
            "page": pm.group(1) if pm else "",
            "title": (tm.group(1) if tm else "")[:80],
            "credit": "",
            "license": "版权未明（关键词检索）",
            "years": None,
            "source": "bing",
        })
        if len(out) >= limit * 2:
            break
    return out


# 图源登记表：risky=True 的只在 license_policy=mixed 时参与
PROVIDERS: dict[str, dict] = {
    "cleveland": {"fn": search_cleveland, "key_env": None, "risky": False, "zh": False},
    "artic":     {"fn": search_artic,     "key_env": None, "risky": False, "zh": False},
    "met":       {"fn": search_met,       "key_env": None, "risky": False, "zh": False},
    "si":        {"fn": search_si,        "key_env": "SMITHSONIAN_API_KEY", "risky": False, "zh": False},
    "rijks":     {"fn": search_rijks,     "key_env": "RIJKSMUSEUM_API_KEY", "risky": False, "zh": False},
    "unsplash":  {"fn": search_unsplash,  "key_env": "UNSPLASH_ACCESS_KEY", "risky": False, "zh": False},
    "pexels":    {"fn": search_pexels,    "key_env": "PEXELS_API_KEY", "risky": False, "zh": False},
    "bing":      {"fn": search_bing,      "key_env": None, "risky": True, "zh": True},
}

# 兼容旧名（probe-images 用）
_SEARCHERS = {k: v["fn"] for k, v in PROVIDERS.items()}


def active_providers(cfg: Config) -> list[str]:
    """按版权策略与 key 可用性筛出本次真正参与检索的图源。

    语义：`providers` 列表 = 用哪些**版权干净**的图源。
    版权未明的（bing）不由列表控制，而是由策略控制 —— `license_policy: mixed`
    时自动追加、并排在最后当兜底（否则「改策略」这个动作会静默失效：
    bing 不在列表里，mixed 跟 clean 跑出来一模一样）。
    """
    policy = str(cfg.images.get("license_policy") or "clean").lower()
    want = [str(p) for p in (cfg.images.get("providers") or list(PROVIDERS))]
    out: list[str] = []
    for name in want:
        spec = PROVIDERS.get(name)
        if spec is None:
            continue
        if spec["risky"] and policy == "clean":
            continue
        if spec["key_env"] and not env_get(spec["key_env"]):
            log.debug("图源 %s 未配置 %s，跳过", name, spec["key_env"])
            continue
        out.append(name)
    if policy == "mixed":
        for name, spec in PROVIDERS.items():
            if spec["risky"] and name not in out:
                out.append(name)      # 版权未明的兜底图源永远排最后
    if not out:
        log.warning("版权策略 %s 下没有任何可用图源（缺 key？），将用渐变底图", policy)
    return out


# ---------------------------------------------------------------- 场景级英文检索词
# 为什么需要这一步：实测（2026-09，用当期 _sources.json 逐条核对）发现
# **18/18 配图全部来自英文检索词，中文检索词 0 个起作用**，而选中的还是兜底泛词
# 「Ming dynasty painting」——因为场景自己那一批候选数是 0。
#
# 机制原因：中文检索词只发给 bing（PROVIDERS[...]["zh"]=True），
# 而 license_policy: clean 下 bing 不参与 → 每个分镜自己的中文检索词是**空转**的，
# 配图完全由「章节级英文词 → 兜底泛词」决定，题材必然对不上
# （实测分镜要「清代铜钱」，配的是明代斗彩婴戏杯）。
#
# 修法：写稿之后、配图之前，花一次便宜的 LLM 调用把每个分镜的中文检索词
# 翻成博物馆索引能命中的英文词，让它真正到达 Cleveland。挂在 images.translate_scene_queries。
_SCENE_QUERY_SYSTEM = """你把中文的配图检索词改写成**博物馆藏品库能命中的英文检索词**。

藏品库（克利夫兰/大都会的开放接口）的索引是英文的，而且只认「时代 + 画种/器物」这种写法。
所以你的输出要求：

1. 结构是「时代 + 画种或器物」，例如：
   · 清代 铜钱 串钱 → "Qing dynasty copper coins"
   · 清代 农民 耕作 古画 → "Qing dynasty painting peasant farming"
   · 清代 粥厂 施粥 古画 → "Chinese painting famine relief porridge"
   · 明代 驿站 马匹 古画 → "Ming dynasty painting horse post station"
2. 只要 3-6 个英文单词。**不要写句子**，不要写事件名（如「鸿门宴」），
   不要写抽象词（history/china/ancient 单独用没用），不要引号。
3. 宁可写「时代 + 泛画种」这种能命中的组合，也不要写馆里肯定没有的具体题材。
   例：找不到「拷问刑具」就写 "Ming dynasty painting figures"。
4. 纯 ASCII 英文，不要出现汉字或拼音。

只输出 JSON：{"queries": [{"index": 分镜序号, "en": "英文检索词"}]}"""


def _en_ok(s: str) -> bool:
    """英文检索词的清洗与校验：必须基本是 ASCII、3-8 个词、不吃汉字。"""
    t = re.sub(r"[\"'“”‘’]", "", (s or "").strip())
    t = re.sub(r"\s+", " ", t).strip(" .,;:")
    if not t or re.search(r"[\u4e00-\u9fa5]", t):
        return False
    words = t.split()
    return 2 <= len(words) <= 8 and len(re.findall(r"[A-Za-z]", t)) >= 6


def translate_scene_queries(scene_rows: list[tuple[int, str]], cfg: Config, llm) -> dict[int, str]:
    """把 [(分镜序号, 中文检索词)] 翻成 {分镜序号: 英文检索词}。

    只在配置开启且给了 llm 时执行；任何异常都返回已拿到的部分（不阻断出片）。
    """
    if not bool(cfg.images.get("translate_scene_queries", True)) or llm is None:
        return {}
    rows = [(i, q) for i, q in scene_rows if q and re.search(r"[\u4e00-\u9fa5]", q)]
    if not rows:
        return {}
    listing = "\n".join(f"{i}. {q}" for i, q in rows)
    try:
        data = llm.chat_json(_SCENE_QUERY_SYSTEM,
                            f"把下面每一条都改写成英文检索词：\n\n{listing}\n\n"
                            f"【输出】只输出 JSON（index 就是上面的序号）：\n"
                            f'{{"queries": [{{"index": 1, "en": "Qing dynasty painting"}}]}}',
                            max_tokens=2000, temperature=0.2)
    except Exception as exc:  # noqa: BLE001
        log.warning("场景检索词翻译失败（仍用章节级英文词）：%s", exc)
        return {}

    valid_idx = {i for i, _ in rows}
    out: dict[int, str] = {}
    for it in (data.get("queries") if isinstance(data, dict) else data) or []:
        if not isinstance(it, dict):
            continue
        try:
            idx = int(it.get("index") or 0)
        except (TypeError, ValueError):
            continue
        en = str(it.get("en") or "")
        if idx not in valid_idx or not _en_ok(en):
            continue
        out[idx] = re.sub(r"\s+", " ", en.strip().strip("\"'")).strip()
    log.info("场景级英文检索词：%d/%d 个分镜拿到（其余退回章节级英文词）", len(out), len(rows))
    return out


# ---------------------------------------------------------------- 下载 & 去重
def _avg_hash(path: Path) -> int:
    try:
        with Image.open(path) as im:
            g = im.convert("L").resize((8, 8), Image.LANCZOS)
            px = list(g.getdata())
    except Exception:  # noqa: BLE001
        return 0
    avg = sum(px) / max(1, len(px))
    bits = 0
    for i, p in enumerate(px):
        if p >= avg:
            bits |= 1 << i
    return bits


def _hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


# `used` 是跨线程共享的去重表（配图是 ThreadPoolExecutor 并发跑的）。
# 检查与登记必须原子完成 —— 否则两个分镜可能同时通过检查、同时登记，
# 同一张图就进了两条分镜（而且两者都以为自己去重过了）。
_USED_LOCK = threading.Lock()


def download_image(url: str, dest: Path, cfg: Config, referer: str = "") -> Path | None:
    if dest.exists() and dest.stat().st_size > 4096:
        return dest
    im = cfg.images
    dest.parent.mkdir(parents=True, exist_ok=True)
    headers = {"Referer": referer} if referer else {}
    max_bytes = int(float(im.get("max_mb", 25)) * 1024 * 1024)
    try:
        with _client(cfg) as c:
            r = c.get(url, headers=headers)
            r.raise_for_status()
            if len(r.content) < 4096:
                return None
            if len(r.content) > max_bytes:
                # 博物馆的原图常有几十 MB，画布最多 4K —— 超限的直接跳过，
                # 否则一张图就能把整个流程卡住几分钟
                log.debug("图片过大 %.1fMB，跳过：%s", len(r.content) / 1048576, url[:80])
                return None
            dest.write_bytes(r.content)
        with Image.open(dest) as img:
            img.verify()
        with Image.open(dest) as img:
            w, h = img.size
        if w < int(im.min_width) or h < int(im.min_height):
            log.debug("分辨率不足 %dx%d，跳过：%s", w, h, url[:80])
            dest.unlink(missing_ok=True)
            return None
        if h and w / h > float(im.max_aspect):
            log.debug("长宽比 %.1f 过宽，跳过：%s", w / h, url[:80])
            dest.unlink(missing_ok=True)
            return None
        return dest
    except Exception as exc:  # noqa: BLE001
        log.debug("下载失败 %s：%s", url[:90], exc)
        dest.unlink(missing_ok=True)
        return None


def _prepare(src: Path, dest: Path) -> Path:
    with Image.open(src) as im:
        if im.mode != "RGB":
            bg = Image.new("RGB", im.size, (255, 255, 255))
            if "A" in im.mode:
                bg.paste(im, mask=im.split()[-1])
            else:
                bg.paste(im.convert("RGB"))
            im = bg
        # 长边限制到 2600px，省内存也不影响画质（画布最多 4K）
        if max(im.size) > 2600:
            scale = 2600 / max(im.size)
            im = im.resize((int(im.width * scale), int(im.height * scale)), Image.LANCZOS)
        im.save(dest, "JPEG", quality=90)
    return dest


def _low_quality(cand: dict, cfg: Config) -> bool:
    """旅游照/素材图/AI 图库 —— 破坏历史沉浸感，降权。"""
    words = list(cfg.images.get("drop_page_keywords", []) or [])
    if not words:
        return False
    blob = f"{cand.get('page', '')} {cand.get('title', '')}".lower()
    return any(w.lower() in blob for w in words)


def _preferred_source(cand: dict, cfg: Config) -> bool:
    """馆藏/档案馆/美术馆/古籍库 —— 通常无水印、信息可靠。"""
    words = list(cfg.images.get("prefer_page_keywords", []) or [])
    if not words:
        return False
    blob = f"{cand.get('page', '')} {cand.get('title', '')}".lower()
    return any(w.lower() in blob for w in words)


def collect_candidates(queries_zh: list[str], cfg: Config,
                       queries_en: list[str] | None = None,
                       period: tuple[int, int] | None = None) -> list[dict]:
    """把所有检索词 × 可用图源的候选汇总，带版权与年代标注。

    · 中文检索词 → 只发给 bing（中文图库）
    · 英文检索词 → 发给博物馆/免费图库（它们的索引是英文的）
    只检索一次，不做过滤；过滤与排序在 fetch_for_scene 里做。
    """
    im = cfg.images
    providers = active_providers(cfg)
    per = int(im.get("candidates_per_query", 30))
    out: list[dict] = []
    seen_url: set[str] = set()

    jobs: list[tuple[str, str]] = []
    for q in queries_zh:
        for name in providers:
            if PROVIDERS[name]["zh"]:
                jobs.append((name, q))
    for q in (queries_en or []):
        for name in providers:
            if not PROVIDERS[name]["zh"]:
                jobs.append((name, q))

    for name, q in jobs:
        spec = PROVIDERS[name]
        for cand in spec["fn"](q, cfg, limit=per):
            url = str(cand.get("url") or "")
            if not url or url in seen_url:
                continue
            seen_url.add(url)
            cand = dict(cand)
            cand["query"] = q
            cand["risky"] = bool(spec["risky"])
            cand["era_tier"] = era_tier(cand.get("years"), period)
            blob = f"{cand.get('title', '')} {cand.get('page', '')}"
            cand["art_marker"] = any(m in blob for m in _ART_MARKERS)
            out.append(cand)
    return out


def _sort_key(cand: dict, cfg: Config, order: list[str] | None = None):
    """排序：年代贴合 → 馆藏来源 → 美术向 → 版权干净 → 图源顺序。

    最后一项是「图源顺序」：大都会的原图常有十几 MB，克利夫兰/芝加哥给的是
    两三 MB 的 JPEG，同样贴合时优先用后者 —— 实测能省掉一大截下载时间。
    """
    providers_order = order if order is not None else [
        str(p) for p in (cfg.images.get("providers") or [])]
    try:
        pos = providers_order.index(str(cand.get("source") or ""))
    except ValueError:
        pos = len(providers_order)
    return (
        int(cand.get("era_tier", 2)),
        0 if _preferred_source(cand, cfg) else 1,
        0 if cand.get("art_marker") else 1,
        1 if cand.get("risky") else 0,
        pos,
    )


def _try_batch(
    scene_index: int,
    qs_zh: list[str],
    qs_en: list[str],
    cfg: Config,
    cache_dir: Path,
    used: list[int],
    period: tuple[int, int] | None,
    providers: list[str],
    n_start: int,
) -> tuple[Path | None, str, str, list[dict], int]:
    """跑一批检索词，返回第一个可用候选。找不到返回 (None, ...)。"""
    im = cfg.images
    attempts: list[dict] = []
    cands = collect_candidates(qs_zh, cfg, qs_en, period)
    attempts.append({"queries_zh": qs_zh, "queries_en": qs_en,
                     "candidates": len(cands),
                     "clean": sum(1 for c in cands if not c.get("risky")),
                     "era0": sum(1 for c in cands if c.get("era_tier") == 0)})
    if not cands:
        return None, "", "", attempts, n_start

    ordered = sorted(cands, key=lambda c: _sort_key(c, cfg, providers))
    n = n_start
    for cand in ordered:
        n += 1
        raw = cache_dir / f"_dl_{scene_index}_{n}.bin"
        t0 = time.monotonic()
        got = download_image(cand["url"], raw, cfg, referer=cand.get("page", ""))
        spent = time.monotonic() - t0
        if not got:
            continue
        if spent > float(im.get("slow_download_warn", 25)):
            log.warning("分镜 %d 的图下载用了 %.0f 秒（图源 %s 很慢，考虑从 "
                        "images.providers 里去掉它）", scene_index, spent, cand.get("source"))
        h = _avg_hash(got)
        # 检查 + 登记放在同一把锁里（下载与 _prepare 在外面，不占锁，并发不受影响）
        with _USED_LOCK:
            if any(_hamming(h, u) < int(im.get("dedupe_hamming", 6)) for u in used):
                raw.unlink(missing_ok=True)
                continue
            used.append(h)
        final = cache_dir / f"scene_{scene_index:03d}.jpg"
        _prepare(got, final)
        raw.unlink(missing_ok=True)
        credit = str(cand.get("credit") or "").strip()
        if not credit and cand.get("page"):
            credit = re.sub(r"^www\.", "", httpx.URL(cand["page"]).host or "")
        license = str(cand.get("license") or "")
        label = f"{license}｜{credit}" if license else credit
        log.info("分镜 %d 配图：%s（%s / %s / 年代%s / %s / %.1fs）",
                 scene_index, final.name, cand.get("source"), cand.get("query"),
                 cand.get("era_tier"), license or "版权未明", spent)
        attempts.append({"picked": cand["url"], "page": cand.get("page"),
                         "source": cand.get("source"), "query": cand.get("query"),
                         "license": license, "credit": credit,
                         "era_tier": cand.get("era_tier"),
                         "download_seconds": round(spent, 1)})
        return final, label, str(cand.get("source") or ""), attempts, n
    return None, "", "", attempts, n


# ---------------------------------------------------------------- AI 生成配图
# 为什么需要：图库路线的天花板已实测到顶（见 config 注释）——克利夫兰索引对具体题材
# 几乎是空的（"Qing dynasty copper coins" 只 1 条、"…porridge relief" 0 条），
# 只有「时代+泛画种」宽词能命中，于是「要找清代铜钱」被配成明代斗彩婴戏杯。
# 生成图没有这个限制（提示词就是分镜内容），而且没有第三方版权。
#
# ---- 两套风格后缀：器物向 / 场景向 ----
# 为什么要分两套：器物向里写死了「整幅画面只有器物本身」，实测会把**所有**分镜
# 都拉成静物小品 —— 第 7 期 18 张里，要找坊市布局图给了干裂土地、要找巡夜兵丁
# 给了灯笼和木杖、要找衙门审案给了**一把西式法槌**（时代错乱）。
# 而当初收窄成器物向，是为了压掉「伪书法题字 + 红印章」——那两个毛病是
# 「工笔/绢本」这两个词诱发的（另外「摄影」会诱发图库水印，两头都不能用）。
# 所以正确做法不是二选一，而是：器物类分镜走静物向，叙事类走场景向，
# 两套都显式禁掉文字/印章/署名/水印/边框。
#
# ⚠️ 内置默认值必须和 config.yaml 同向。这里原来写的是「工笔风俗画/绢本设色」，
#    config 已按实测改掉，但默认值没跟着改 —— 等于埋了个雷：
#    谁删掉配置键，伪题跋和红印章就回来了。
_GEN_SUFFIX_DEFAULT = (
    "中国古代器物静物特写，暖光，浅景深，质感真实，色调灰暗克制，"
    "整幅画面只有器物本身，不要任何文字、印章、署名、水印、边框，"
    "不要仿照书画题跋的排版，不要书法题字，不要落款")
_GEN_SUFFIX_SCENE_DEFAULT = (
    "中国古代历史题材的叙事画面，以人物与场景为主体，中景或全身构图，"
    "真实质感，暖光侧照，浅景深，色调灰暗克制，"
    "不要任何文字、印章、署名、水印、边框，不要仿照书画题跋的排版，"
    "不要书法题字，不要落款，不要画面里出现书页、碑文或匾额文字")

# 分镜画面类型：object = 器物/文书静物；scene = 人物/场所叙事
KIND_OBJECT, KIND_SCENE = "object", "scene"
_KIND_SYSTEM = """你判断一条中文「配图检索词」适合画成哪一类画面，只输出 object 或 scene。

object = 单件器物、文书、钱粮、刑具、食物、服饰，用**静物特写**就能讲清楚的。
         例：清代铜钱 串钱 道光通宝 实物 / 唐律疏议古籍书影 笞杖刑具实物
scene  = 必须出现**人物或场所**才讲得清楚的：市井、街道、城门坊门、衙门审案、
         巡夜兵丁、农耕、赈济、宴饮、夜景、驿路。
         例：明代京城巡夜兵丁古画 / 唐代长安城坊市布局图 / 清代衙门审案场景

判断要点：
1. 出现人物身份（兵丁/官/吏/百姓/农夫/妇人/工匠/商贩/僧人）→ scene
2. 出现场所（城/坊门/街市/衙门/驿路/店铺/村落/桥/夜市/市井）→ scene
3. 只有单件物品或一篇文书，没有人物也没有场所 → object
4. 拿不准时选 scene（本频道讲的是故事，人物场景更贴合）

只输出 JSON：{"kinds": [{"index": 序号, "kind": "object" 或 "scene"}]}"""

# 规则兜底用的关键词（LLM 不可用/异常时靠它，scene 略占优）
_SCENE_KW = ("人", "兵", "官", "吏", "百姓", "民", "农", "妇", "工匠", "商", "僧",
             "队伍", "行列", "市", "街", "城", "坊", "门", "桥", "衙", "堂",
             "店", "铺", "村", "田", "路", "驿", "夜", "庙", "祭", "宴",
             "巡", "审", "赈", "婚", "丧", "戏", "场景", "市井")
_OBJECT_KW = ("器物", "实物", "钱", "币", "银", "账", "契", "券", "票", "古籍",
              "书影", "刻本", "纸", "笔", "墨", "砚", "杯", "碗", "壶", "罐",
              "瓶", "灯", "烛", "伞", "扇", "衣", "冠", "棺", "俑", "玉",
              "瓷", "陶", "铁", "瓦", "秤", "尺", "斗", "食", "饭", "粥",
              "饼", "茶", "酒", "药", "税", "粮", "仓", "具")


def _guess_kind_zh(query: str) -> str:
    """规则兜底判类型：出现人物/场所词就按 scene，否则 object。"""
    q = query or ""
    s = sum(1 for k in _SCENE_KW if k in q)
    o = sum(1 for k in _OBJECT_KW if k in q)
    return KIND_SCENE if (s >= 1 and s >= o) else KIND_OBJECT


def classify_scene_kinds(scene_rows: list[tuple[int, str]], cfg: Config, llm) -> dict[int, str]:
    """给每个分镜判画面类型 {分镜序号: "object"|"scene"}，供分派风格后缀。

    先铺规则结果兜底，再用一次便宜的 LLM 调用覆盖；任何异常都返回兜底值，
    不阻断出片。开关：images.classify_scene_kinds
    """
    if not bool(cfg.images.get("classify_scene_kinds", True)):
        return {}
    rows = [(i, q) for i, q in (scene_rows or []) if q]
    if not rows:
        return {}
    kinds = {i: _guess_kind_zh(q) for i, q in rows}
    if llm is None:
        return kinds
    try:
        listing = "\n".join(f"{i}. {q}" for i, q in rows)
        data = llm.chat_json(_KIND_SYSTEM,
                             f"判断下面每一条的类型：\n\n{listing}\n\n"
                             f"【输出】只输出 JSON（index 就是上面的序号）：\n"
                             f'{{"kinds": [{{"index": 1, "kind": "scene"}}]}}')
        for item in (data or {}).get("kinds") or []:
            try:
                idx = int(item.get("index"))
            except (TypeError, ValueError):
                continue
            kind = str(item.get("kind") or "").strip().lower()
            if idx in kinds and kind in (KIND_OBJECT, KIND_SCENE):
                kinds[idx] = kind
    except Exception as exc:  # noqa: BLE001
        log.warning("分镜类型判定失败，用规则兜底：%s", exc)
    return kinds


def build_generate_prompt(prompt: str, cfg: Config, kind: str = KIND_OBJECT) -> str:
    """拼生成提示词：分镜的检索词 + 风格/禁文字后缀（截到接口上限 1500 字符）。

    kind="scene" 时用场景向后缀（否则所有分镜都会变成静物小品，见上方注释）。
    """
    if str(kind).strip().lower() == KIND_SCENE:
        style = str(cfg.images.get("generate_style_suffix_scene")
                    or _GEN_SUFFIX_SCENE_DEFAULT)
    else:
        style = str(cfg.images.get("generate_style_suffix") or _GEN_SUFFIX_DEFAULT)
    p = (prompt or "").strip().rstrip("。")
    s = style.strip().rstrip("。")
    full = f"{p}。{s}" if p and s else (p or s)
    return full[:1500]


def _recorded_prompt(cache_dir: Path, scene_index: int) -> str | None:
    """上一次给这个分镜生成配图时用的**完整提示词**（从 _sources.json 反查）。

    有了它，「复用已有配图」才是安全的：提示词没变就复用，变了（换了画风/类型后缀）就重出。
    """
    import json

    f = cache_dir / "_sources.json"
    if not f.exists():
        return None
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    rows = data if isinstance(data, list) else (data.get("scenes") or data.get("entries") or [])
    for r in rows:
        if not isinstance(r, dict):
            continue
        try:
            if int(r.get("scene") or 0) != int(scene_index):
                continue
        except (TypeError, ValueError):
            continue
        if r.get("prompt"):
            return str(r["prompt"])
        # 行里通常没有 prompt，真正的提示词记在 attempts 里（生成那条记录）
        for a in r.get("attempts") or []:
            if isinstance(a, dict) and a.get("prompt"):
                return str(a["prompt"])
    return None


def generate_scene_image(scene_index: int, prompt: str, cfg: Config,
                         cache_dir: Path,
                         kind: str = KIND_OBJECT) -> tuple[Path | None, str, str, dict]:
    """用 MiniMax image-01 生成一张配图。返回 (路径, 出处标注, 来源名, 记录)。

    实测（2026-09）：
      · 出图约 24-27 秒/张（9:16 与 16:9 各测一次）
      · 提示词里写「无文字」模型仍会加伪书法题字与红印章 —— 所以
        ① 后缀里反复强调，② 生成后用 scripts/review_images.py 复核，
        复核发现带字就直接 reject 重生成（那时会带上「不要文字」的期望词）。
    """
    im = cfg.images
    if not bool(im.get("generate", False)):
        return None, "", "", {}
    key = env_get("MINIMAX_API_KEY")
    if not key:
        log.warning("images.generate 开着但取不到 MINIMAX_API_KEY，跳过生成")
        return None, "", "", {}

    import httpx

    base = str(cfg.tts.minimax.base_url).rstrip("/")
    full = build_generate_prompt(prompt, cfg, kind)
    # ---- 复用已有配图（2026-09-17 加）：重渲一遍不该把 18 张图重烧一遍
    # 条件：图在 + 提示词跟上次一模一样（换了画风/后缀就会重出）。
    # 想强制重出：--force 或把 images.reuse_existing 关掉。
    final = cache_dir / f"scene_{scene_index:03d}.jpg"
    if (bool(im.get("reuse_existing", True)) and not bool(cfg.runtime.get("force", False))
            and final.exists() and final.stat().st_size > 5000):
        prev = _recorded_prompt(cache_dir, scene_index)
        if prev is None or prev == full:
            log.info("分镜 %d 配图：复用已有 %s（提示词没变，不重新生成/不花钱）",
                     scene_index, final.name)
            return (final, f"AI 生成（{im.get('generate_model') or 'image-01'}）", "minimax-gen",
                    {"generator": str(im.get("generate_model") or "image-01"), "prompt": full,
                     "aspect": str(im.get("generate_aspect") or "3:4"), "kind": kind,
                     "reused": True, "file": final.name,
                     "license": "AI 生成（无第三方版权）"})
    payload = {
        "model": str(im.get("generate_model") or "image-01"),
        "prompt": full[:1500],
        "aspect_ratio": str(im.get("generate_aspect") or "3:4"),
        "response_format": "url",
        "n": 1,
        "prompt_optimizer": True,
        "aigc_watermark": bool(im.get("generate_aigc_watermark", False)),
    }
    rec: dict = {"generator": payload["model"], "prompt": full,
                 "aspect": payload["aspect_ratio"], "kind": kind}
    t0 = time.monotonic()
    try:
        with httpx.Client(timeout=float(im.get("generate_timeout", 300)),
                          follow_redirects=True) as c:
            r = c.post(f"{base}/image_generation",
                       headers={"Authorization": f"Bearer {key}",
                                "Content-Type": "application/json"}, json=payload)
            data = r.json()
        code = ((data.get("base_resp") or {}).get("status_code"))
        if code not in (0, None):
            log.warning("分镜 %d 生成配图失败：status_code=%s %s（退回图库检索）",
                        scene_index, code, (data.get("base_resp") or {}).get("status_msg"))
            rec["error"] = f"{code} {(data.get('base_resp') or {}).get('status_msg')}"
            return None, "", "", rec
        d = data.get("data") or {}
        url = (d.get("image_urls") or [None])[0]
        raw = cache_dir / f"_gen_{scene_index:03d}.png"
        raw.parent.mkdir(parents=True, exist_ok=True)
        with httpx.Client(timeout=300.0, follow_redirects=True) as c:
            if url:
                got = c.get(url, headers={"User-Agent": _UA})
                got.raise_for_status()
                raw.write_bytes(got.content)
            elif d.get("image_base64"):
                import base64
                raw.write_bytes(base64.b64decode(d["image_base64"][0]))
            else:
                log.warning("分镜 %d 生成接口没返回图片数据（退回图库检索）", scene_index)
                return None, "", "", rec
        final = cache_dir / f"scene_{scene_index:03d}.jpg"
        _prepare(raw, final)
        raw.unlink(missing_ok=True)
        spent = time.monotonic() - t0
        rec.update({"picked": str(url or "base64"), "file": final.name,
                    "download_seconds": round(spent, 1),
                    "license": "AI 生成（无第三方版权）"})
        log.info("分镜 %d 配图：%s（生成／%s／%.1fs）", scene_index, final.name,
                 payload["model"], spent)
        return (final, f"AI 生成（{payload['model']}）", "minimax-gen", rec)
    except Exception as exc:  # noqa: BLE001
        log.warning("分镜 %d 生成配图异常（退回图库检索）：%s", scene_index, exc)
        rec["error"] = str(exc)
        return None, "", "", rec


def fetch_for_scene(
    scene_index: int,
    queries: list[str],
    cfg: Config,
    cache_dir: Path,
    used: list[int],
    queries_en: list[str] | None = None,
    period: tuple[int, int] | None = None,
    kind: str = KIND_OBJECT,
) -> tuple[Path | None, str, str, list[dict]]:
    """给一个分镜找一张图。

    返回 (图片路径|None, 出处文本, 图源名, 尝试记录)。
    先按分镜自己的检索词找；找不到就用**兜底检索词**再找一轮
    （兜底词是「时代 + 泛画种」，实测能把馆里没有具体题材的情况救回来）。
    """
    im = cfg.images
    cache_dir.mkdir(parents=True, exist_ok=True)
    providers = active_providers(cfg)
    max_q = int(im.get("max_queries_per_scene", 3))

    qs = [q for q in (queries or []) if q][:max_q] or ["中国古代 文物"]
    qs_en = [q for q in (queries_en or []) if q][:max_q]

    # ---- 生成优先（若开启）：生成图不受图库索引限制，也没有第三方版权。
    # 失败（额度/限流/接口异常）就静默退回下面的图库检索，不会因此没图。
    all_attempts: list[dict] = []
    if bool(im.get("generate", False)):
        p, label, src, rec = generate_scene_image(scene_index, qs[0], cfg, cache_dir, kind)
        if rec:
            all_attempts.append({**rec, "round": "generate"})
        if p:
            return p, label, src, all_attempts
        log.info("分镜 %d 生成未成功，退回图库检索", scene_index)

    batches: list[tuple[list[str], list[str]]] = [(qs, qs_en)]
    fb_zh = [str(q) for q in (im.get("fallback_queries") or [])][:max_q]
    fb_en = [str(q) for q in (im.get("fallback_queries_en") or [])][:max_q]
    if fb_zh or fb_en:
        batches.append((fb_zh, fb_en))

    n = 0
    for bi, (b_zh, b_en) in enumerate(batches):
        if bi and not (b_zh or b_en):
            continue
        p, label, src, attempts, n = _try_batch(
            scene_index, b_zh, b_en, cfg, cache_dir, used, period, providers, n)
        for a in attempts:
            a["round"] = bi
        all_attempts += attempts
        if p:
            return p, label, src, all_attempts
        if bi == 0:
            log.info("分镜 %d 按原检索词没找到，换兜底检索词再试一轮", scene_index)

    log.warning("分镜 %d 两轮都没找到图（图源 %s，检索词 %s）→ 用渐变底图",
                scene_index, providers, (qs + qs_en)[:4])
    return None, "", "", all_attempts


def save_source_log(path: Path, entries: list[dict], cfg: Config) -> None:
    """把图源使用情况落盘：每个分镜试了什么、最后选了哪张、什么许可证。"""
    import json

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "license_policy": str(cfg.images.get("license_policy") or "clean"),
            "providers_active": active_providers(cfg),
            "entries": entries,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        log.debug("图源日志写入失败：%s", exc)
