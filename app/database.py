from __future__ import annotations

"""SQLite 数据层：连接管理 + schema 初始化。WAL 模式，每次操作独立连接，线程安全。"""
import sqlite3
from pathlib import Path

from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
    id INTEGER PRIMARY KEY,
    slug TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    name_zh TEXT DEFAULT '',
    ticker TEXT DEFAULT '',          -- 美股代码
    code TEXT DEFAULT '',            -- 港股代码（如 0700）
    market TEXT NOT NULL,            -- US / HK / PRIVATE
    cik TEXT DEFAULT '',             -- SEC CIK（美股，自动解析后回填）
    hkex_stock_id TEXT DEFAULT '',   -- 港交所内部 stockId（自动解析后回填）
    aliases TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY,
    key TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    channel TEXT NOT NULL,           -- ai / robot / stock
    tier TEXT NOT NULL DEFAULT 'media',   -- official / media / info / reconcile
    type TEXT NOT NULL,              -- rss / html / sec / hkex
    url TEXT DEFAULT '',
    company_slug TEXT DEFAULT '',    -- 个股源绑定的公司（如 yahoo-nvda → nvidia）
    enabled INTEGER NOT NULL DEFAULT 1,
    interval_minutes INTEGER NOT NULL DEFAULT 30,
    state TEXT NOT NULL DEFAULT '{}', -- 水位等状态 JSON
    fail_count INTEGER NOT NULL DEFAULT 0,
    last_success_at TEXT,
    last_run_at TEXT,
    last_error TEXT
);

CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES sources(id),
    url TEXT UNIQUE NOT NULL,
    title TEXT NOT NULL,
    title_en TEXT DEFAULT '',
    summary TEXT DEFAULT '',
    channel TEXT NOT NULL,           -- ai / robot / stock
    event_type TEXT DEFAULT '',      -- 股市: earnings/insider/buyback/ma/personnel/product/regulation/rating/offering/other
    score INTEGER,                   -- AI 重要性 0-100
    heat REAL NOT NULL DEFAULT 0,
    companies TEXT NOT NULL DEFAULT '[]',  -- 关联公司 slug 列表(JSON，冗余存一份便于渲染)
    official INTEGER NOT NULL DEFAULT 0,   -- 官方一手来源
    via TEXT NOT NULL DEFAULT 'normal',    -- normal / reconcile(对账补录)
    published_at TEXT NOT NULL,      -- ISO8601（含时区）
    fetched_at TEXT NOT NULL,
    extra TEXT NOT NULL DEFAULT '{}' -- 表单类型、PDF 链接等 JSON
);
CREATE INDEX IF NOT EXISTS idx_items_channel_pub ON items(channel, published_at DESC);
CREATE INDEX IF NOT EXISTS idx_items_pub ON items(published_at DESC);

CREATE TABLE IF NOT EXISTS item_companies (
    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    company_id INTEGER NOT NULL REFERENCES companies(id),
    PRIMARY KEY (item_id, company_id)
);
CREATE INDEX IF NOT EXISTS idx_ic_company ON item_companies(company_id);

CREATE TABLE IF NOT EXISTS clusters (
    id INTEGER PRIMARY KEY,
    channel TEXT NOT NULL,
    title TEXT NOT NULL,
    url TEXT DEFAULT '',
    heat REAL NOT NULL DEFAULT 0,
    source_count INTEGER NOT NULL DEFAULT 1,
    company_slugs TEXT NOT NULL DEFAULT '[]',
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_clusters_heat ON clusters(heat DESC);

CREATE TABLE IF NOT EXISTS cluster_members (
    cluster_id INTEGER NOT NULL REFERENCES clusters(id) ON DELETE CASCADE,
    item_id INTEGER UNIQUE NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    PRIMARY KEY (cluster_id, item_id)
);

CREATE TABLE IF NOT EXISTS daily_reports (
    id INTEGER PRIMARY KEY,
    date TEXT UNIQUE NOT NULL,       -- YYYY-MM-DD（报告覆盖的日期）
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fetch_log (
    id INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL,
    ran_at TEXT NOT NULL,
    ok INTEGER NOT NULL,
    new_items INTEGER NOT NULL DEFAULT 0,
    message TEXT DEFAULT ''
);

-- 中文全文搜索：trigram 分词器对 CJK 友好（query 需 >=3 字符，短词走 LIKE 兜底）
CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(
    title, summary, content='items', content_rowid='id', tokenize='trigram'
);
CREATE TRIGGER IF NOT EXISTS items_ai AFTER INSERT ON items BEGIN
    INSERT INTO items_fts(rowid, title, summary) VALUES (new.id, new.title, new.summary);
END;
CREATE TRIGGER IF NOT EXISTS items_ad AFTER DELETE ON items BEGIN
    INSERT INTO items_fts(items_fts, rowid, title, summary) VALUES ('delete', old.id, old.title, old.summary);
END;
CREATE TRIGGER IF NOT EXISTS items_au AFTER UPDATE ON items BEGIN
    INSERT INTO items_fts(items_fts, rowid, title, summary) VALUES ('delete', old.id, old.title, old.summary);
    INSERT INTO items_fts(rowid, title, summary) VALUES (new.id, new.title, new.summary);
END;
"""


def get_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_schema() -> None:
    with get_db() as db:
        db.executescript(SCHEMA)
