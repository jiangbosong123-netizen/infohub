from __future__ import annotations

"""抓取共用的 HTTP 客户端：浏览器 UA、超时、单次重试。"""
import logging
import time

import httpx

log = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


def fetch(url: str, *, headers: dict | None = None, timeout: float = 25.0,
          retries: int = 1) -> httpx.Response:
    """GET，失败重试一次；遇到 429 限流则等 30 秒再试。"""
    merged = {**HEADERS, **(headers or {})}
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            resp = httpx.get(url, headers=merged, timeout=timeout, follow_redirects=True)
            if resp.status_code == 429 and attempt < retries:
                time.sleep(30)  # 限流：等一波再试
                continue
            resp.raise_for_status()
            return resp
        except Exception as exc:  # noqa: BLE001 - 统一记日志后重试
            last_exc = exc
            if attempt < retries:
                time.sleep(2)
    raise last_exc  # type: ignore[misc]
