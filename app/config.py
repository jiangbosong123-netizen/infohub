from __future__ import annotations

"""全局配置：读 .env，提供统一设置对象。"""
import os
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

APP_TZ = ZoneInfo(os.getenv("APP_TZ", "Asia/Shanghai"))
DB_PATH = BASE_DIR / "data" / "app.db"
WATCHLIST_PATH = BASE_DIR / "config" / "watchlist.yaml"

# --- LLM（OpenAI 兼容接口：智谱 GLM / DeepSeek / OpenAI 均可）---
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "").strip()
LLM_API_KEY = os.getenv("LLM_API_KEY", "").strip()
LLM_MODEL = os.getenv("LLM_MODEL", "").strip()

# --- 抓取调度 ---
# 官方层(SEC/港交所)每 10 分钟，媒体/资讯 RSS 每 20-30 分钟（在 sources.py 里按源配置）
CRAWL_TICK_MINUTES = int(os.getenv("CRAWL_TICK_MINUTES", "5"))
RECONCILE_HOUR = int(os.getenv("RECONCILE_HOUR", "6"))      # 每日对账时间
RECONCILE_MINUTE = int(os.getenv("RECONCILE_MINUTE", "30"))
REPORT_HOUR = int(os.getenv("REPORT_HOUR", "8"))            # 日报生成时间
REPORT_MINUTE = int(os.getenv("REPORT_MINUTE", "0"))

# --- SEC 官方要求：请求需带可联系的 User-Agent ---
SEC_USER_AGENT = os.getenv("SEC_USER_AGENT", "personal-news-aggregator admin@example.com")

WEB_HOST = os.getenv("WEB_HOST", "127.0.0.1").strip() or "127.0.0.1"
WEB_PORT = int(os.getenv("PORT", "8000"))


def llm_enabled() -> bool:
    return bool(LLM_API_KEY and LLM_BASE_URL and LLM_MODEL)
