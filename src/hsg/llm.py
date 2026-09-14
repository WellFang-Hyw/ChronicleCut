"""统一的大模型调用层（文本）。

provider 都是 OpenAI 兼容协议（deepseek / minimax / aliyun）。
本项目文本模型锁定 deepseek —— 见 config.assert_text_provider。
MiniMax 系列会内联输出 <think>...</think>，这里统一剥离。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from .config import ApiKeys, Config

log = logging.getLogger("hsg.llm")

_THINK_RE = re.compile(r"<think(?:ing)?>.*?</think(?:ing)?>", re.S | re.I)
_FENCE_RE = re.compile(r"^\s*```(?:json|JSON)?\s*|\s*```\s*$")


def strip_think(text: str) -> str:
    text = _THINK_RE.sub("", text)
    for tag in ("<think", "<thinking"):
        idx = text.find(tag)
        if idx != -1:
            text = text[:idx]
    return text.strip()


def extract_json(text: str) -> Any:
    """从模型输出里稳健地抠出 JSON。"""
    text = strip_think(text).strip()
    text = _FENCE_RE.sub("", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    for op, cl in (("{", "}"), ("[", "]")):
        s, e = text.find(op), text.rfind(cl)
        if s != -1 and e > s:
            frag = text[s : e + 1]
            for cand in (frag, re.sub(r",\s*([}\]])", r"\1", frag)):
                try:
                    return json.loads(cand)
                except json.JSONDecodeError:
                    continue
    raise ValueError(f"无法从模型输出里解析 JSON：\n{text[:600]}")


class LLM:
    def __init__(self, cfg: Config, keys: ApiKeys | None = None):
        self.cfg = cfg
        self.keys = keys or ApiKeys.from_env()
        self.provider = str(cfg.llm.provider)
        if self.provider not in ("minimax", "deepseek", "aliyun"):
            raise ValueError(f"未知的 llm.provider: {self.provider}")
        sub = cfg.llm[self.provider]
        self.base_url = str(sub.base_url).rstrip("/")
        self.model = str(sub.model)
        self.timeout = float(cfg.llm.get("timeout", 180))
        self.max_tokens = int(cfg.llm.get("max_tokens", 8192))
        self.max_tokens_cap = int(cfg.llm.get("max_tokens_cap", 32768))
        self.api_key = self.keys.for_provider(self.provider)
        self.last_finish_reason = ""
        self.calls = 0
        self._client = httpx.Client(timeout=self.timeout, follow_redirects=True)

    def close(self) -> None:
        self._client.close()

    def _ensure_client(self) -> httpx.Client:
        """客户端被关掉之后再调就自动重开。

        为什么需要：`with LLM(...)` 块退出时会 close()，但后面的「按时长自适应
        改写章节」还在用同一个 llm 对象 —— 一旦真的需要改写，就会撞上
        RuntimeError: Cannot send a request, as the client has been closed.
        这个 bug 只在「TTS 实测时长超出区间」时才显形，属于埋得比较深的坑。
        """
        if getattr(self._client, "is_closed", False):
            self._client = httpx.Client(timeout=self.timeout, follow_redirects=True)
        return self._client

    def __enter__(self) -> "LLM":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, max=20), reraise=True)
    def chat(
        self,
        system: str,
        user: str,
        *,
        json_mode: bool = False,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> str:
        budget = int(max_tokens or self.max_tokens)
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.7 if temperature is None else temperature,
            "max_tokens": budget,
            "stream": False,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        resp = self._ensure_client().post(
            f"{self.base_url}/chat/completions",
            json=payload,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
        )
        if resp.status_code >= 400:
            log.error("LLM %s 返回 %s: %s", self.provider, resp.status_code, resp.text[:400])
            resp.raise_for_status()
        data = resp.json()
        self.calls += 1
        try:
            choice = data["choices"][0]
            msg = choice["message"]["content"]
        except (KeyError, IndexError) as exc:
            raise RuntimeError(f"LLM 响应结构异常: {json.dumps(data)[:400]}") from exc
        self.last_finish_reason = str(choice.get("finish_reason") or "")
        if self.last_finish_reason == "length":
            log.warning("LLM 输出被 max_tokens=%d 截断", budget)
        return strip_think(msg or "")

    def chat_json(
        self,
        system: str,
        user: str,
        *,
        max_tokens: int | None = None,
        tries: int = 3,
        **kw: Any,
    ) -> Any:
        """要 JSON；被截断或解析失败时自动加倍 max_tokens 重试。"""
        budget = int(max_tokens or self.max_tokens)
        last_err: Exception | None = None
        for attempt in range(1, tries + 1):
            raw = self.chat(system, user, json_mode=True, max_tokens=budget, **kw)
            if self.last_finish_reason != "length":
                try:
                    return extract_json(raw)
                except ValueError as exc:
                    last_err = exc
                    log.warning("第 %d 次 JSON 解析失败（输出 %d 字符）", attempt, len(raw))
            else:
                last_err = ValueError("输出被截断")
            if attempt < tries and budget < self.max_tokens_cap:
                budget = min(self.max_tokens_cap, budget * 2)
                log.warning("加大 max_tokens 到 %d 重试（第 %d/%d 次）", budget, attempt + 1, tries)
        raise ValueError(f"LLM 未能返回可解析的 JSON：{last_err}")
