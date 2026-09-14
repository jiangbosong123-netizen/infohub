from __future__ import annotations

"""SQLite 数据层：连接管理 + schema 初始化。WAL 模式，每次操作独立连接，线程安全。"""
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .config import DB_PATH

MIGRATIONS = (
    (1, "versioned NLP result storage", (
        """CREATE TABLE IF NOT EXISTS nlp_results (
            id INTEGER PRIMARY KEY,
            item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
            analysis_type TEXT NOT NULL,
            pipeline_version TEXT NOT NULL,
            model TEXT NOT NULL DEFAULT '',
            input_json TEXT NOT NULL,
            output_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(item_id, analysis_type, pipeline_version)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_nlp_results_item ON nlp_results(item_id, analysis_type)",
        "CREATE INDEX IF NOT EXISTS idx_nlp_results_type_created ON nlp_results(analysis_type, created_at DESC)",
    )),
)

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
    title_zh TEXT DEFAULT '',        -- AI 翻译的中文标题（渲染时优先显示）
    summary TEXT DEFAULT '',
    channel TEXT NOT NULL,           -- ai / robot / stock
    event_type TEXT DEFAULT '',      -- 股市: earnings/insider/buyback/ma/personnel/product/regulation/rating/offering/other
    score INTEGER,                   -- AI 重要性 0-100
    tmt INTEGER,                     -- AI 判定是否 TMT 相关: 1=是 0=否(过滤隐藏) NULL=未判定
    heat REAL NOT NULL DEFAULT 0,
    reason TEXT DEFAULT '',          -- AI 推荐理由（为什么值得看）
    ai_cat TEXT DEFAULT '',          -- AI 频道子分类: model/product/industry/paper/opinion
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


class ManagedConnection(sqlite3.Connection):
    """Commit/rollback and close: sqlite3's default context manager never closes."""

    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()


def get_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30, factory=ManagedConnection)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_schema() -> None:
    with get_db() as db:
        db.executescript(SCHEMA)
        # 老库迁移：补 title_zh 列（CREATE TABLE IF NOT EXISTS 不会改已有表）
        cols = {r["name"] for r in db.execute("PRAGMA table_info(items)")}
        if "title_zh" not in cols:
            db.execute("ALTER TABLE items ADD COLUMN title_zh TEXT DEFAULT ''")
        for col, ddl in (("tmt", "INTEGER"), ("reason", "TEXT DEFAULT ''"),
                         ("ai_cat", "TEXT DEFAULT ''")):
            if col not in cols:
                db.execute(f"ALTER TABLE items ADD COLUMN {col} {ddl}")

        # v2 search index: migrate once, including historical translated titles.
        fts_cols = {r["name"] for r in db.execute("PRAGMA table_info(items_fts)")}
        if "title_zh" not in fts_cols:
            db.executescript("""
                BEGIN IMMEDIATE;
                DROP TRIGGER IF EXISTS items_ai;
                DROP TRIGGER IF EXISTS items_ad;
                DROP TRIGGER IF EXISTS items_au;
                DROP TABLE items_fts;
                CREATE VIRTUAL TABLE items_fts USING fts5(
                    title, title_zh, summary, content='items', content_rowid='id', tokenize='trigram');
                CREATE TRIGGER items_ai AFTER INSERT ON items BEGIN
                    INSERT INTO items_fts(rowid,title,title_zh,summary)
                    VALUES(new.id,new.title,new.title_zh,new.summary);
                END;
                CREATE TRIGGER items_ad AFTER DELETE ON items BEGIN
                    INSERT INTO items_fts(items_fts,rowid,title,title_zh,summary)
                    VALUES('delete',old.id,old.title,old.title_zh,old.summary);
                END;
                CREATE TRIGGER items_au AFTER UPDATE OF title,title_zh,summary ON items BEGIN
                    INSERT INTO items_fts(items_fts,rowid,title,title_zh,summary)
                    VALUES('delete',old.id,old.title,old.title_zh,old.summary);
                    INSERT INTO items_fts(rowid,title,title_zh,summary)
                    VALUES(new.id,new.title,new.title_zh,new.summary);
                END;
                INSERT INTO items_fts(items_fts) VALUES('rebuild');
                COMMIT;
            """)

        if 'raw_summary' not in cols:
            # Historical summaries may already be AI-generated; leave them NULL.
            db.execute('ALTER TABLE items ADD COLUMN raw_summary TEXT')
        db.executescript(DERIVED_SCHEMA)
        _apply_migrations(db)
        # Keep the trigger definition current on existing databases. Channel
        # changes also affect event membership and must enter the derived queue.
        db.executescript("""
            DROP TRIGGER IF EXISTS items_derived_update;
            CREATE TRIGGER items_derived_update
            AFTER UPDATE OF title,title_zh,summary,raw_summary,companies,score,tmt,event_type,
                            ai_cat,official,extra,published_at,channel ON items BEGIN
                INSERT OR IGNORE INTO derived_dirty(item_id) VALUES(new.id);
            END;
        """)
        db.execute("INSERT OR IGNORE INTO derived_dirty(item_id) SELECT id FROM items WHERE id NOT IN (SELECT item_id FROM indexed_items)")


def _apply_migrations(db: sqlite3.Connection) -> None:
    """Apply ordered, transactional schema changes once and retain an audit trail."""
    db.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
        version INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        applied_at TEXT NOT NULL
    )""")
    applied = {row[0] for row in db.execute("SELECT version FROM schema_migrations")}
    for version, name, statements in MIGRATIONS:
        if version in applied:
            continue
        for statement in statements:
            db.execute(statement)
        db.execute(
            "INSERT INTO schema_migrations(version,name,applied_at) VALUES(?,?,?)",
            (version, name, datetime.now(timezone.utc).isoformat()))


DERIVED_SCHEMA = """
CREATE TABLE IF NOT EXISTS item_discoveries (
    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    source_id INTEGER NOT NULL REFERENCES sources(id),
    first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
    PRIMARY KEY(item_id,source_id)
);
CREATE TABLE IF NOT EXISTS topics (
    slug TEXT PRIMARY KEY, name TEXT NOT NULL, group_key TEXT NOT NULL,
    description TEXT NOT NULL, rules TEXT NOT NULL, position INTEGER NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS item_topics (
    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    topic_slug TEXT NOT NULL REFERENCES topics(slug), evidence TEXT NOT NULL,
    PRIMARY KEY(item_id,topic_slug)
);
CREATE INDEX IF NOT EXISTS idx_item_topics_slug ON item_topics(topic_slug,item_id);
CREATE TABLE IF NOT EXISTS stories (
    id TEXT PRIMARY KEY, anchor_item_id INTEGER REFERENCES items(id) ON DELETE SET NULL,
    title TEXT NOT NULL, channel TEXT NOT NULL, url TEXT NOT NULL,
    heat REAL NOT NULL DEFAULT 0, source_count INTEGER NOT NULL DEFAULT 0,
    item_count INTEGER NOT NULL DEFAULT 0, first_at TEXT NOT NULL, last_at TEXT NOT NULL,
    company_slugs TEXT NOT NULL DEFAULT '[]', redirect_to TEXT REFERENCES stories(id)
);
CREATE INDEX IF NOT EXISTS idx_stories_recent ON stories(last_at DESC,heat DESC);
CREATE TABLE IF NOT EXISTS story_items (
    item_id INTEGER PRIMARY KEY REFERENCES items(id) ON DELETE CASCADE,
    story_id TEXT NOT NULL REFERENCES stories(id),
    match_reason TEXT NOT NULL DEFAULT '', match_score REAL NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_story_items_story ON story_items(story_id,item_id);
CREATE TABLE IF NOT EXISTS derived_dirty (
    item_id INTEGER PRIMARY KEY REFERENCES items(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS indexed_items (
    item_id INTEGER PRIMARY KEY REFERENCES items(id) ON DELETE CASCADE
);
CREATE TRIGGER IF NOT EXISTS items_derived_insert AFTER INSERT ON items BEGIN
    INSERT OR IGNORE INTO derived_dirty(item_id) VALUES(new.id);
END;
CREATE TRIGGER IF NOT EXISTS items_derived_update
AFTER UPDATE OF title,title_zh,summary,raw_summary,companies,score,tmt,event_type,ai_cat,official,extra,published_at,channel ON items BEGIN
    INSERT OR IGNORE INTO derived_dirty(item_id) VALUES(new.id);
END;
"""
